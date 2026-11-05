#!/usr/bin/env python3
"""Exercise actual trial handlers with fake Telegram, panels and persistence.

Only the two handler functions are compiled from their AST. The application is
never imported, and these checks do not access a database or external service.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/shop_bot/bot/handlers.py"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=101),
        bot=object(),
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        answer=AsyncMock(),
    )


def _callback(message: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=101), message=message, answer=AsyncMock()
    )


def _panel_result(host: str) -> dict:
    return {
        "host_name": host,
        "email": f"fake-{host}",
        "client_uuid": f"fake-uuid-{host}",
        "expiry_timestamp_ms": int((NOW + timedelta(days=1)).timestamp() * 1000),
        "connection_string": "fake-connection",
    }


def _handlers(hosts: tuple[str, ...] = ("fake-a",)) -> dict:
    # Mimic the conditional database UPDATE, including competing requests.
    claim_state = {"used": False}

    def claim(_user_id: int) -> bool:
        if claim_state["used"]:
            return False
        claim_state["used"] = True
        return True

    def release(_user_id: int) -> bool:
        claim_state["used"] = False
        return True

    settings = {"trial_enabled": "true", "trial_duration_days": "1", "domain": "fake"}
    namespace = {
        "claim_state": claim_state,
        "get_user": Mock(return_value={"agreed_to_terms": True}),
        "get_setting": Mock(side_effect=settings.get),
        "has_ever_purchased_vpn_subscription": Mock(return_value=False),
        "get_all_hosts": Mock(return_value=[{"host_name": host} for host in hosts]),
        "get_user_trial_keys": Mock(return_value=[]),
        "claim_trial_if_unused": Mock(side_effect=claim),
        "release_trial_claim": Mock(side_effect=release),
        "get_or_create_subscription_token": Mock(return_value="fake-token"),
        "add_new_key": Mock(return_value=1),
        "update_key_info": Mock(return_value=True),
        "xui_api": SimpleNamespace(
            create_or_update_key_on_host_absolute_expiry=AsyncMock(
                side_effect=[_panel_result(host) for host in hosts]
            )
        ),
        "time_utils": SimpleNamespace(
            get_msk_now=lambda: NOW,
            get_timestamp_ms=lambda value: int(value.timestamp() * 1000),
            from_timestamp_ms=lambda value: datetime.fromtimestamp(
                value / 1000, tz=timezone.utc
            ),
        ),
        "timedelta": timedelta,
        "_build_subscription_link": Mock(return_value="fake-subscription"),
        "_subscription_clients_hint": Mock(return_value="fake-hint"),
        "keyboards": SimpleNamespace(
            create_global_info_keyboard=Mock(return_value=None),
            create_back_to_menu_keyboard=Mock(return_value=None),
        ),
        "notify_admin_of_trial": AsyncMock(),
        "logger": Mock(spec=logging.Logger),
    }
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }
    selected = []
    for name in ("process_trial_key_creation", "trial_period_handler"):
        node = functions[name]
        node.decorator_list = []
        selected.append(node)
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            *selected,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
    return namespace


async def _run_checks() -> None:
    ns = _handlers()
    callback = _callback(_message())
    callback.answer.side_effect = RuntimeError("expired callback")
    try:
        await ns["trial_period_handler"](callback, None)
    except RuntimeError as exc:
        assert str(exc) == "expired callback"
    else:
        raise AssertionError("An expired callback must abort before claiming")
    ns["claim_trial_if_unused"].assert_not_called()
    ns["xui_api"].create_or_update_key_on_host_absolute_expiry.assert_not_awaited()

    ns = _handlers()
    message = _message()
    message.edit_text.side_effect = [RuntimeError("cannot edit"), None]
    await ns["trial_period_handler"](_callback(message), None)
    ns["release_trial_claim"].assert_called_once_with(101)
    assert not ns["claim_state"]["used"]
    ns["xui_api"].create_or_update_key_on_host_absolute_expiry.assert_not_awaited()

    ns = _handlers()
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def pending_panel(**_kwargs):
        entered.set()
        await proceed.wait()
        return _panel_result("fake-a")

    ns["xui_api"].create_or_update_key_on_host_absolute_expiry.side_effect = pending_panel
    first = asyncio.create_task(ns["trial_period_handler"](_callback(_message()), None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await ns["trial_period_handler"](_callback(_message()), None)
    finally:
        proceed.set()
        await first
    assert ns["claim_trial_if_unused"].call_count == 2
    ns["xui_api"].create_or_update_key_on_host_absolute_expiry.assert_awaited_once()
    ns["add_new_key"].assert_called_once()
    ns["release_trial_claim"].assert_not_called()

    ns = _handlers(("fake-a", "fake-b"))
    ns["xui_api"].create_or_update_key_on_host_absolute_expiry.side_effect = [None, None]
    await ns["trial_period_handler"](_callback(_message()), None)
    ns["release_trial_claim"].assert_called_once_with(101)
    ns["add_new_key"].assert_not_called()
    assert not ns["claim_state"]["used"]

    ns = _handlers()
    ns["add_new_key"].return_value = None
    await ns["trial_period_handler"](_callback(_message()), None)
    ns["release_trial_claim"].assert_not_called()
    assert ns["claim_state"]["used"]

    ns = _handlers(("fake-a", "fake-b"))
    ns["xui_api"].create_or_update_key_on_host_absolute_expiry.side_effect = [
        _panel_result("fake-a"), None
    ]
    await ns["trial_period_handler"](_callback(_message()), None)
    ns["add_new_key"].assert_called_once()
    ns["release_trial_claim"].assert_not_called()
    assert ns["claim_state"]["used"]

    for failing_step in ("user_notification", "admin_notification"):
        ns = _handlers()
        message = _message()
        failing_mock = (
            message.answer
            if failing_step == "user_notification"
            else ns["notify_admin_of_trial"]
        )
        failing_mock.side_effect = RuntimeError(failing_step)
        await ns["trial_period_handler"](_callback(message), None)
        ns["add_new_key"].assert_called_once()
        ns["release_trial_claim"].assert_not_called()
        assert ns["claim_state"]["used"]


def main() -> None:
    asyncio.run(_run_checks())
    print("Trial delivery safety: OK (8 isolated failure/concurrency scenarios)")


if __name__ == "__main__":
    main()

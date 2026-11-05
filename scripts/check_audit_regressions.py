#!/usr/bin/env python3
"""Checks for referral, payment-target, and logging audit regressions."""

from __future__ import annotations

import logging
import os
import tempfile
from decimal import Decimal
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["DB_PATH"] = str(Path(tmp_dir) / "test.db")

        from shop_bot.data_manager import database

        database.initialize_db()
        database.register_user_if_not_exists(10, "phantom", 999999)
        database.register_user_if_not_exists(11, "self", 11)
        database.register_user_if_not_exists(12, "referrer", None)
        database.register_user_if_not_exists(13, "referred", 12)

        assert database.get_user(10)["referred_by"] is None
        assert database.get_user(11)["referred_by"] is None
        assert database.get_user(13)["referred_by"] == 12

        from shop_bot.bot.handlers import (
            _calculate_plan_quote,
            _validate_plan_selection,
        )

        global_plan = {
            "host_name": "ALL",
            "service_type": "xui",
            "price": 100,
        }
        single_xui_plan = {
            "host_name": "one-host",
            "service_type": "xui",
            "price": 10,
        }
        assert _validate_plan_selection(
            global_plan, host_name="ALL", action="new", key_id=0, user_id=13
        )[0]
        assert not _validate_plan_selection(
            single_xui_plan,
            host_name="ALL",
            action="new",
            key_id=0,
            user_id=13,
        )[0]

        handlers_source = (
            Path(__file__).resolve().parents[1]
            / "src/shop_bot/bot/handlers.py"
        ).read_text(encoding="utf-8")
        assert "panel_mutated = True" in handlers_source
        assert "if not results and not panel_mutated:" in handlers_source
        assert "await asyncio.to_thread(\n                Payment.create" in handlers_source
        assert "ClientTimeout(total=10, connect=3, sock_read=5)" in handlers_source
        assert "<pre>Ошибка: {e}</pre>" not in handlers_source
        assert not _validate_plan_selection(
            {"host_name": "proxy-a", "service_type": "mtg", "price": 10},
            host_name="proxy-b",
            action="new",
            key_id=0,
            user_id=13,
        )[0]

        price, discounted = _calculate_plan_quote(global_plan, database.get_user(13))
        assert discounted and price == Decimal("95.00")
        price, discounted = _calculate_plan_quote(global_plan, database.get_user(10))
        assert not discounted and price == Decimal("100")

    from shop_bot.utils.logging_utils import SensitiveUrlFilter

    record = logging.LogRecord(
        "py3xui.api.api_base",
        logging.WARNING,
        __file__,
        1,
        "Request to https://panel.example:1234/secret/api failed",
        (),
        None,
    )
    assert SensitiveUrlFilter().filter(record)
    assert record.getMessage() == (
        "Request to https://panel.example:1234/<redacted> failed"
    )

    from shop_bot.webhook_server.subscription_api import (
        _safe_subscription_header_name,
    )

    assert _safe_subscription_header_name("Name\r\nInjected: yes") == (
        "Name Injected: yes"
    )

    webhook_source = (
        Path(__file__).resolve().parents[1]
        / "src/shop_bot/webhook_server/app.py"
    ).read_text(encoding="utf-8")
    assert "legacy_secret_valid" not in webhook_source
    assert "path_secret_valid" not in webhook_source

    print("Audit regression checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

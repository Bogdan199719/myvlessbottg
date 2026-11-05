#!/usr/bin/env python3
"""Check panel write ownership when an awaiting coroutine is cancelled."""

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shop_bot.modules import xui_api  # noqa: E402


async def main() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    other_panel_started = threading.Event()

    hosts = {
        "first": {"host_url": "https://PANEL.example.test/panel/"},
        "second": {"host_url": "https://panel.example.test/panel"},
        "other": {"host_url": "https://other.example.test/panel"},
    }

    @xui_api._serialize_host_write
    def fake_write(host_name: str) -> None:
        if host_name == "first":
            first_started.set()
            assert release_first.wait(timeout=3)
        elif host_name == "second":
            second_started.set()
        else:
            other_panel_started.set()

    with patch.object(xui_api, "get_host", side_effect=hosts.get):
        first = asyncio.create_task(asyncio.to_thread(fake_write, "first"))
        try:
            assert await asyncio.to_thread(first_started.wait, 2)
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass

            second = asyncio.create_task(asyncio.to_thread(fake_write, "second"))
            other = asyncio.create_task(asyncio.to_thread(fake_write, "other"))
            assert await asyncio.to_thread(other_panel_started.wait, 2)
            assert not second_started.is_set(), "same panel wrote while first worker was active"
        finally:
            release_first.set()

        await asyncio.wait_for(second, timeout=3)
        await asyncio.wait_for(other, timeout=3)
        assert second_started.is_set()

    guarded = (
        xui_api.validate_host_write_access,
        xui_api._create_or_update_key_on_host_sync,
        xui_api._fix_client_parameters_on_host_sync,
        xui_api._fix_all_client_parameters_on_host_sync,
        xui_api._sync_clients_state_on_host_sync,
        xui_api._delete_client_on_host_sync,
        xui_api._sync_xtls_for_host,
    )
    assert all(hasattr(operation, "__wrapped__") for operation in guarded)
    print("Panel write serialization check: OK")


if __name__ == "__main__":
    asyncio.run(main())

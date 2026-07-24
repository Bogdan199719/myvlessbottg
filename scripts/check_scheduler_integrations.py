#!/usr/bin/env python3
"""Deterministic checks for non-blocking and bounded scheduler integrations."""

import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shop_bot.data_manager import scheduler  # noqa: E402


def check_client_state_reconciliation_interval() -> None:
    assert scheduler._CLIENT_STATE_ENFORCE_INTERVAL_SECONDS == 300
    source = (ROOT / "src/shop_bot/data_manager/scheduler.py").read_text(
        encoding="utf-8"
    )
    assert (
        "current_time - last_client_state_enforce_time"
        "\n                >= _CLIENT_STATE_ENFORCE_INTERVAL_SECONDS" in source
    ), "full XUI reconciliation must be interval-gated"


async def check_xui_snapshot_does_not_block_loop() -> None:
    original_get_hosts = scheduler.database.get_all_hosts
    original_loader = scheduler._load_xui_panel_snapshot
    scheduler._host_failure_backoff.clear()
    scheduler.database.get_all_hosts = lambda enabled: [
        {
            "host_name": "slow-xui",
            "host_url": "https://example.invalid",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        }
    ]

    def slow_snapshot(_host):
        time.sleep(0.08)
        return None

    scheduler._load_xui_panel_snapshot = slow_snapshot
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.06
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0.005)

    try:
        await asyncio.gather(scheduler.sync_keys_with_panels(), ticker())
        assert ticks >= 5, f"event loop was blocked during XUI snapshot (ticks={ticks})"
    finally:
        scheduler.database.get_all_hosts = original_get_hosts
        scheduler._load_xui_panel_snapshot = original_loader
        scheduler._host_failure_backoff.clear()


async def check_mtg_concurrency_and_backoff() -> None:
    original_get_keys = scheduler.database.get_keys_by_service_type
    original_get_hosts = scheduler.database.get_all_mtg_hosts
    original_enable = scheduler.mtg_api.enable_proxy_for_user
    original_disable = scheduler.mtg_api.disable_proxy_for_user
    original_global_limit = scheduler._MTG_ENFORCE_MAX_CONCURRENCY
    original_host_limit = scheduler._MTG_ENFORCE_PER_HOST_CONCURRENCY
    scheduler._mtg_failure_backoff.clear()

    keys = []
    for host_name in ("mtg-a", "mtg-b"):
        for index in range(5):
            keys.append(
                {
                    "host_name": host_name,
                    "key_email": f"{host_name}-{index}",
                    "xui_client_uuid": str(index + 1),
                    "expiry_date": "2099-01-01T00:00:00+03:00",
                }
            )

    scheduler.database.get_keys_by_service_type = lambda service: keys
    scheduler.database.get_all_mtg_hosts = lambda enabled: [
        {"host_name": "mtg-a"},
        {"host_name": "mtg-b"},
    ]
    scheduler._MTG_ENFORCE_MAX_CONCURRENCY = 3
    scheduler._MTG_ENFORCE_PER_HOST_CONCURRENCY = 2

    running = 0
    max_running = 0
    running_by_host = defaultdict(int)
    max_by_host = defaultdict(int)
    calls = 0

    async def toggle(host_name, proxy_name, node_id):
        nonlocal running, max_running, calls
        calls += 1
        running += 1
        running_by_host[host_name] += 1
        max_running = max(max_running, running)
        max_by_host[host_name] = max(
            max_by_host[host_name], running_by_host[host_name]
        )
        await asyncio.sleep(0.01)
        running -= 1
        running_by_host[host_name] -= 1
        return host_name != "mtg-b"

    scheduler.mtg_api.enable_proxy_for_user = toggle
    scheduler.mtg_api.disable_proxy_for_user = toggle
    try:
        await scheduler.enforce_mtg_proxies_state()
        assert calls == len(keys), f"expected {len(keys)} MTG calls, got {calls}"
        assert max_running <= 3, f"global concurrency exceeded: {max_running}"
        assert all(value <= 2 for value in max_by_host.values()), (
            f"per-host concurrency exceeded: {dict(max_by_host)}"
        )
        assert scheduler._is_mtg_host_in_failure_backoff("mtg-b")

        calls_before_backoff_check = calls
        await scheduler.enforce_mtg_proxies_state()
        assert calls - calls_before_backoff_check == 5, (
            "failed MTG host was not skipped while in backoff"
        )
    finally:
        scheduler.database.get_keys_by_service_type = original_get_keys
        scheduler.database.get_all_mtg_hosts = original_get_hosts
        scheduler.mtg_api.enable_proxy_for_user = original_enable
        scheduler.mtg_api.disable_proxy_for_user = original_disable
        scheduler._MTG_ENFORCE_MAX_CONCURRENCY = original_global_limit
        scheduler._MTG_ENFORCE_PER_HOST_CONCURRENCY = original_host_limit
        scheduler._mtg_failure_backoff.clear()


async def check_mtg_login_is_single_flight() -> None:
    mtg_api = scheduler.mtg_api
    original_get_host = mtg_api.get_mtg_host
    original_client_session = mtg_api.aiohttp.ClientSession
    mtg_api._token_cache.clear()
    mtg_api._token_locks.clear()
    login_calls = 0

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            await asyncio.sleep(0.01)
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def json(self):
            return {"token": "shared-token"}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def post(self, *args, **kwargs):
            nonlocal login_calls
            login_calls += 1
            return FakeResponse()

    mtg_api.get_mtg_host = lambda host_name: {
        "host_url": "https://example.invalid",
        "username": "user",
        "password": "pass",
    }
    mtg_api.aiohttp.ClientSession = FakeSession
    try:
        tokens = await asyncio.gather(
            *(mtg_api._get_token("mtg-single-flight") for _ in range(6))
        )
        assert tokens == ["shared-token"] * 6
        assert login_calls == 1, (
            f"parallel callers performed {login_calls} MTG logins"
        )
    finally:
        mtg_api.get_mtg_host = original_get_host
        mtg_api.aiohttp.ClientSession = original_client_session
        mtg_api._token_cache.clear()
        mtg_api._token_locks.clear()


async def main() -> None:
    check_client_state_reconciliation_interval()
    await check_xui_snapshot_does_not_block_loop()
    await check_mtg_concurrency_and_backoff()
    await check_mtg_login_is_single_flight()
    print("Scheduler integration checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

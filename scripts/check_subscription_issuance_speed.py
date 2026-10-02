#!/usr/bin/env python3
"""Exercise issuance concurrency and persistence without production data or HTTP.

Handler functions are compiled from their AST with fake dependencies so this
check does not start the application or require payment/Telegram credentials.
The timing comparison models equal HTTP delays; it is not a production benchmark.
"""

import ast
import asyncio
import logging
import sys
import threading
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shop_bot.modules import panel_jobs
from shop_bot.modules.host_selector import panel_group_key
from shop_bot.utils import time_utils


def load_function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    node.decorator_list = []
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


def run_check(coro):
    """Bound checks, including executor shutdown in restricted environments."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def watchdog():
        while True:
            await asyncio.sleep(0.005)

    ticker = loop.create_task(watchdog())
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, timeout=10))
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor(timeout=5))
        ticker.cancel()
        loop.run_until_complete(asyncio.gather(ticker, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


HOSTS = [
    {"host_name": f"panel{panel}-inbound{inbound}", "host_url": f"https://panel{panel}.test/"}
    for panel in range(5) for inbound in range(3)
]
HOST_MAP = {host["host_name"]: host for host in HOSTS}


async def check_grouping():
    active = set()
    peak = 0
    seen = []

    async def operation(host):
        nonlocal peak
        panel = host["host_url"]
        assert panel not in active, "overlapping operations on the same panel"
        active.add(panel)
        peak = max(peak, len(active))
        try:
            await asyncio.sleep(0.02)
            seen.append(host["host_name"])
            return host["host_name"]
        finally:
            active.remove(panel)

    with patch.object(panel_jobs, "get_host", side_effect=HOST_MAP.get):
        start = time.monotonic()
        sequential = [await operation(host) for host in HOSTS]
        sequential_seconds = time.monotonic() - start
        start = time.monotonic()
        concurrent = await panel_jobs.run_panel_jobs(HOSTS, lambda h: h["host_name"], operation)
        concurrent_seconds = time.monotonic() - start
    assert sequential == concurrent
    assert peak == 5
    assert len(seen) == 30
    assert concurrent_seconds < sequential_seconds * 0.6
    print(f"Simulated HTTP delays, 15 hosts / 5 panels: sequential={sequential_seconds:.3f}s concurrent={concurrent_seconds:.3f}s")

    # A sixth panel must wait for the configured capacity, even with many jobs.
    extra = [{"host_name": f"extra{i}", "host_url": f"https://extra{i}.test"} for i in range(7)]
    mapping = {h["host_name"]: h for h in extra}
    peak = 0
    with patch.object(panel_jobs, "get_host", side_effect=mapping.get):
        await panel_jobs.run_panel_jobs(extra, lambda h: h["host_name"], operation)
    assert peak == panel_jobs.MAX_CONCURRENT_PANELS

    # Equivalent panel URL spellings must share a queue.
    aliases = [dict(HOSTS[0]), dict(HOSTS[1])]
    aliases[1]["host_url"] = "https://PANEL0.test"
    mapping = {h["host_name"]: h for h in aliases}
    order = []
    async def alias_operation(host):
        order.append((host["host_name"], "start"))
        await asyncio.sleep(0)
        order.append((host["host_name"], "end"))
    with patch.object(panel_jobs, "get_host", side_effect=mapping.get):
        await panel_jobs.run_panel_jobs(aliases, lambda h: h["host_name"], alias_operation)
    assert [phase for _, phase in order] == ["start", "end", "start", "end"]


async def check_cancellation():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    async def operation(host):
        calls.append(host["host_name"])
        started.set()
        await release.wait()
    with patch.object(panel_jobs, "get_host", side_effect=HOST_MAP.get):
        task = asyncio.create_task(panel_jobs.run_panel_jobs(HOSTS, lambda h: h["host_name"], operation))
        await started.wait()
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("batch swallowed cancellation")
        release.set()
        await asyncio.sleep(0)
        assert len(calls) == 5, "queued jobs started after cancellation"

        cancelled = asyncio.Event()
        async def failing_operation(host):
            if host is HOSTS[0]:
                await asyncio.sleep(0)
                raise RuntimeError("expected unexpected worker failure")
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        try:
            await panel_jobs.run_panel_jobs(HOSTS, lambda h: h["host_name"], failing_operation)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unexpected worker failure was swallowed")
        assert cancelled.is_set(), "failed batch left siblings running"
    assert await panel_jobs.run_panel_jobs([], lambda h: h, operation) == []


async def check_worker_lock():
    """Run the actual lock decorators without importing the HTTP SDK."""
    namespace = {
        "threading": threading,
        "wraps": wraps,
        "panel_group_key": panel_group_key,
        "_panel_write_locks": {},
        "_panel_write_locks_guard": threading.Lock(),
        "get_host": HOST_MAP.get,
    }
    path = "src/shop_bot/modules/xui_api.py"
    load_function(path, "_get_panel_write_lock", namespace)
    # Retain the wrapper returned by this factory; it owns the thread lock.
    decorate = load_function(path, "_serialize_host_write", namespace)
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    other_started = threading.Event()

    @decorate
    def write(host_name):
        if host_name == HOSTS[0]["host_name"]:
            started.set()
            assert release.wait(3)
        elif host_name == HOSTS[1]["host_name"]:
            second_started.set()
        else:
            other_started.set()

    async def wait_event(event):
        for _ in range(400):
            if event.is_set():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("worker did not start")

    first = asyncio.create_task(asyncio.to_thread(write, HOSTS[0]["host_name"]))
    second = other = None
    try:
        await wait_event(started)
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
        second = asyncio.create_task(asyncio.to_thread(write, HOSTS[1]["host_name"]))
        other = asyncio.create_task(asyncio.to_thread(write, HOSTS[3]["host_name"]))
        await wait_event(other_started)
        assert not second_started.is_set(), "cancellation released the panel lock early"
    finally:
        release.set()
        await asyncio.gather(*(t for t in (second, other) if t is not None))
    assert second_started.is_set()


class FakeIssuance:
    def __init__(self, fail_host=None):
        self.calls = []
        self.fail_host = fail_host
        self.persisted = []

    async def create_or_update_key_on_host_absolute_expiry(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        if kwargs["host_name"] == self.fail_host:
            raise RuntimeError("expected panel failure")
        return {
            "host_name": kwargs["host_name"], "email": kwargs["email"],
            "client_uuid": "fake-uuid", "expiry_timestamp_ms": kwargs["target_expiry_ms"],
            "connection_string": "vless://fake",
        }

    def add_new_key(self, *args, **kwargs):
        self.persisted.append(kwargs or args)
        return len(self.persisted)

    def update_key_info(self, *args, **kwargs):
        self.persisted.append((args, kwargs))
        return True


async def check_payment():
    fake = FakeIssuance(fail_host=HOSTS[4]["host_name"])
    existing = {"key_id": 99, "key_email": "preserved-trial-email"}
    namespace = {
        "xui_api": fake, "time_utils": time_utils, "logger": logging.getLogger("check"),
        "run_panel_jobs": panel_jobs.run_panel_jobs,
        "_find_existing_xui_key_for_host": lambda user, host: existing if host == HOSTS[0]["host_name"] else None,
        "update_key_info": fake.update_key_info, "update_key_plan_id": lambda *args: True,
        "add_new_key": fake.add_new_key,
    }
    fn = load_function("src/shop_bot/bot/handlers.py", "_execute_payment_for_hosts", namespace)
    target = time_utils.get_timestamp_ms(time_utils.get_msk_now() + timedelta(days=30))
    with patch.object(panel_jobs, "get_host", side_effect=HOST_MAP.get):
        results, primary = await fn(101, "ALL", "new", 1, 30, [(h["host_name"], "new-email") for h in HOSTS], 0, target)
    assert len(results) == len(fake.persisted) == 14
    assert primary is None
    assert [r["host_name"] for r in results] == [h["host_name"] for h in HOSTS if h["host_name"] != fake.fail_host]
    assert fake.calls[0]["email"] == existing["key_email"]
    assert all(c["target_expiry_ms"] == target for c in fake.calls)
    assert all(c["preserve_longer_expiry"] for c in fake.calls)

    # A panel success with failed DB persistence must never count as issued.
    namespace["update_key_info"] = lambda *args, **kwargs: False
    with patch.object(panel_jobs, "get_host", side_effect=HOST_MAP.get):
        results, primary = await fn(101, "ALL", "new", 1, 30, [(HOSTS[0]["host_name"], "new")], 0, target)
    assert results == [] and primary is None
    namespace["add_new_key"] = lambda *args, **kwargs: None
    with patch.object(panel_jobs, "get_host", side_effect=HOST_MAP.get):
        results, _ = await fn(101, "ALL", "new", 1, 30, [(HOSTS[2]["host_name"], "new")], 0, target)
    assert results == []


def check_admin(custom=False, fail=False, db_fail=False, new=False):
    fake = FakeIssuance(HOSTS[4]["host_name"] if fail else None)
    flashes = []
    stats = []
    existing_expiry = time_utils.get_msk_now() + timedelta(days=90)
    keys = [dict(host, key_id=i+1, key_email=f"preserved{i}", plan_id=0, service_type="xui", expiry_date=existing_expiry.isoformat()) for i, host in enumerate(HOSTS)]
    if new:
        keys = []
    namespace = {
        "request": SimpleNamespace(form={"custom_days": "7"} if custom else {"plan_id": "1"}),
        "get_user": lambda user: {}, "get_plan_by_id": lambda plan: {"plan_id": 1, "host_name": "ALL", "months": 1, "service_type": "xui"},
        "get_user_keys": lambda user: keys, "get_all_hosts": lambda **kwargs: HOSTS,
        "get_next_key_number": lambda user: 1, "time_utils": time_utils, "timedelta": timedelta,
        "asyncio": asyncio, "xui_api": fake, "run_panel_jobs": panel_jobs.run_panel_jobs,
        "_run_async": lambda coro, **kwargs: run_check(coro),
        "update_key_info": (lambda *args, **kwargs: False) if db_fail else fake.update_key_info,
        "update_key_plan_id": lambda *args: True,
        "add_new_key": (lambda *args, **kwargs: None) if db_fail else fake.add_new_key,
        "update_user_stats": lambda *args: stats.append(args),
        "_bot_controller": SimpleNamespace(get_status=lambda: {"is_running": False}, get_bot_instance=lambda: None),
        "flash": lambda msg, category: flashes.append((msg, category)),
        "redirect": lambda url: url, "url_for": lambda name: name,
        "logger": logging.getLogger("check"),
    }
    namespace["get_user"] = lambda user: {"telegram_id": user}
    fn = load_function("src/shop_bot/webhook_server/app.py", "issue_key_route", namespace)
    with patch.object(panel_jobs, "get_host", side_effect=HOST_MAP.get):
        fn(101)
    assert len(fake.calls) == 15, flashes
    assert len({c["target_expiry_ms"] for c in fake.calls}) == 1
    assert all(c["preserve_longer_expiry"] == (not custom and not new) for c in fake.calls)
    if custom:
        assert abs(fake.calls[0]["target_expiry_ms"] - time_utils.get_timestamp_ms(time_utils.get_msk_now() + timedelta(days=7))) < 2000
    elif new:
        assert abs(fake.calls[0]["target_expiry_ms"] - time_utils.get_timestamp_ms(time_utils.get_msk_now() + timedelta(days=30))) < 2000
    else:
        assert fake.calls[0]["target_expiry_ms"] == time_utils.get_timestamp_ms(existing_expiry + timedelta(days=30))
    assert len(stats) == (0 if custom or fail or db_fail else 1)
    assert flashes[-1][1] == ("danger" if db_fail else "warning" if fail else "success"), flashes


def check_loaded_inbound():
    reads = []
    inbound = SimpleNamespace(id=7, protocol="vless", settings=SimpleNamespace(clients=[]))
    namespace = {
        "logger": logging.getLogger("check"), "time_utils": time_utils,
        "_get_inbound_by_id_compat": lambda *args: reads.append(args) or inbound,
        "_get_stream_network_security": lambda inbound: ("tcp", "none"),
        "_protocol_uses_raw_client_api": lambda protocol: False,
        "_build_client_for_inbound": lambda **kwargs: (object(), "fake-uuid"),
        "_add_client_compat": lambda *args: None,
        "_get_raw_client_identifier_by_email": lambda *args: "fake-uuid",
        "timedelta": timedelta,
    }
    fn = load_function("src/shop_bot/modules/xui_api.py", "update_or_create_client_on_panel", namespace)
    target = time_utils.get_timestamp_ms(time_utils.get_msk_now() + timedelta(days=30))
    assert fn(object(), 7, "fake", absolute_expiry_ms=target, loaded_inbound=inbound) == ("fake-uuid", target)
    assert not reads, "issuance repeated the inbound GET"
    assert fn(object(), 7, "fake", absolute_expiry_ms=target) == ("fake-uuid", target)
    assert len(reads) == 1, "other callers stopped reading a fresh inbound"
    assert fn(object(), 8, "fake", absolute_expiry_ms=target, loaded_inbound=inbound) == (None, None)


async def async_checks():
    await check_grouping()
    await check_cancellation()
    await check_worker_lock()
    await check_payment()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    run_check(async_checks())
    for custom in (False, True):
        check_admin(custom=custom)
        check_admin(custom=custom, fail=True)
        check_admin(custom=custom, db_fail=True)
    check_admin(new=True)
    check_admin(new=True, fail=True)
    check_admin(new=True, db_fail=True)
    check_loaded_inbound()
    print("Subscription issuance concurrency, cancellation, expiry and persistence: OK")

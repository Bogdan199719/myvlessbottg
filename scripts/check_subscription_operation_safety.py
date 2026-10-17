#!/usr/bin/env python3
"""Offline regression checks: concurrent trial/manual/repair, no HTTP or real DB."""

import ast
import asyncio
import gc
import logging
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from shop_bot.modules import subscription_operations as operations

TZ = timezone(timedelta(hours=3))
NOW = datetime(2030, 1, 1, tzinfo=TZ)
clock = SimpleNamespace(
    get_msk_now=lambda: NOW,
    from_timestamp_ms=lambda ms: datetime.fromtimestamp(ms / 1000, TZ),
    parse_iso_to_msk=lambda s: datetime.fromisoformat(str(s)) if s else None,
    get_timestamp_ms=lambda dt: int(dt.timestamp() * 1000),
)


def load(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    nodes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *nodes,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)


def check_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "synthetic.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE vpn_keys(key_id INTEGER PRIMARY KEY,user_id INTEGER,host_name TEXT,xui_client_uuid TEXT,key_email TEXT UNIQUE,expiry_date TEXT,created_date TEXT,connection_string TEXT,plan_id INTEGER,service_type TEXT);CREATE TABLE vpn_keys_missing(key_email TEXT);"
        )
        conn.close()
        ns = dict(
            sqlite3=sqlite3,
            DB_FILE=db,
            datetime=datetime,
            logging=logging,
            time_utils=clock,
        )
        load(
            "src/shop_bot/data_manager/database.py",
            {
                "add_new_key",
                "merge_provisioned_key_by_email",
                "get_key_by_email",
                "update_key_info",
            },
            ns,
        )
        short = clock.get_timestamp_ms(NOW + timedelta(days=1))
        long = clock.get_timestamp_ms(NOW + timedelta(days=31))
        key = ns["add_new_key"](
            1, "host", "identity", "email", long, "unchanged-link", 27
        )
        # Exact pre-fix failure: a late add result used to overwrite paid/free
        # monthly access with an older one-day trial and plan_id=0.
        assert (
            ns["add_new_key"](1, "host", "identity", "email", short, "other-link", 0)
            == key
        )
        row = ns["get_key_by_email"]("email")
        assert (
            clock.get_timestamp_ms(datetime.fromisoformat(row["expiry_date"])) == long
        )
        assert (row["plan_id"], row["xui_client_uuid"], row["connection_string"]) == (
            27,
            "identity",
            "unchanged-link",
        )
        # Real renewal remains possible; a conflicting identity is rejected.
        assert ns["merge_provisioned_key_by_email"](
            "email", "host", "identity", long + 86400000, plan_id=0
        )
        assert not ns["merge_provisioned_key_by_email"](
            "email", "host", "different", long, plan_id=0
        )
        assert ns["get_key_by_email"]("email")["xui_client_uuid"] == "identity"
        # Explicit shortening is intentionally supported by the separate path.
        assert ns["update_key_info"](key, clock.from_timestamp_ms(short))
        row = ns["get_key_by_email"]("email")
        assert (
            clock.get_timestamp_ms(datetime.fromisoformat(row["expiry_date"])) == short
        )
        assert row["plan_id"] == 27
        # A normal new trial still has plan_id=0; conversion preserves its ID/link.
        trial = ns["add_new_key"](
            2, "host", "trial-id", "trial", short, "trial-link", 0
        )
        assert ns["merge_provisioned_key_by_email"](
            "trial", "host", "trial-id", long, plan_id=27
        )
        row = ns["get_key_by_email"]("trial")
        assert (
            row["key_id"] == trial
            and row["plan_id"] == 27
            and row["connection_string"] == "trial-link"
        )
        gc.collect()


async def check_coordination():
    order = []
    started, finish = asyncio.Event(), asyncio.Event()

    @operations.serialize_user_operation(lambda uid, label: uid)
    async def issue(uid, label):
        order.append(label + "-start")
        if label == "trial":
            started.set()
            await finish.wait()
        order.append(label + "-end")

    trial = asyncio.create_task(issue(1, "trial"))
    await started.wait()
    manual = asyncio.create_task(issue(1, "manual"))
    await issue(2, "other-user")
    await asyncio.sleep(0.06)
    assert "manual-start" not in order
    assert not operations.try_acquire(1)
    # Caller cancellation cannot expose an unfinished operation to manual work.
    trial.cancel()
    await asyncio.sleep(0.01)
    assert not trial.done() and not operations.try_acquire(1)
    finish.set()
    await asyncio.gather(trial, return_exceptions=True)
    await manual
    assert order.index("trial-end") < order.index("manual-start")
    assert operations.try_acquire(1)
    operations.release(1)

    # Exceptions must release the reservation, allowing recovery/retry.
    @operations.serialize_user_operation(lambda uid: uid)
    async def fail(uid):
        raise ValueError("synthetic failure")

    try:
        await fail(1)
    except ValueError:
        pass
    assert operations.try_acquire(1)
    operations.release(1)
    # The sync worker can outlive cancellation; its write must finish first.
    entered, release = threading.Event(), threading.Event()

    def worker():
        entered.set()
        assert release.wait(2)

    task = asyncio.create_task(operations.run_worker(worker))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    await asyncio.gather(task, return_exceptions=True)


def check_enforcement_snapshot():
    current_ms = clock.get_timestamp_ms(NOW + timedelta(days=31))
    row = dict(
        key_email="email",
        expiry_date=clock.from_timestamp_ms(current_ms).isoformat(),
        xui_client_uuid="identity",
    )
    calls = []
    ns = dict(
        subscription_operations=operations,
        time_utils=clock,
        logger=logging.getLogger(__name__),
        get_keys_for_host=lambda host: [row],
    )

    def sync(host, states):
        calls.append(states)
        return {"checked": len(states), "updated": 0, "errors": 0}

    ns["_sync_clients_state_on_host_sync"] = sync
    load(
        "src/shop_bot/modules/xui_api.py", {"_sync_clients_state_with_user_guards"}, ns
    )
    sync_guarded = ns["_sync_clients_state_with_user_guards"]
    state = dict(
        expiry_timestamp_ms=current_ms - 86400000,
        telegram_id="1",
        client_identifier="identity",
    )
    assert sync_guarded("host", {"email": state})["deferred"] == 1
    assert calls[-1] == {}
    state["expiry_timestamp_ms"] = current_ms
    assert operations.try_acquire(1)
    try:
        assert sync_guarded("host", {"email": state})["deferred"] == 1
    finally:
        operations.release(1)

    assert sync_guarded("host", {"email": state})["checked"] == 1
    assert operations.try_acquire(1)
    operations.release(1)


async def check_auto_provision_refresh():
    row = dict(
        user_id=1,
        host_name="one",
        key_email="one",
        plan_id=27,
        service_type="xui",
        expiry_date=(NOW + timedelta(days=31)).isoformat(),
    )
    two = dict(row, host_name="two", key_email="two")
    db = SimpleNamespace(
        get_all_hosts=lambda enabled: [{"host_name": "one"}, {"host_name": "two"}],
        get_global_plan_ids=lambda *args: {27},
        get_all_keys=lambda: [row],
        get_user_keys=lambda uid: [row, two],
        is_global_xui_key=lambda k, ids: k["plan_id"] in ids,
    )
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return None

    ns = dict(
        asyncio=asyncio,
        database=db,
        time_utils=clock,
        subscription_operations=operations,
        logger=logging.getLogger(__name__),
        _provision_timeout_seconds=lambda: 10,
        _is_host_in_failure_backoff=lambda host: False,
        xui_api=SimpleNamespace(create_or_update_key_on_host_absolute_expiry=create),
        _host_slug=lambda h: h,
    )
    load(
        "src/shop_bot/data_manager/scheduler.py",
        {"auto_provision_new_hosts_for_global_users"},
        ns,
    )
    # Initial bulk snapshot was missing a host; a manual grant has since filled it.
    await ns["auto_provision_new_hosts_for_global_users"]()
    assert not calls
    assert operations.try_acquire(1)
    try:
        await ns["auto_provision_new_hosts_for_global_users"]()
        assert not calls
    finally:
        operations.release(1)

    # A genuinely missing host is still provisioned with the existing expiry
    # and tariff, then persisted. A failed persistence must be reported.
    db.get_user_keys = lambda uid: [row]
    db.get_key_by_email = lambda email: None
    persisted = []
    db.add_new_key = lambda **values: persisted.append(values) or 99

    async def successful_create(**kwargs):
        calls.append(kwargs)
        return dict(
            email="new-email",
            client_uuid="new-identity",
            expiry_timestamp_ms=kwargs["target_expiry_ms"],
            connection_string="new-link",
        )

    ns["xui_api"].create_or_update_key_on_host_absolute_expiry = successful_create
    ns["_mark_host_success"] = lambda host: None
    ns["_mark_host_failure"] = lambda *args: None
    await ns["auto_provision_new_hosts_for_global_users"]()
    assert len(persisted) == 1 and persisted[0]["plan_id"] == 27
    assert persisted[0]["expiry_timestamp_ms"] == clock.get_timestamp_ms(
        NOW + timedelta(days=31)
    )


async def main():
    check_persistence()
    await check_coordination()
    check_enforcement_snapshot()
    await check_auto_provision_refresh()
    print(
        "PASS: stale trial persistence, identity/link preservation, explicit shortening, renewal, per-user concurrency, cancellation, stale enforcement and auto-provision snapshots"
    )


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Exercise real lifecycle functions against an isolated, replacing fake panel.

Only their AST is loaded: no application import, environment, DB, network, or
third-party dependencies. Full inbound writes deliberately raise immediately.
The existing serialization check covers the unchanged production decorators.
"""

import ast
import copy
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

SOURCE = Path(__file__).resolve().parents[1] / "src/shop_bot/modules/xui_api.py"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def client(email, expiry, enabled=True):
    return NS(
        email=email,
        id=f"credential-{email}",
        sub_id=f"sub-{email}",
        expiry_time=expiry,
        enable=enabled,
        flow="xtls-rprx-vision",
        total_gb=0,
        limit_ip=9,
        tg_id="123",
        comment="keep",
    )


class Panel:
    def __init__(self, clients):
        self.clients = {c.email: copy.deepcopy(c) for c in clients}
        self.updates = []
        self.adds = []
        self.resets = []
        self.fail_email = None
        self.missing_update = False

    def inbound(self, *args, **kwargs):
        return NS(
            id=7,
            protocol="vless",
            settings=NS(clients=copy.deepcopy(list(self.clients.values()))),
            client_stats=[],
        )

    def update(self, api, identifier, record):
        if self.missing_update:
            self.missing_update = False
            raise RuntimeError("record not found")
        if record.email == self.fail_email:
            raise RuntimeError("synthetic write failure")
        assert identifier == record.id
        self.updates.append(copy.deepcopy(record))
        self.clients[record.email] = copy.deepcopy(record)

    def add(self, api, inbound_id, record, identifier):
        assert inbound_id == 7 and identifier == record.id
        self.adds.append(copy.deepcopy(record))
        self.clients[record.email] = copy.deepcopy(record)


def load(panel):
    names = {
        "update_or_create_client_on_panel",
        "_sync_clients_state_on_host_sync",
        "_refresh_reactivated_client_visual_state",
    }
    nodes = [
        n
        for n in ast.parse(SOURCE.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name in names
    ]
    for node in nodes:
        node.decorator_list = []

    def forbidden(*args, **kwargs):
        raise AssertionError("unrelated inbound rewrite or visual access toggle")

    env = dict(
        Api=object,
        Client=object,
        Inbound=object,
        uuid=uuid,
        timedelta=timedelta,
        logger=logging.getLogger("xui-client-write-check"),
        get_host=lambda name: dict(
            host_url="https://example.invalid",
            host_username="test",
            host_pass="test",
            host_inbound_id=7,
        ),
        login_to_host=lambda **kw: (object(), panel.inbound()),
        _get_inbound_by_id_compat=panel.inbound,
        _get_stream_network_security=lambda inbound: ("tcp", "reality"),
        _protocol_uses_raw_client_api=lambda protocol: False,
        _set_unlimited_traffic_fields=lambda record: False,
        _ensure_client_identifier_for_protocol=lambda protocol, record, email: record.id,
        _update_client_compat=panel.update,
        _add_client_compat=panel.add,
        _get_raw_client_identifier_by_email=lambda api, ident, proto, email: panel.clients[
            email
        ].id,
        _update_inbound_compat=forbidden,
        _set_client_enabled_state=forbidden,
        _normalize_client_traffic_state=lambda api, ident, email: panel.resets.append(
            email
        ),
        time_utils=NS(
            get_msk_now=lambda: NOW,
            get_timestamp_ms=lambda dt: int(dt.timestamp() * 1000),
            from_timestamp_ms=lambda ms: datetime.fromtimestamp(
                ms / 1000, timezone.utc
            ),
        ),
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), env)
    return env


def main():
    active = client("active@example.invalid", NOW_MS + 100_000)
    other = client("other@example.invalid", NOW_MS + 200_000)
    panel = Panel([active, other])
    env = load(panel)
    issue = env["update_or_create_client_on_panel"]
    before_other = copy.deepcopy(vars(panel.clients[other.email]))
    ident, expiry = issue(object(), 7, active.email, absolute_expiry_ms=NOW_MS + 50_000)
    assert ident == active.id and expiry == active.expiry_time
    assert panel.clients[active.email].sub_id == active.sub_id
    assert vars(panel.clients[other.email]) == before_other
    assert panel.clients[active.email].limit_ip == 9

    # Exact administrative reduction still disables expired access.
    ident, expiry = issue(
        object(),
        7,
        active.email,
        absolute_expiry_ms=NOW_MS - 10_000,
        preserve_longer_expiry=False,
    )
    assert ident == active.id and expiry == NOW_MS - 10_000
    assert panel.clients[active.email].enable is False

    # Renewal applies enable=true once; its visual path never toggles off.
    ident, expiry = issue(
        object(), 7, active.email, absolute_expiry_ms=NOW_MS + 300_000
    )
    assert panel.clients[active.email].enable is True
    assert panel.clients[active.email].sub_id == active.sub_id
    assert panel.resets

    # A failed update of an existing account must not recreate or rotate it.
    panel.missing_update = True
    ident, expiry = issue(
        object(), 7, active.email, absolute_expiry_ms=NOW_MS + 400_000
    )
    assert ident is None and expiry is None and not panel.adds

    # Missing + changed existing in one reconciliation must retain both.
    panel = Panel([active, other])
    env = load(panel)

    def recreate(**kw):
        record = client(kw["email"], kw["absolute_expiry_ms"])
        record.id = kw["client_identifier"]
        panel.clients[record.email] = record
        return record.id, record.expiry_time

    env["update_or_create_client_on_panel"] = recreate
    desired = {
        active.email: dict(enabled=False, expiry_timestamp_ms=NOW_MS - 1_000),
        "missing@example.invalid": dict(
            enabled=True,
            expiry_timestamp_ms=NOW_MS + 500_000,
            recreate_missing=True,
            client_identifier="retained-uuid",
        ),
    }
    result = env["_sync_clients_state_on_host_sync"]("test", desired)
    assert result["recreated"] == 1 and result["updated"] == 2 and not result["errors"]
    assert panel.clients["missing@example.invalid"].id == "retained-uuid"
    assert panel.clients[active.email].enable is False
    assert vars(panel.clients[other.email]) == before_other

    # A failed per-client write is reported, doesn't reset that client, and
    # doesn't prevent another independent client's write.
    panel = Panel([active, other])
    panel.fail_email = active.email
    env = load(panel)
    result = env["_sync_clients_state_on_host_sync"](
        "test",
        {
            active.email: dict(enabled=False, expiry_timestamp_ms=NOW_MS - 1_000),
            other.email: dict(enabled=True, ip_limit=3),
        },
    )
    assert result["errors"] == 1 and result["updated"] == 1
    assert active.email not in panel.resets
    assert panel.clients[active.email].enable is True
    assert panel.clients[other.email].limit_ip == 3
    assert other.email not in panel.resets, "IP-only update must not reset traffic"
    print(
        "XUI client write safety: OK (expiry, identity, isolation, recreation, failure)"
    )


if __name__ == "__main__":
    main()

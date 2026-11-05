"""Network-free checks of access updates and preservation through the real helpers."""

import ast, copy, json, logging, uuid
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import quote

SOURCE = Path(__file__).resolve().parents[1] / "src/shop_bot/modules/xui_api.py"
NAMES = {
    "_safe_int",
    "_get_canonical_client",
    "_canonical_client_update_payload",
    "_update_canonical_client",
    "_update_client_compat",
    "_raw_client_payload_for_clients_api",
    "_client_payload_for_clients_api",
    "_normalize_client_traffic_state",
    "_client_traffic_is_disabled",
}


class Panel:
    def __init__(self):
        self.record = dict(
            id=42,
            email="fixture@example.invalid",
            uuid="keep-uuid",
            password="keep-password",
            auth="keep-auth",
            subId="keep-sub",
            flow="keep-flow",
            security="auto",
            reverse={"tag": "keep"},
            allowedIPs='["10.0.0.2/32"]',
            enable=True,
            expiryTime=20000,
            totalGB=0,
            reset=0,
            limitIp=3,
            tgId=7,
            limitHwid=4,
            group="keep-group",
            comment="keep-comment",
            resetDay=8,
            resetWeekday=2,
            resetMax=9,
            trafficReset="monthly",
            trafficResetDay=5,
            createdAt=100,
            updatedAt=200,
            adTag="keep-ad",
            futureField={"retain": True},
        )
        self.posts = []
        self.resets = 0
        self.traffic_enabled = True
        self.ignore_write = False
        self.drop_field = False

    def request(self, api, method, route, payload=None):
        if "/get/" in route:
            return {"obj": {"client": copy.deepcopy(self.record)}}
        if "/update/" in route:
            self.posts.append(copy.deepcopy(payload))
            assert isinstance(payload.get("id", ""), str)
            assert isinstance(payload.get("allowedIPs"), list)
            if not self.ignore_write:
                for k in (
                    "expiryTime",
                    "enable",
                    "totalGB",
                    "reset",
                    "limitIp",
                    "tgId",
                ):
                    self.record[k] = payload[k]
                if self.drop_field:
                    self.record["group"] = ""
            return {"success": True}
        raise AssertionError("Unexpected API operation: " + route)

    def reset(self, *args):
        self.resets += 1
        self.record["enable"] = True
        self.traffic_enabled = True


def main():
    p = Panel()
    nodes = [
        n
        for n in ast.parse(SOURCE.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name in NAMES
    ]
    env = dict(
        Api=object,
        Client=object,
        json=json,
        uuid=uuid,
        quote=quote,
        requests=NS(get=object(), post=object()),
        logger=logging.getLogger("canonical-check"),
        _raw_api_request=p.request,
        _is_endpoint_not_found_error=lambda e: False,
        _get_client_traffic_compat=lambda api, email: {"enable": p.traffic_enabled},
        _reset_client_traffic_compat=p.reset,
        time_utils=NS(get_msk_now=lambda: None, get_timestamp_ms=lambda dt: 10000),
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), env)
    update = env["_update_canonical_client"]
    normalize = env["_normalize_client_traffic_state"]
    before = copy.deepcopy(p.record)
    # Renewing existing access changes only its term; all labels/credentials survive.
    update(
        None,
        p.record["email"],
        {
            "expiryTime": 30000,
            "id": "untrusted-sdk-id",
            "password": "untrusted",
            "flow": "untrusted",
            "group": "untrusted",
            "limitHwid": 0,
        },
    )
    assert p.record["expiryTime"] == 30000
    assert all(v == p.record[k] for k, v in before.items() if k != "expiryTime")
    payload = p.posts[-1]
    for k in (
        "password",
        "auth",
        "subId",
        "group",
        "limitHwid",
        "flow",
        "resetDay",
        "trafficReset",
        "futureField",
        "reverse",
    ):
        assert payload[k] == before[k]
    assert (
        payload["id"] == before["uuid"] and payload["created_at"] == before["createdAt"]
    )
    count = len(p.posts)
    update(None, p.record["email"], {"expiryTime": 30000})
    assert len(p.posts) == count
    try:
        update(
            None,
            p.record["email"],
            {"expiryTime": 40000},
            expected_identifier="stale-credential",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Stale identity accepted")
    assert len(p.posts) == count
    # HTTP success with unchanged state must not be reported as a successful renewal.
    p.ignore_write = True
    try:
        update(None, p.record["email"], {"expiryTime": 40000})
    except ValueError:
        pass
    else:
        raise AssertionError("Ignored write accepted")
    p.ignore_write = False
    p.drop_field = True
    try:
        update(None, p.record["email"], {"expiryTime": 40000})
    except ValueError:
        pass
    else:
        raise AssertionError("Lost group accepted")
    p.drop_field = False
    # Expired/disabled records must not be auto-enabled by traffic reset.
    p.record.update(enable=False, expiryTime=9000)
    p.traffic_enabled = False
    assert normalize(None, 7, p.record["email"]) is False and p.resets == 0
    p.record.update(enable=True, expiryTime=9000)
    assert normalize(None, 7, p.record["email"]) is False and p.resets == 0
    # Restored active access may clear genuinely stale traffic once, never toggle OFF.
    p.record.update(enable=True, expiryTime=50000)
    assert normalize(None, 7, p.record["email"]) is True and p.resets == 1
    assert normalize(None, 7, p.record["email"]) is True and p.resets == 1
    print(
        "Canonical access safety: PASS (preservation, no-op, ignored write, lost field, expired guard, active recovery)"
    )


if __name__ == "__main__":
    main()

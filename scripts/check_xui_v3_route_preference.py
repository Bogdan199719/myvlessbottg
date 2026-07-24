#!/usr/bin/env python3
"""Verify that XUI client operations prefer v3 routes and retain legacy fallback."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from shop_bot.modules import xui_api  # noqa: E402


class LegacyClientApi:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def add(self, inbound_id, clients):
        self.calls.append(("add", inbound_id, clients))

    def update(self, identifier, client):
        self.calls.append(("update", identifier, client))

    def delete(self, inbound_id, identifier):
        self.calls.append(("delete", inbound_id, identifier))

    def reset_stats(self, inbound_id, email):
        self.calls.append(("reset", inbound_id, email))

    def get_by_email(self, email):
        self.calls.append(("traffic", email))
        return {"email": email, "legacy": True}


def _client(email: str = "route-check@example.test"):
    return SimpleNamespace(
        email=email,
        sub_id="routechecksubid",
        id="11111111-1111-4111-8111-111111111111",
        password=None,
        auth=None,
        flow="",
        total_gb=0,
        expiry_time=1,
        limit_ip=0,
        tg_id=0,
        comment="",
        enable=True,
    )


def _api():
    return SimpleNamespace(client=LegacyClientApi())


def _endpoint(call) -> str:
    return call.args[2]


def main() -> int:
    failures: list[str] = []
    checks = 0

    def expect(condition: bool, message: str) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(message)

    api = _api()
    client = _client()
    raw_client = {
        "email": client.email,
        "auth": "raw-auth",
        "expiryTime": 1,
        "enable": True,
    }

    with patch.object(
        xui_api, "_raw_api_request", return_value={"success": True, "obj": {}}
    ) as request:
        xui_api._add_client_compat(api, 6, client, client.id)
        expect(
            _endpoint(request.call_args_list[-1]) == "panel/api/clients/add",
            "add did not prefer v3",
        )
        xui_api._update_client_compat(api, client.id, client)
        expect(
            _endpoint(request.call_args_list[-1]).startswith(
                "panel/api/clients/update/"
            ),
            "update did not prefer v3",
        )
        xui_api._delete_client_compat(api, 6, client.id, client.email)
        expect(
            _endpoint(request.call_args_list[-1]).startswith("panel/api/clients/del/"),
            "delete did not prefer v3",
        )
        xui_api._reset_client_traffic_compat(api, 6, client.email)
        expect(
            _endpoint(request.call_args_list[-1]).startswith(
                "panel/api/clients/resetTraffic/"
            ),
            "traffic reset did not prefer v3",
        )
        xui_api._get_client_traffic_compat(api, client.email)
        expect(
            _endpoint(request.call_args_list[-1]).startswith(
                "panel/api/clients/traffic/"
            ),
            "traffic read did not prefer v3",
        )
        xui_api._add_raw_client(api, 7, raw_client)
        expect(
            _endpoint(request.call_args_list[-1]) == "panel/api/clients/add",
            "raw add did not prefer v3",
        )
        xui_api._update_raw_client(api, 7, "raw-auth", raw_client)
        expect(
            _endpoint(request.call_args_list[-1]).startswith(
                "panel/api/clients/update/"
            ),
            "raw update did not prefer v3",
        )
        xui_api._delete_raw_client(api, 7, "raw-auth", client.email)
        expect(
            _endpoint(request.call_args_list[-1]).startswith("panel/api/clients/del/"),
            "raw delete did not prefer v3",
        )

    expect(not api.client.calls, "legacy client API was called when v3 succeeded")

    api = _api()
    legacy_endpoints: list[str] = []

    def current_missing(_api, _method, endpoint, _payload=None):
        if endpoint.startswith("panel/api/clients/"):
            raise RuntimeError("404 Not Found")
        legacy_endpoints.append(endpoint)
        return {"success": True, "obj": {}}

    with patch.object(xui_api, "_raw_api_request", side_effect=current_missing):
        xui_api._add_client_compat(api, 6, client, client.id)
        xui_api._update_client_compat(api, client.id, client)
        xui_api._delete_client_compat(api, 6, client.id, client.email)
        xui_api._reset_client_traffic_compat(api, 6, client.email)
        traffic = xui_api._get_client_traffic_compat(api, client.email)
        xui_api._add_raw_client(api, 7, raw_client)
        xui_api._update_raw_client(api, 7, "raw-auth", raw_client)
        xui_api._delete_raw_client(api, 7, "raw-auth", client.email)

    legacy_names = [call[0] for call in api.client.calls]
    expect(
        legacy_names == ["add", "update", "delete", "reset", "traffic"],
        "model-client legacy fallback changed",
    )
    expect(
        traffic.get("legacy") is True,
        "legacy traffic fallback result was not returned",
    )
    expect(
        legacy_endpoints
        == [
            "panel/api/inbounds/addClient",
            "panel/api/inbounds/updateClient/raw-auth",
            "panel/api/inbounds/7/delClient/raw-auth",
        ],
        "raw-client legacy fallback changed",
    )

    api = _api()
    with patch.object(
        xui_api,
        "_raw_api_request",
        side_effect=RuntimeError("403 permission denied"),
    ):
        try:
            xui_api._delete_client_compat(api, 6, client.id, client.email)
        except RuntimeError as exc:
            expect("403" in str(exc), "v3 permission error was altered")
        else:
            expect(False, "v3 permission error incorrectly fell back")
    expect(not api.client.calls, "permission error incorrectly called legacy API")

    running = 0
    max_running = 0
    lock = threading.Lock()

    def build_link(**kwargs):
        nonlocal running, max_running
        with lock:
            running += 1
            max_running = max(max_running, running)
        time.sleep(0.01)
        with lock:
            running -= 1
        return f"vless://{kwargs['client_identifier']}@example.test:443"

    client_rows = [(f"user-{index}", f"id-{index}") for index in range(12)]
    with patch.object(
        xui_api, "_connection_string_for_client", side_effect=build_link
    ):
        links = xui_api._connection_strings_for_client_rows(
            api=SimpleNamespace(),
            inbound=SimpleNamespace(),
            host_url="https://example.test",
            host_name="Riga-test",
            client_rows=client_rows,
        )
    expect(len(links) == len(client_rows), "bounded link fetch lost clients")
    expect(max_running > 1, "link fetch did not run concurrently")
    expect(
        max_running <= xui_api._LINK_FETCH_MAX_WORKERS,
        "link fetch exceeded its worker limit",
    )

    print(f"XUI v3 route preference checks: {checks}")
    if failures:
        print("\nERROR: XUI v3 route preference regression:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("\nOK: v3 routes are preferred and legacy fallback remains available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

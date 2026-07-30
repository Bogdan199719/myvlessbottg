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

    host_url = "https://native-token.example.test"
    native_api = SimpleNamespace()
    target_inbound = SimpleNamespace(id=9)
    xui_api._host_bearer_failure_cache.pop(host_url, None)
    with (
        patch.object(xui_api, "Api", return_value=native_api) as api_class,
        patch.object(xui_api, "_set_api_request_timeouts"),
        patch.object(
            xui_api,
            "_get_inbound_list_compat",
            return_value=[target_inbound],
        ),
    ):
        actual_api, actual_inbound = xui_api.login_to_host(
            host_url,
            "username",
            "password",
            target_inbound.id,
            "native-bearer-token",
        )
    expect(
        api_class.call_args.kwargs.get("token") == "native-bearer-token",
        "Bearer authentication did not use py3xui's native token parameter",
    )
    expect(
        actual_api is native_api and actual_inbound is target_inbound,
        "native Bearer authentication did not return the target inbound",
    )

    legacy_api = SimpleNamespace()
    with (
        patch.object(xui_api, "Api", return_value=legacy_api),
        patch.object(xui_api, "_set_api_request_timeouts"),
        patch.object(xui_api, "_login_with_csrf", return_value=False),
        patch.object(xui_api, "_login_without_csrf", return_value=True) as legacy_login,
        patch.object(
            xui_api,
            "_get_inbound_list_compat",
            return_value=[target_inbound],
        ),
    ):
        actual_api, actual_inbound = xui_api.login_to_host(
            host_url,
            "username",
            "password",
            target_inbound.id,
        )
    expect(
        legacy_login.call_count == 1,
        "pre-CSRF cookie login fallback was not attempted",
    )
    expect(
        actual_api is legacy_api and actual_inbound is target_inbound,
        "pre-CSRF cookie login fallback did not return the target inbound",
    )

    sync_inbound = SimpleNamespace(
        id=11,
        protocol="vless",
        settings=SimpleNamespace(clients=[]),
        client_stats=[],
    )
    missing_state = {
        "enabled": True,
        "expiry_timestamp_ms": 2_000_000_000_000,
        "force_unlimited": True,
        "recreate_missing": True,
        "client_identifier": "11111111-1111-4111-8111-111111111111",
        "telegram_id": "12345",
        "ip_limit": 0,
    }
    with (
        patch.object(
            xui_api,
            "get_host",
            return_value={
                "host_url": host_url,
                "host_username": "username",
                "host_pass": "password",
                "host_inbound_id": sync_inbound.id,
                "api_token": "token",
            },
        ),
        patch.object(
            xui_api,
            "login_to_host",
            return_value=(native_api, sync_inbound),
        ),
        patch.object(
            xui_api,
            "_get_inbound_by_id_compat",
            return_value=sync_inbound,
        ),
        patch.object(
            xui_api,
            "update_or_create_client_on_panel",
            return_value=(
                missing_state["client_identifier"],
                missing_state["expiry_timestamp_ms"],
            ),
        ) as recreate_client,
    ):
        sync_result = xui_api._sync_clients_state_on_host_sync(
            "missing-client-host",
            {"missing@example.test": missing_state},
        )
    expect(
        sync_result["recreated"] == 1
        and sync_result["updated"] == 1
        and sync_result["not_found"] == 0
        and sync_result["errors"] == 0,
        "active panel client was not recreated from DB state",
    )
    expect(
        recreate_client.call_args.kwargs.get("client_identifier")
        == missing_state["client_identifier"],
        "active panel client recreation did not preserve its stored credential",
    )

    expired_state = dict(missing_state, enabled=False, recreate_missing=False)
    with (
        patch.object(
            xui_api,
            "get_host",
            return_value={
                "host_url": host_url,
                "host_username": "username",
                "host_pass": "password",
                "host_inbound_id": sync_inbound.id,
                "api_token": "token",
            },
        ),
        patch.object(
            xui_api,
            "login_to_host",
            return_value=(native_api, sync_inbound),
        ),
        patch.object(
            xui_api,
            "_get_inbound_by_id_compat",
            return_value=sync_inbound,
        ),
        patch.object(xui_api, "update_or_create_client_on_panel") as recreate_expired,
    ):
        sync_result = xui_api._sync_clients_state_on_host_sync(
            "missing-expired-client-host",
            {"expired@example.test": expired_state},
        )
    expect(
        sync_result["recreated"] == 0
        and sync_result["not_found"] == 1
        and not recreate_expired.called,
        "expired missing client was unexpectedly recreated",
    )

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
    with patch.object(xui_api, "_connection_string_for_client", side_effect=build_link):
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

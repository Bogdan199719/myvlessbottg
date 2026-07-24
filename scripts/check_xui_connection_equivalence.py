#!/usr/bin/env python3
"""Validate XUI connection-string equivalence rules used by panel sync."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from shop_bot.modules.xui_api import (
    _get_vless_connection_string,
    _normalize_inbound_payload,
    connection_strings_equivalent,
)


BASE = (
    "vless://11111111-1111-4111-8111-111111111111@example.com:443"
    "?type=tcp&security=reality&pbk=pubkey&fp=chrome&sni=www.nvidia.com"
    "&sid=abc&spx=%2Ffoo&flow=xtls-rprx-vision#old"
)


def main() -> int:
    normalized_null_sniffing = _normalize_inbound_payload({"sniffing": None})
    if normalized_null_sniffing.get("sniffing") != {"enabled": False}:
        print(
            "\nERROR: null inbound sniffing was not normalized to a disabled object."
        )
        return 1

    checks = [
        (
            "volatile 3x-ui sid/spx and remark changes are equivalent",
            BASE,
            BASE.replace("sid=abc", "sid=def")
            .replace("spx=%2Ffoo", "spx=%2Fbar")
            .replace("#old", "#new"),
            True,
        ),
        (
            "explicit encryption=none is equivalent to omitted encryption",
            BASE,
            BASE.replace("?type=tcp", "?type=tcp&encryption=none"),
            True,
        ),
        (
            "uuid changes are not equivalent",
            BASE,
            BASE.replace("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
            False,
        ),
        (
            "public key changes are not equivalent",
            BASE,
            BASE.replace("pbk=pubkey", "pbk=otherkey"),
            False,
        ),
        (
            "transport changes are not equivalent",
            BASE,
            BASE.replace("type=tcp", "type=xhttp"),
            False,
        ),
    ]

    failures = []
    for name, left, right, expected in checks:
        actual = connection_strings_equivalent(left, right)
        if actual != expected:
            failures.append((name, expected, actual))

    print(f"XUI equivalence checks: {len(checks)}")
    if failures:
        print("\nERROR: connection-string equivalence regression:")
        for name, expected, actual in failures:
            print(f" - {name}: expected={expected} actual={actual}")
        return 1

    stream_settings = SimpleNamespace(
        reality_settings={
            "settings": {"publicKey": "pubkey", "fingerprint": "firefox"},
            "serverNames": ["www.example.com"],
            "shortIds": ["abcdef0123456789"],
        },
        _shop_bot_raw_stream_settings={
            "xhttpSettings": {
                "path": "/xhttp-test",
                "mode": "auto",
                "host": "www.example.com",
            }
        },
    )
    inbound = SimpleNamespace(stream_settings=stream_settings, protocol="vless")
    xhttp_link = _get_vless_connection_string(
        inbound,
        "11111111-1111-4111-8111-111111111111",
        "vpn.example.com",
        8443,
        "test",
        "xhttp",
    )
    xhttp_query = parse_qs(urlsplit(xhttp_link).query)
    expected_xhttp = {
        "path": ["/xhttp-test"],
        "mode": ["auto"],
        "host": ["www.example.com"],
    }
    for name, expected in expected_xhttp.items():
        if xhttp_query.get(name) != expected:
            print(
                f"\nERROR: XHTTP link parameter {name!r}: "
                f"expected={expected!r} actual={xhttp_query.get(name)!r}"
            )
            return 1

    print("\nOK: XUI equivalence and XHTTP link generation checks are stable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

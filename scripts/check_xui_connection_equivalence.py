#!/usr/bin/env python3
"""Validate XUI connection-string equivalence rules used by panel sync."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from shop_bot.modules.xui_api import connection_strings_equivalent


BASE = (
    "vless://11111111-1111-4111-8111-111111111111@example.com:443"
    "?type=tcp&security=reality&pbk=pubkey&fp=chrome&sni=www.nvidia.com"
    "&sid=abc&spx=%2Ffoo&flow=xtls-rprx-vision#old"
)


def main() -> int:
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

    print("\nOK: XUI connection-string equivalence rules are stable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

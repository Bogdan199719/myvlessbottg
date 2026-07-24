#!/usr/bin/env python3
"""Check automatic host selection without touching panels or runtime data."""

from __future__ import annotations

from datetime import timedelta

from shop_bot.modules.host_selector import (
    AUTO_SELECTOR_TITLE,
    replace_config_title,
    select_automatic_host,
)
from shop_bot.utils import time_utils


def _health(
    host_name: str,
    *,
    cpu: float,
    memory: float,
    online: int,
    latency: float,
    age_seconds: int = 0,
    available: bool = True,
) -> dict:
    return {
        "host_name": host_name,
        "is_available": int(available),
        "xray_running": int(available),
        "cpu_percent": cpu,
        "memory_percent": memory,
        "active_connections": online,
        "latency_ms": latency,
        "checked_at": (
            time_utils.get_msk_now() - timedelta(seconds=age_seconds)
        ).isoformat(),
    }


def main() -> int:
    configs = {
        "fast": "vless://11111111-1111-1111-1111-111111111111@fast.example:443?security=reality#Fast",
        "busy": "vless://22222222-2222-2222-2222-222222222222@busy.example:443?security=reality#Busy",
        "stale": "vless://33333333-3333-3333-3333-333333333333@stale.example:443?security=reality#Stale",
    }
    health = [
        _health("fast", cpu=20, memory=30, online=10, latency=50),
        _health("busy", cpu=95, memory=40, online=200, latency=20),
        _health(
            "stale",
            cpu=5,
            memory=10,
            online=1,
            latency=10,
            age_seconds=3600,
        ),
    ]

    selected = select_automatic_host(configs, health, "stable-token")
    assert selected is not None
    assert selected["host_name"] == "fast"
    assert selected["eligible_hosts"] == 1
    assert selected["config"].endswith(
        "#%E2%9A%A1%20%D0%90%D0%B2%D1%82%D0%BE%D0%B2%D1%8B%D0%B1%D0%BE%D1%80"
    )

    assert (
        select_automatic_host(
            {"busy": configs["busy"]}, [health[1]], "stable-token"
        )
        is None
    )
    assert replace_config_title("not-a-proxy-url", AUTO_SELECTOR_TITLE) is None

    equal_health = [
        _health("fast", cpu=20, memory=30, online=10, latency=50),
        _health("busy", cpu=20, memory=30, online=10, latency=50),
    ]
    first = select_automatic_host(configs, equal_health, "same-user")
    second = select_automatic_host(configs, equal_health, "same-user")
    assert first and second and first["host_name"] == second["host_name"]
    distributed = {
        select_automatic_host(configs, equal_health, f"user-{number}")["host_name"]
        for number in range(100)
    }
    assert distributed == {"fast", "busy"}

    grouped = select_automatic_host(
        configs,
        equal_health,
        "grouped-user",
        groups_by_host={"fast": "panel-a", "busy": "panel-a"},
    )
    assert grouped and grouped["eligible_hosts"] == 2
    assert grouped["eligible_groups"] == 1

    print("OK: automatic selector is safe, stable and health-aware.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

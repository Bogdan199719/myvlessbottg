"""Pure helpers for the subscription's virtual automatic server."""

from __future__ import annotations

import hashlib
import math
from urllib.parse import quote, urlsplit, urlunsplit

from shop_bot.utils import time_utils

AUTO_SELECTOR_TITLE = "⚡ Автовыбор"


def _bounded_percent(value: object, default: float = 100.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(parsed, 100.0))


def _non_negative(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, parsed)


def replace_config_title(connection_string: str, title: str) -> str | None:
    """Return the same proxy URI with a different display title."""
    try:
        parts = urlsplit(str(connection_string or "").strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parts.query,
            quote(title, safe=""),
        )
    )


def select_automatic_host(
    configs_by_host: dict[str, str],
    health_rows: list[dict],
    subscription_token: str,
    *,
    groups_by_host: dict[str, str] | None = None,
    max_cpu_percent: float = 90.0,
    max_memory_percent: float = 90.0,
    max_age_seconds: int = 900,
) -> dict | None:
    """Choose a healthy host while spreading equal-quality users deterministically.

    Health thresholds remove unsafe candidates. Weighted rendezvous hashing then
    spreads users deterministically across the remaining capacity instead of
    sending everybody to whichever host is momentarily first.
    """
    if not configs_by_host or not subscription_token:
        return None

    now = time_utils.get_msk_now()
    health_by_host = {
        str(row.get("host_name") or ""): row
        for row in health_rows
        if row.get("host_name")
    }
    candidates: list[dict] = []

    for host_name, config in configs_by_host.items():
        health = health_by_host.get(host_name)
        if not health or not health.get("is_available") or not health.get(
            "xray_running"
        ):
            continue

        checked_at = time_utils.parse_iso_to_msk(health.get("checked_at"))
        if not checked_at:
            continue
        age_seconds = (now - checked_at).total_seconds()
        if age_seconds < -60 or age_seconds > max(60, int(max_age_seconds)):
            continue

        cpu = _bounded_percent(health.get("cpu_percent"))
        memory = _bounded_percent(health.get("memory_percent"))
        if cpu >= max_cpu_percent or memory >= max_memory_percent:
            continue

        candidates.append(
            {
                "host_name": host_name,
                "group_key": (groups_by_host or {}).get(host_name, host_name),
                "config": config,
                "cpu_percent": cpu,
                "memory_percent": memory,
                "active_connections": _non_negative(
                    health.get("active_connections")
                ),
                "latency_ms": _non_negative(health.get("latency_ms"), 1000.0),
            }
        )

    if not candidates:
        return None

    max_connections = max(
        1.0, max(row["active_connections"] for row in candidates)
    )
    max_latency = max(1.0, max(row["latency_ms"] for row in candidates))

    for candidate in candidates:
        load_score = (
            0.35 * (candidate["cpu_percent"] / 100.0)
            + 0.25 * (candidate["memory_percent"] / 100.0)
            + 0.25 * (candidate["active_connections"] / max_connections)
            + 0.15 * (candidate["latency_ms"] / max_latency)
        )
        candidate["load_score"] = load_score
        member_digest = hashlib.sha256(
            f"{subscription_token}|member|{candidate['host_name']}".encode("utf-8")
        ).digest()
        candidate["member_rank"] = int.from_bytes(member_digest[:8], "big")

    # Several logical inbounds may point to one physical 3x-ui panel. Keep one
    # deterministic representative per panel so it does not receive extra weight.
    representatives: dict[str, dict] = {}
    for candidate in candidates:
        group_key = candidate["group_key"]
        current = representatives.get(group_key)
        if current is None or candidate["member_rank"] < current["member_rank"]:
            representatives[group_key] = candidate

    for candidate in representatives.values():
        digest = hashlib.sha256(
            f"{subscription_token}|group|{candidate['group_key']}".encode("utf-8")
        ).digest()
        uniform = (int.from_bytes(digest[:8], "big") + 1) / ((1 << 64) + 1)
        capacity_weight = max(0.02, (1.0 - min(load_score, 0.98)) ** 2)
        candidate["weighted_rank"] = -math.log(uniform) / capacity_weight

    selected = min(
        representatives.values(),
        key=lambda row: row["weighted_rank"],
    )
    auto_config = replace_config_title(selected["config"], AUTO_SELECTOR_TITLE)
    if not auto_config:
        return None

    return {
        "host_name": selected["host_name"],
        "config": auto_config,
        "load_score": selected["load_score"],
        "eligible_hosts": len(candidates),
        "eligible_groups": len(representatives),
    }

"""Read-only 3x-ui health collection for automatic host selection."""

from __future__ import annotations

import asyncio
import time

import requests

from shop_bot.modules import xui_api
from shop_bot.utils import time_utils


def _ratio_percent(current: object, total: object) -> float | None:
    try:
        current_value = float(current)
        total_value = float(total)
    except (TypeError, ValueError):
        return None
    if total_value <= 0:
        return None
    return max(0.0, min((current_value / total_value) * 100.0, 100.0))


def collect_host_health_sync(host_name: str) -> dict:
    """Fetch a small read-only status snapshot from one configured panel."""
    host = xui_api.get_host(host_name)
    if not host:
        raise RuntimeError("host is missing from the local database")

    api, inbound = xui_api.login_to_host(
        host_url=host["host_url"],
        username=host["host_username"],
        password=host["host_pass"],
        inbound_id=host["host_inbound_id"],
        api_token=host.get("api_token"),
    )
    if not api or not inbound:
        raise RuntimeError("panel login or inbound lookup failed")

    started = time.monotonic()
    status_payload = xui_api._raw_api_request(
        api, requests.get, "panel/api/server/status"
    )
    latency_ms = (time.monotonic() - started) * 1000.0

    status = status_payload.get("obj") or {}
    xray = status.get("xray") or {}
    memory = status.get("mem") or {}
    net_io = status.get("netIO") or {}
    xray_state = str(xray.get("state") or "")

    return {
        "is_available": True,
        "xray_running": xray_state.strip().lower()
        in {"run", "running", "started", "active"},
        "cpu_percent": float(status.get("cpu") or 0.0),
        "memory_percent": _ratio_percent(
            memory.get("current"), memory.get("total")
        ),
        "network_up_bps": int(net_io.get("up") or 0),
        "network_down_bps": int(net_io.get("down") or 0),
        "active_connections": int(status.get("tcpCount") or 0),
        "tcp_count": int(status.get("tcpCount") or 0),
        "latency_ms": round(latency_ms, 1),
        "checked_at": time_utils.get_msk_now().isoformat(),
        "failure_reason": None,
        "consecutive_failures": 0,
    }


async def collect_host_health(host_name: str) -> dict:
    return await asyncio.to_thread(collect_host_health_sync, host_name)

"""
MTG Proxy Panel API wrapper.

Mirrors the structure of xui_api.py but talks to the MTG AdminPanel REST API
instead of the 3x-ui panel.

Key mapping inside vpn_keys table for MTG rows (service_type='mtg'):
  key_email        → proxy user name in MTG panel  (e.g. "user123key1mtg")
  xui_client_uuid  → node_id as string             (e.g. "3")
  connection_string→ tg://proxy?server=…&port=…&secret=…
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

import aiohttp

from shop_bot.data_manager.database import get_mtg_host

logger = logging.getLogger(__name__)

# ── token cache: {host_name: (token, expires_at_unix)} ──────────────────────
_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_TTL = 3600  # seconds


async def _get_token(host_name: str) -> str | None:
    """Login to MTG panel and return the x-auth-token, with in-process caching."""
    cached = _token_cache.get(host_name)
    if cached:
        token, expires_at = cached
        if time.time() < expires_at:
            return token

    host = get_mtg_host(host_name)
    if not host:
        logger.error(f"MTG host '{host_name}' not found in DB.")
        return None

    url = host["host_url"].rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}/api/login",
                json={"username": host["username"], "password": host["password"]},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(
                        f"MTG login failed for host '{host_name}': {resp.status} {text}"
                    )
                    return None
                data = await resp.json()
                token = data.get("token")
                if not token:
                    logger.error(
                        f"MTG login response missing token for host '{host_name}': {data}"
                    )
                    return None
                _token_cache[host_name] = (token, time.time() + _TOKEN_TTL)
                return token
    except Exception as e:
        logger.error(f"MTG login exception for host '{host_name}': {e}", exc_info=True)
        return None


def _invalidate_token(host_name: str):
    _token_cache.pop(host_name, None)


async def _get_best_node_id(url: str, token: str) -> int | None:
    """Return the node_id of the least-loaded node via GET /api/nodes/best."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{url}/api/nodes/best",
                headers={"x-auth-token": token},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.error(f"MTG get best node failed: {resp.status}")
                    return None
                data = await resp.json()
                node_id = data.get("id")
                return int(node_id) if node_id is not None else None
    except Exception as e:
        logger.error(f"MTG get best node exception: {e}", exc_info=True)
        return None


async def create_proxy_for_user(
    host_name: str, proxy_name: str, days: int
) -> dict | None:
    """
    Create a new MTG proxy user on the best available node.

    Returns:
        {
            "proxy_name": str,        # name used in panel
            "node_id": int,
            "connection_string": str, # tg://proxy?...
            "expiry_timestamp_ms": int,
        }
    or None on failure.
    """
    token = await _get_token(host_name)
    if not token:
        return None

    host = get_mtg_host(host_name)
    url = host["host_url"].rstrip("/")

    node_id = await _get_best_node_id(url, token)
    if node_id is None:
        logger.error(f"MTG: could not get best node for host '{host_name}'")
        return None

    expires_at = (datetime.now(tz=timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}/api/nodes/{node_id}/users",
                headers={"x-auth-token": token},
                json={"name": proxy_name, "expires_at": expires_at},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.error(
                        f"MTG create user failed for host '{host_name}': {resp.status} {text}"
                    )
                    # Token may be stale
                    if resp.status == 401:
                        _invalidate_token(host_name)
                    return None
                data = await resp.json()
    except Exception as e:
        logger.error(
            f"MTG create user exception for host '{host_name}': {e}", exc_info=True
        )
        return None

    link = data.get("link") or _build_proxy_link(url, data)
    if not link:
        logger.error(
            f"MTG create user: no link in response for host '{host_name}': {data}"
        )
        return None

    expiry_ms = _iso_to_ms(data.get("expires_at"))

    return {
        "proxy_name": data["name"],
        "node_id": node_id,
        "connection_string": link,
        "expiry_timestamp_ms": expiry_ms,
    }


async def delete_proxy_for_user(host_name: str, proxy_name: str, node_id: int) -> bool:
    token = await _get_token(host_name)
    if not token:
        return False
    host = get_mtg_host(host_name)
    url = host["host_url"].rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{url}/api/nodes/{node_id}/users/{proxy_name}",
                headers={"x-auth-token": token},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    _invalidate_token(host_name)
                return resp.status in (200, 204)
    except Exception as e:
        logger.error(f"MTG delete user exception: {e}", exc_info=True)
        return False


async def enable_proxy_for_user(host_name: str, proxy_name: str, node_id: int) -> bool:
    return await _toggle_proxy(host_name, proxy_name, node_id, action="start")


async def disable_proxy_for_user(host_name: str, proxy_name: str, node_id: int) -> bool:
    return await _toggle_proxy(host_name, proxy_name, node_id, action="stop")


async def _toggle_proxy(
    host_name: str, proxy_name: str, node_id: int, action: str
) -> bool:
    token = await _get_token(host_name)
    if not token:
        return False
    host = get_mtg_host(host_name)
    url = host["host_url"].rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}/api/nodes/{node_id}/users/{proxy_name}/{action}",
                headers={"x-auth-token": token},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 401:
                    _invalidate_token(host_name)
                return resp.status in (200, 204)
    except Exception as e:
        logger.error(f"MTG {action} proxy exception: {e}", exc_info=True)
        return False


async def renew_proxy_for_user(
    host_name: str,
    proxy_name: str,
    node_id: int,
    days: int,
    current_expiry_ms: int = 0,
) -> int | None:
    """
    Extend proxy subscription by `days` days via POST /renew.
    Panel adds days from current expiry (or now if expired) and auto-resumes suspended users.
    Returns new expiry_timestamp_ms or None on failure.
    current_expiry_ms is unused but kept for API compatibility.
    """
    token = await _get_token(host_name)
    if not token:
        return None
    host = get_mtg_host(host_name)
    url = host["host_url"].rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}/api/nodes/{node_id}/users/{proxy_name}/renew",
                headers={"x-auth-token": token},
                json={"days": days},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    _invalidate_token(host_name)
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.error(f"MTG renew failed: {resp.status} {text}")
                    return None
                data = await resp.json()
                return _iso_to_ms(data.get("expires_at"))
    except Exception as e:
        logger.error(f"MTG renew exception: {e}", exc_info=True)
        return None


async def get_proxy_link(host_name: str, proxy_name: str) -> str | None:
    """Fetch proxy connection link from panel."""
    token = await _get_token(host_name)
    if not token:
        return None
    host = get_mtg_host(host_name)
    url = host["host_url"].rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{url}/api/users/{proxy_name}",
                headers={"x-auth-token": token},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    _invalidate_token(host_name)
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("link")
    except Exception as e:
        logger.error(f"MTG get_proxy_link exception: {e}", exc_info=True)
        return None


# ── helpers ──────────────────────────────────────────────────────────────────


def _iso_to_ms(iso_str: str | None) -> int:
    """Convert ISO 8601 datetime string to milliseconds timestamp. Returns 0 if None."""
    if not iso_str:
        return 0
    try:
        # Handle both "Z" suffix and "+00:00"
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _build_proxy_link(panel_url: str, user_data: dict) -> str | None:
    """Fallback: build tg://proxy link from node host + user port + secret."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(panel_url)
        server = parsed.hostname
        port = user_data.get("port")
        secret = user_data.get("secret")
        if server and port and secret:
            return f"tg://proxy?server={server}&port={port}&secret={secret}"
    except Exception:
        pass
    return None


def make_t_me_proxy_url(tg_proxy_link: str) -> str:
    """Convert tg://proxy?... to https://t.me/proxy?... for use as inline button URL."""
    return tg_proxy_link.replace("tg://proxy?", "https://t.me/proxy?", 1)

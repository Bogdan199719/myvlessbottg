import ipaddress
import os
import re
from typing import Any


# The production compose file publishes the application only on host loopback;
# nginx reaches it through the Docker bridge. Additional reverse proxies (for
# example a CDN) must be added explicitly through TRUSTED_PROXY_CIDRS.
_DEFAULT_TRUSTED_PROXY_CIDRS = "127.0.0.0/8,::1/128,172.16.0.0/12"


def split_ip_allowlist(raw_value: str | None) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[\s,;]+", raw_value or "")
        if item.strip()
    ]


def _parse_ip(
    value: str | None,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _is_trusted_proxy(ip_value: str | None, trusted_proxy_cidrs: str) -> bool:
    proxy_ip = _parse_ip(ip_value)
    if proxy_ip is None:
        return False
    for item in split_ip_allowlist(trusted_proxy_cidrs):
        try:
            if proxy_ip in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def get_client_ip(request: Any, trusted_proxy_cidrs: str | None = None) -> str:
    """Return the client IP without trusting attacker-supplied forwarding headers.

    X-Forwarded-For is considered only when the direct peer is a configured
    reverse proxy. The chain is walked from right to left, as nginx appends the
    real peer to any incoming header via ``$proxy_add_x_forwarded_for``.
    """
    remote_addr = str(request.remote_addr or "").strip()
    remote_ip = _parse_ip(remote_addr)
    if remote_ip is None:
        return "unknown"

    trusted_cidrs = (
        trusted_proxy_cidrs
        if trusted_proxy_cidrs is not None
        else os.getenv("TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED_PROXY_CIDRS)
    )
    if not _is_trusted_proxy(remote_addr, trusted_cidrs):
        return str(remote_ip)

    forwarded_values = [
        item.strip()
        for item in (request.headers.get("X-Forwarded-For") or "").split(",")
        if item.strip()
    ]
    for value in reversed(forwarded_values):
        forwarded_ip = _parse_ip(value)
        if forwarded_ip is None:
            continue
        normalized = str(forwarded_ip)
        if not _is_trusted_proxy(normalized, trusted_cidrs):
            return normalized

    # A malformed/all-proxy chain is not useful for identifying a client. The
    # direct peer is a safe, stable fallback for rate limiting.
    return str(remote_ip)


def is_ip_allowlisted(ip_value: str | None, allowlist_value: str | None) -> bool:
    try:
        client_ip = ipaddress.ip_address((ip_value or "").strip())
    except ValueError:
        return False

    for item in split_ip_allowlist(allowlist_value):
        try:
            if "/" in item:
                if client_ip in ipaddress.ip_network(item, strict=False):
                    return True
            elif client_ip == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False

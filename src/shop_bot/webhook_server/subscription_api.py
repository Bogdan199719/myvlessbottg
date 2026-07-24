import asyncio
import base64
import logging
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from urllib.parse import urlparse
from flask import Blueprint, Response, request, abort, current_app
from werkzeug.exceptions import HTTPException
from shop_bot.data_manager.database import (
    get_user_paid_keys,
    get_user_trial_keys,
    get_user_by_token,
    get_all_hosts,
    get_all_xui_host_health,
    get_missing_keys,
    get_setting,
)
from shop_bot.modules import host_selector, xui_api
from shop_bot.utils.ip_allowlist import get_client_ip, is_ip_allowlisted

logger = logging.getLogger(__name__)

from shop_bot.utils import time_utils

subscription_bp = Blueprint("subscription", __name__)

_XTLS_SYNC_INTERVAL_SECONDS = 300
_last_xtls_sync_by_host: dict[str, float] = {}
_SUBSCRIPTION_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_TRAFFIC_TIMEOUT_SECONDS = 2
_XTLS_SYNC_TIMEOUT_SECONDS = 5
_FALLBACK_TIMEOUT_SECONDS = 5
_INVALID_TOKEN_WINDOW_SECONDS = 60
_INVALID_TOKEN_MAX_PER_WINDOW = 30
_INVALID_TOKEN_LOG_INTERVAL_SECONDS = 60
_INVALID_TOKEN_MAX_TRACKED_IPS = 2048
_invalid_token_hits_by_ip: dict[str, list[float]] = {}
_invalid_token_last_log_by_ip: dict[str, float] = {}
_invalid_token_lock = threading.Lock()


def _run_on_event_loop(coro, timeout_seconds: int, operation: str):
    loop = current_app.config.get("EVENT_LOOP")
    if not loop or not loop.is_running():
        logger.warning(
            f"Subscription: EVENT_LOOP unavailable for {operation}; skipping async operation"
        )
        return None

    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        future.cancel()
        logger.warning(
            f"Subscription: timeout waiting for {operation} after {timeout_seconds}s"
        )
        return None
    except Exception as e:
        logger.error(f"Subscription: {operation} failed: {e}", exc_info=True)
        return None


def _token_prefix(token: str, limit: int = 5) -> str:
    if not token:
        return "empty"
    return f"{token[:limit]}..."


def _client_ip() -> str:
    return get_client_ip(request)


def _record_invalid_token_request(route_name: str, token: str) -> bool:
    """Return True when the caller should be rate limited."""
    now = time.monotonic()
    ip = _client_ip()
    if is_ip_allowlisted(ip, get_setting("admin_ip_allowlist")):
        return False

    should_log = False
    with _invalid_token_lock:
        hits = [
            ts
            for ts in _invalid_token_hits_by_ip.get(ip, [])
            if now - ts < _INVALID_TOKEN_WINDOW_SECONDS
        ]
        hits.append(now)
        _invalid_token_hits_by_ip[ip] = hits

        if len(_invalid_token_hits_by_ip) > _INVALID_TOKEN_MAX_TRACKED_IPS:
            stale_ips = [
                tracked_ip
                for tracked_ip, tracked_hits in _invalid_token_hits_by_ip.items()
                if not tracked_hits
                or now - max(tracked_hits) >= _INVALID_TOKEN_WINDOW_SECONDS
            ]
            if not stale_ips:
                trim_count = max(
                    1, len(_invalid_token_hits_by_ip) - _INVALID_TOKEN_MAX_TRACKED_IPS
                )
                stale_ips = list(_invalid_token_hits_by_ip.keys())[:trim_count]
            for tracked_ip in stale_ips:
                _invalid_token_hits_by_ip.pop(tracked_ip, None)
                _invalid_token_last_log_by_ip.pop(tracked_ip, None)

        last_log = _invalid_token_last_log_by_ip.get(ip, 0.0)
        if now - last_log >= _INVALID_TOKEN_LOG_INTERVAL_SECONDS:
            _invalid_token_last_log_by_ip[ip] = now
            should_log = True
        should_limit = len(hits) > _INVALID_TOKEN_MAX_PER_WINDOW

    if should_log:
        logger.info(
            "%s token not found from ip=%s prefix=%s recent_invalid=%s",
            route_name,
            ip,
            _token_prefix(token),
            len(hits),
        )

    return should_limit


def _bool_setting(key: str, default: bool = False) -> bool:
    raw = get_setting(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _number_setting(key: str, default: float, minimum: float, maximum: float) -> float:
    raw = get_setting(key)
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _sanitize_subscription_line(value: str, max_length: int = 4096) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length]


def _build_subscription_link(domain: str | None, token: str | None) -> str | None:
    domain_value = (domain or "").strip()
    token_value = (token or "").strip()
    if not domain_value or not token_value:
        return None
    if not domain_value.startswith(("http://", "https://")):
        domain_value = f"https://{domain_value}"
    return f"{domain_value.rstrip('/')}/sub/{token_value}"


def _safe_external_url(value: str | None) -> str | None:
    raw_url = str(value or "").strip()
    if re.search(r"[\x00-\x1f\x7f]", raw_url):
        return None
    if raw_url.startswith(("t.me/", "telegram.me/")):
        raw_url = f"https://{raw_url}"
    elif raw_url.startswith("@") and re.fullmatch(
        r"@[A-Za-z0-9_]{5,32}", raw_url
    ):
        raw_url = f"https://t.me/{raw_url[1:]}"
    url = _sanitize_subscription_line(raw_url, max_length=2048)
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _build_telegram_renew_url(bot_username: str | None) -> str | None:
    username = str(bot_username or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    return f"https://t.me/{username}?start=renew"


def _subscription_update_interval_hours(value: str | None) -> int:
    try:
        interval = int(str(value or "").strip())
    except (TypeError, ValueError):
        return 6
    return interval if interval in {1, 3, 6, 12, 24} else 6


def _subscription_expiry(all_keys: list[dict], selected_keys: list[dict]):
    """Return the safest expiry to advertise for the current subscription.

    While configs are available, the earliest selected-host expiry is the
    guaranteed end of the complete bundle. Once no configs remain, preserve
    the most recent XUI expiry so Happ can still show when access ended.
    """

    def _parsed_expiries(keys: list[dict]) -> list:
        expiries = []
        for key in keys:
            if key.get("service_type", "xui") != "xui":
                continue
            expiry = time_utils.parse_iso_to_msk(key.get("expiry_date"))
            if expiry:
                expiries.append(expiry)
        return expiries

    selected_expiries = _parsed_expiries(selected_keys)
    if selected_expiries:
        return min(selected_expiries)

    historical_expiries = _parsed_expiries(all_keys)
    return max(historical_expiries) if historical_expiries else None


@subscription_bp.route("/happ/<token>", methods=["GET"])
def redirect_to_happ(token):
    user = get_user_by_token(token)
    if not user:
        if _record_invalid_token_request("Happ deeplink", token):
            abort(429, "Too many invalid subscription requests")
        abort(404, "Subscription not found")
    if user.get("is_banned"):
        logger.warning(
            "Blocked Happ deeplink for banned subscription (token prefix: %s)",
            _token_prefix(token),
        )
        abort(403, "Subscription is disabled")

    subscription_url = _build_subscription_link(get_setting("domain"), token)
    if not subscription_url:
        logger.error(
            "Failed to build Happ deeplink (token prefix: %s): domain or token missing",
            _token_prefix(token),
        )
        abort(500, "Subscription domain is not configured")

    deeplink_url = f"happ://add/{subscription_url}"
    logger.info("Redirecting to Happ deeplink (token prefix: %s)", _token_prefix(token))
    return Response(status=302, headers={"Location": deeplink_url})


def _call_with_timeout(func, timeout_seconds: int, *args, **kwargs):
    try:
        future = _SUBSCRIPTION_EXECUTOR.submit(func, *args, **kwargs)
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        logger.warning(
            f"Subscription: timeout calling {getattr(func, '__name__', 'callable')} after {timeout_seconds}s"
        )
        return None
    except Exception as e:
        logger.error(
            f"Subscription: error calling {getattr(func, '__name__', 'callable')}: {e}",
            exc_info=True,
        )
        return None


def _maybe_sync_xtls_for_hosts(host_names: set[str]) -> None:
    if not host_names:
        return

    now = time.time()
    to_sync = {
        h
        for h in host_names
        if now - _last_xtls_sync_by_host.get(h, 0) >= _XTLS_SYNC_INTERVAL_SECONDS
    }
    if not to_sync:
        return

    results = _call_with_timeout(
        xui_api.sync_inbounds_xtls_for_hosts, _XTLS_SYNC_TIMEOUT_SECONDS, to_sync
    )
    if results is None:
        return
    for host_name in to_sync:
        _last_xtls_sync_by_host[host_name] = now
    logger.info(
        f"Auto XTLS sync triggered from subscription: hosts={sorted(to_sync)} results={results}"
    )


def _expiry_sort_key(key: dict) -> float:
    try:
        dt = time_utils.parse_iso_to_msk(key.get("expiry_date"))
        return dt.timestamp() if dt else 0.0
    except Exception:
        return 0.0


def _resolve_connection_string(key: dict, allow_fallback_fetch: bool) -> str | None:
    cached_config = (key.get("connection_string") or "").strip()
    if cached_config:
        return cached_config

    host_name = key.get("host_name")
    if not allow_fallback_fetch:
        logger.warning(
            "Subscription key on host %s has no cached connection_string; fallback disabled.",
            host_name,
        )
        return None

    logger.warning(
        "Subscription key on host %s has no cached connection_string, attempting fallback.",
        host_name,
    )
    try:

        async def _fetch_fallback():
            return await asyncio.wait_for(
                xui_api.get_key_details_from_host(key),
                timeout=_FALLBACK_TIMEOUT_SECONDS,
            )

        fallback_config = _run_on_event_loop(
            _fetch_fallback(),
            timeout_seconds=_FALLBACK_TIMEOUT_SECONDS + 2,
            operation=f"fallback config fetch for host '{host_name}'",
        )
        connection_string = (
            (fallback_config or {}).get("connection_string") or ""
        ).strip()
        if connection_string:
            key["connection_string"] = connection_string
            logger.info("Successfully regenerated config for host '%s'", host_name)
            return connection_string
    except Exception as e:
        logger.error(
            "Fallback config regeneration failed for host '%s': %s", host_name, e
        )

    logger.warning("Failed to regenerate config for host '%s'", host_name)
    return None


@subscription_bp.route("/sub/<token>", methods=["GET"])
def get_subscription(token):
    try:
        live_sync_enabled = _bool_setting("subscription_live_sync", default=False)
        live_stats_enabled = _bool_setting("subscription_live_stats", default=False)
        allow_fallback_fetch = _bool_setting(
            "subscription_allow_fallback_host_fetch", default=False
        )
        # Find user by subscription token
        user = get_user_by_token(token)

        if not user:
            if _record_invalid_token_request("Subscription", token):
                abort(429, "Too many invalid subscription requests")
            abort(404, "Subscription not found")
        if user.get("is_banned"):
            logger.warning(
                "Blocked fetch for banned subscription (token prefix: %s)",
                _token_prefix(token),
            )
            abort(403, "Subscription is disabled")

        token_prefix = _token_prefix(token)
        logger.info("Serving subscription (token prefix: %s)", token_prefix)

        user_id = user["telegram_id"]
        keys = get_user_paid_keys(user_id) + get_user_trial_keys(user_id)
        now = time_utils.get_msk_now()

        active_paid_keys = []
        for key in keys:
            try:
                dt = time_utils.parse_iso_to_msk(key.get("expiry_date"))
                if dt and dt > now:

                    active_paid_keys.append(key)
            except Exception:
                continue

        logger.info(
            "Subscription %s has %s total keys; active by date: %s",
            token_prefix,
            len(keys),
            len(active_paid_keys),
        )

        enabled_host_rows = get_all_hosts(only_enabled=True)
        enabled_hosts = {
            h.get("host_name") for h in enabled_host_rows if h.get("host_name")
        }
        selector_groups_by_host = {
            h["host_name"]: str(h.get("host_url") or h["host_name"])
            for h in enabled_host_rows
            if h.get("host_name")
        }
        if live_sync_enabled:
            missing_emails = {m.get("key_email") for m in get_missing_keys()}
            missing_emails.discard(None)
            logger.info(f"Missing emails count: {len(missing_emails)}")
        else:
            missing_emails = set()

        logger.info(f"Enabled hosts: {enabled_hosts}")

        # Keys that are actually usable right now
        available_paid_keys = [
            k
            for k in active_paid_keys
            if k.get("host_name") in enabled_hosts
            and k.get("key_email") not in missing_emails
        ]

        # Never contact XUI panels while serving a subscription. A failed host
        # must not delay access to healthy hosts or trigger a retry stampede
        # when many clients refresh at once. The scheduler reconciles missing
        # global/trial keys in the background with per-host failure backoff.
        existing_hosts = {
            k.get("host_name") for k in available_paid_keys if k.get("host_name")
        }
        missing_hosts = enabled_hosts - existing_hosts
        has_reconcilable_global_access = any(
            k.get("service_type", "xui") == "xui"
            and (
                str(k.get("plan_id") or 0).strip() == "0"
                or "-global-" in str(k.get("key_email") or "").lower()
            )
            for k in active_paid_keys
        )
        if has_reconcilable_global_access and missing_hosts:
            logger.info(
                "Subscription %s is missing hosts %s; returning available "
                "configs immediately while background reconciliation handles them.",
                token_prefix,
                sorted(missing_hosts),
            )

        # Filter out disabled hosts and missing keys
        filtered_keys = []
        for k in active_paid_keys:
            h_name = k.get("host_name")
            k_email = k.get("key_email")
            if h_name not in enabled_hosts:
                logger.warning(
                    "Subscription %s config filtered out: host '%s' is disabled.",
                    token_prefix,
                    h_name,
                )
                continue
            if k_email in missing_emails:
                logger.warning(
                    "Subscription %s config filtered out: host '%s' key is missing.",
                    token_prefix,
                    h_name,
                )
                continue
            filtered_keys.append(k)

        active_paid_keys = filtered_keys
        logger.info(
            "Subscription %s active keys after host/missing filter: %s",
            token_prefix,
            len(active_paid_keys),
        )

        # Group by host_name and preserve all candidates ordered by expiry.
        keys_by_host: dict[str, list[dict]] = {}
        for key in active_paid_keys:
            host_name = key.get("host_name") or ""
            keys_by_host.setdefault(host_name, []).append(key)

        for host_name, host_keys in keys_by_host.items():
            host_keys.sort(key=_expiry_sort_key, reverse=True)
            logger.debug(
                "Subscription host '%s' has %s candidate keys after filtering.",
                host_name,
                len(host_keys),
            )

        logger.info(
            "Subscription %s grouped %s active keys into %s hosts.",
            token_prefix,
            len(active_paid_keys),
            len(keys_by_host),
        )
        if len(active_paid_keys) > len(keys_by_host):
            logger.warning(
                "DEDUP ALERT: %s duplicate host entries require candidate fallback selection.",
                len(active_paid_keys) - len(keys_by_host),
            )

        if live_sync_enabled:
            _maybe_sync_xtls_for_hosts({h for h in keys_by_host.keys() if h})

        configs: list[str] = []
        configs_by_host: dict[str, str] = {}
        selected_keys: list[dict] = []
        seen_configs: set[str] = set()
        for host_name in sorted(keys_by_host.keys()):
            selected_key = None
            selected_config = None
            for candidate in keys_by_host[host_name]:
                candidate_config = _resolve_connection_string(
                    candidate, allow_fallback_fetch
                )
                if candidate_config:
                    selected_key = candidate
                    selected_config = candidate_config
                    break

            if not selected_key or not selected_config:
                logger.warning(
                    "No usable subscription config found for host '%s' after checking %s candidate(s).",
                    host_name,
                    len(keys_by_host[host_name]),
                )
                continue

            if selected_config in seen_configs:
                logger.error(
                    "DUPLICATE CONFIG DETECTED for host '%s'; skipping repeated payload.",
                    host_name,
                )
                continue

            seen_configs.add(selected_config)
            configs.append(selected_config)
            configs_by_host[host_name] = selected_config
            selected_keys.append(selected_key)
            logger.debug(
                "Added subscription config for host '%s'",
                host_name,
            )

        logger.info(
            "Subscription %s final config count: %s", token_prefix, len(configs)
        )

        subscription_lines = list(configs)
        if configs and _bool_setting("auto_selector_enabled", default=False):
            automatic = host_selector.select_automatic_host(
                configs_by_host,
                get_all_xui_host_health(),
                token,
                groups_by_host=selector_groups_by_host,
                max_cpu_percent=_number_setting(
                    "auto_selector_max_cpu_percent", 90.0, 10.0, 100.0
                ),
                max_memory_percent=_number_setting(
                    "auto_selector_max_memory_percent", 90.0, 10.0, 100.0
                ),
                max_age_seconds=int(
                    _number_setting(
                        "auto_selector_health_max_age_seconds",
                        900.0,
                        60.0,
                        3600.0,
                    )
                ),
            )
            if automatic:
                subscription_lines.insert(0, automatic["config"])
                logger.info(
                    "Subscription %s automatic selector chose host '%s' from "
                    "%s eligible logical host(s) on %s panel(s).",
                    token_prefix,
                    automatic["host_name"],
                    automatic["eligible_hosts"],
                    automatic["eligible_groups"],
                )
            else:
                logger.warning(
                    "Subscription %s automatic selector found no fresh healthy host; "
                    "manual configs remain available.",
                    token_prefix,
                )

        if configs and _bool_setting("happ_routing_enabled", default=False):
            happ_routing_rules = _sanitize_subscription_line(
                get_setting("happ_routing_rules") or ""
            )
            if happ_routing_rules.startswith("happ://routing/onadd/"):
                subscription_lines.append(happ_routing_rules)
            elif happ_routing_rules:
                logger.warning(
                    "Happ routing is enabled but happ_routing_rules has an unsupported format."
                )

        # Join with newlines
        subscription_data = "\n".join(subscription_lines)

        # Base64 encode for wide compatibility
        encoded_data = base64.b64encode(subscription_data.encode("utf-8")).decode(
            "utf-8"
        )

        # Calculate traffic stats
        total_up = 0
        total_down = 0
        total_limit = 0
        is_unlimited = False
        stats_samples = 0

        # Gather stats only for keys that are actually present in the final subscription.
        stats_source_keys = selected_keys

        # Gather stats from XUI for active keys
        # Note: This might be slow if many keys.
        # Ideally this should be cached or synced via scheduler.
        # For now, we fetch live to ensure "instant sync" as requested.
        if live_stats_enabled:
            stats_keys = stats_source_keys[:20]
            for key in stats_keys:
                try:
                    stats = _call_with_timeout(
                        xui_api._get_client_traffic_sync, _TRAFFIC_TIMEOUT_SECONDS, key
                    )
                    if stats:
                        stats_samples += 1
                        total_up += stats.get("up", 0)
                        total_down += stats.get("down", 0)
                        limit = stats.get("total", 0)
                        if limit <= 0:
                            is_unlimited = True
                        else:
                            total_limit += limit
                except Exception as e:
                    logger.error(
                        f"Failed to fetch stats for key {key.get('key_id')}: {e}"
                    )

        subscription_name = get_setting("subscription_name") or "AresVPN"
        filename = f"{subscription_name}.txt"

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Profile-Title": subscription_name,
            "Profile-Update-Interval": str(
                _subscription_update_interval_hours(
                    get_setting("subscription_update_interval_hours")
                )
            ),
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        }

        userinfo_parts: list[str] = []
        subscription_expiry = _subscription_expiry(keys, selected_keys)
        if subscription_expiry:
            userinfo_parts.append(f"expire={int(subscription_expiry.timestamp())}")
        if live_stats_enabled and stats_samples:
            userinfo_parts.extend(
                [f"upload={total_up}", f"download={total_down}"]
            )
            if not is_unlimited and total_limit > 0:
                userinfo_parts.append(f"total={total_limit}")
        if userinfo_parts:
            headers["Subscription-Userinfo"] = "; ".join(userinfo_parts)

        support_url = _safe_external_url(get_setting("support_user"))
        if support_url:
            headers["Support-Url"] = support_url

        renew_url = _build_telegram_renew_url(
            get_setting("telegram_bot_username")
        )
        if renew_url:
            headers["Profile-Web-Page-Url"] = renew_url

        announce_text = _sanitize_subscription_line(
            get_setting("subscription_announce") or "", max_length=500
        )
        if announce_text:
            encoded_announce = base64.b64encode(
                announce_text.encode("utf-8")
            ).decode("ascii")
            headers["Announce"] = f"base64:{encoded_announce}"

        return Response(encoded_data, mimetype="text/plain", headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Error serving subscription for token prefix %s: %s",
            _token_prefix(token),
            e,
        )
        return Response("Internal Server Error", status=500)

import asyncio
import base64
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from flask import Blueprint, Response, request, abort, current_app
from werkzeug.exceptions import HTTPException
from shop_bot.data_manager.database import (
    get_user,
    get_user_paid_keys,
    get_user_trial_keys,
    get_all_settings,
    get_user_by_token,
    get_plans_for_host,
    get_all_hosts,
    add_new_key,
    get_missing_keys,
    get_setting,
    get_key_by_email,
    update_key_by_email,
    host_slug as _host_slug,
)
from shop_bot.modules import xui_api

logger = logging.getLogger(__name__)

from shop_bot.utils import time_utils

subscription_bp = Blueprint("subscription", __name__)

_XTLS_SYNC_INTERVAL_SECONDS = 300
_last_xtls_sync_by_host: dict[str, float] = {}
_SUBSCRIPTION_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_TRAFFIC_TIMEOUT_SECONDS = 2
_XTLS_SYNC_TIMEOUT_SECONDS = 5
_FALLBACK_TIMEOUT_SECONDS = 5
_DEFAULT_PROVISION_TIMEOUT_SECONDS = 45


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


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _token_prefix(token: str, limit: int = 5) -> str:
    if not token:
        return "empty"
    return f"{token[:limit]}..."


def _bool_setting(key: str, default: bool = False) -> bool:
    raw = get_setting(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _provision_timeout_seconds() -> int:
    raw = get_setting("provision_timeout_seconds")
    try:
        timeout = int(raw) if raw is not None else _DEFAULT_PROVISION_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = _DEFAULT_PROVISION_TIMEOUT_SECONDS
    return max(10, min(timeout, 180))


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


def _get_global_plan_ids() -> set[int]:
    plan_ids: set[int] = set()
    try:
        for plan in get_plans_for_host("ALL", service_type="xui"):
            plan_id = _safe_int(plan.get("plan_id"), default=0)
            if plan_id > 0:
                plan_ids.add(plan_id)
    except Exception as e:
        logger.error(f"Failed to load global plan ids: {e}")
    return plan_ids


def _is_global_key(key: dict, global_plan_ids: set[int]) -> bool:
    if not global_plan_ids:
        return False
    return _safe_int(key.get("plan_id"), default=0) in global_plan_ids


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
    key_email = key.get("key_email")
    if not allow_fallback_fetch:
        logger.warning(
            "Key %s on host %s has no cached connection_string; fallback disabled.",
            key_email,
            host_name,
        )
        return None

    logger.warning(
        "Key %s on host %s has no cached connection_string, attempting fallback.",
        key_email,
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
            operation=f"fallback config fetch for key '{key_email}'",
        )
        connection_string = (
            (fallback_config or {}).get("connection_string") or ""
        ).strip()
        if connection_string:
            key["connection_string"] = connection_string
            logger.info("Successfully regenerated config for key %s", key_email)
            return connection_string
    except Exception as e:
        logger.error("Fallback config regeneration failed for %s: %s", key_email, e)

    logger.warning(
        "Failed to regenerate config for key %s on host %s", key_email, host_name
    )
    return None


@subscription_bp.route("/sub/<token>", methods=["GET"])
def get_subscription(token):
    try:
        live_sync_enabled = _bool_setting("subscription_live_sync", default=False)
        live_stats_enabled = _bool_setting("subscription_live_stats", default=False)
        allow_fallback_fetch = _bool_setting(
            "subscription_allow_fallback_host_fetch", default=False
        )
        auto_provision_enabled = _bool_setting(
            "subscription_auto_provision", default=False
        )

        # Find user by subscription token
        user = get_user_by_token(token)

        if not user:
            logger.info(
                f"Subscription token not found (prefix: {_token_prefix(token)})"
            )
            abort(404, "Subscription not found")

        logger.info(
            f"Serving subscription for user {user['telegram_id']} (token prefix: {_token_prefix(token)})"
        )

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
            f"User {user_id} has {len(keys)} total paid keys. Active keys (by date): {len(active_paid_keys)}"
        )

        enabled_hosts = {
            h.get("host_name")
            for h in get_all_hosts(only_enabled=True)
            if h.get("host_name")
        }
        if live_sync_enabled:
            missing_emails = {m.get("key_email") for m in get_missing_keys()}
            missing_emails.discard(None)
            logger.info(f"Missing emails count: {len(missing_emails)}")
        else:
            missing_emails = set()

        logger.info(f"Enabled hosts: {enabled_hosts}")

        # Determine global plan ids to support global subscription behavior
        global_plan_ids = _get_global_plan_ids()

        # Keys that are actually usable right now
        available_paid_keys = [
            k
            for k in active_paid_keys
            if k.get("host_name") in enabled_hosts
            and k.get("key_email") not in missing_emails
        ]

        provision_timeout = _provision_timeout_seconds()

        # Auto-provision missing hosts for active global subscriptions
        active_global_keys = [
            k for k in active_paid_keys if _is_global_key(k, global_plan_ids)
        ]

        if active_global_keys and global_plan_ids and auto_provision_enabled:
            # Deterministic plan selection for stable writes across workers/restarts.
            first_global_plan_id = int(min(global_plan_ids))
            # Target expiry based on the soonest-expiring global key
            try:
                global_expiries = []
                for key in active_global_keys:
                    parsed = time_utils.parse_iso_to_msk(key.get("expiry_date"))
                    if parsed:
                        global_expiries.append(parsed)
                min_expiry_dt = min(global_expiries) if global_expiries else None
                remaining_seconds = (
                    int((min_expiry_dt - now).total_seconds()) if min_expiry_dt else 0
                )
            except Exception:
                min_expiry_dt = None
                remaining_seconds = 0

            if remaining_seconds > 0 and min_expiry_dt:
                target_expiry_ms = time_utils.get_timestamp_ms(min_expiry_dt)
                existing_hosts = {k.get("host_name") for k in available_paid_keys}
                logger.info(
                    f"Global subscription detected. Existing hosts: {existing_hosts}. Remaining seconds: {remaining_seconds}"
                )

                for host in get_all_hosts(only_enabled=True):
                    host_name = host.get("host_name")
                    if not host_name or host_name == "ALL":
                        logger.debug(
                            f"Skipping host '{host_name}' (not a regular host)"
                        )
                        continue
                    if host_name in existing_hosts:
                        logger.debug(f"Host '{host_name}' already has a key")
                        continue

                    email = f"user{user_id}-global-{_host_slug(host_name)}"
                    logger.info(
                        f"Auto-provisioning key for host '{host_name}' with email '{email}'"
                    )

                    # Run async provisioning via the shared bot event loop
                    # (same pattern as app.py routes — avoids creating a new loop per request).
                    async def _provision():
                        return await asyncio.wait_for(
                            xui_api.create_or_update_key_on_host_absolute_expiry(
                                host_name=host_name,
                                email=email,
                                target_expiry_ms=target_expiry_ms,
                                telegram_id=str(user_id),
                            ),
                            timeout=provision_timeout,
                        )

                    res = _run_on_event_loop(
                        _provision(),
                        timeout_seconds=provision_timeout + 5,
                        operation=f"global auto-provision for host '{host_name}'",
                    )
                    if res:
                        try:
                            existing_key = get_key_by_email(res["email"])
                            if existing_key:
                                update_key_by_email(
                                    key_email=res["email"],
                                    host_name=host_name,
                                    xui_client_uuid=res["client_uuid"],
                                    expiry_timestamp_ms=res["expiry_timestamp_ms"],
                                    connection_string=res.get("connection_string"),
                                    plan_id=first_global_plan_id,
                                )
                            else:
                                add_new_key(
                                    user_id=user_id,
                                    host_name=host_name,
                                    xui_client_uuid=res["client_uuid"],
                                    key_email=res["email"],
                                    expiry_timestamp_ms=res["expiry_timestamp_ms"],
                                    connection_string=res.get("connection_string"),
                                    plan_id=first_global_plan_id,
                                )
                            # Update local cache so that newly created keys appear in the same response
                            new_key = {
                                "host_name": host_name,
                                "key_email": res["email"],
                                "expiry_date": time_utils.from_timestamp_ms(
                                    res["expiry_timestamp_ms"]
                                ).isoformat(),
                                "connection_string": res.get("connection_string"),
                                "plan_id": first_global_plan_id,
                            }
                            active_paid_keys.append(new_key)
                            available_paid_keys.append(new_key)
                            logger.info(
                                f"Successfully added global key for host '{host_name}'"
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to persist new global key for host {host_name}: {e}"
                            )
                    else:
                        logger.error(f"Failed to create key on host '{host_name}'")
        else:
            if active_global_keys and global_plan_ids and not auto_provision_enabled:
                logger.debug(
                    "Global auto-provision disabled (subscription_auto_provision=false)."
                )
            if active_global_keys and not global_plan_ids:
                logger.debug(
                    "Active global keys found but no global plan IDs configured"
                )
            if global_plan_ids and not active_global_keys:
                logger.debug("Global plan IDs found but no active global keys")

        # Filter out disabled hosts and missing keys
        filtered_keys = []
        for k in active_paid_keys:
            h_name = k.get("host_name")
            k_email = k.get("key_email")
            if h_name not in enabled_hosts:
                logger.warning(
                    f"Key {k_email} filtered out: Host '{h_name}' is not in enabled_hosts."
                )
                continue
            if k_email in missing_emails:
                logger.warning(
                    f"Key {k_email} filtered out: Email is in missing_emails."
                )
                continue
            filtered_keys.append(k)

        active_paid_keys = filtered_keys
        logger.info(
            f"User {user_id}: Active keys after host/missing filter: {len(active_paid_keys)}"
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
            "User %s: grouped %s active keys into %s hosts.",
            user_id,
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
            selected_keys.append(selected_key)
            logger.debug(
                "Added config for %s on host '%s'",
                selected_key.get("key_email"),
                host_name,
            )

        logger.info(f"User {user_id}: Final config count: {len(configs)}")

        # Join with newlines
        subscription_data = "\n".join(configs)

        # Base64 encode for wide compatibility
        encoded_data = base64.b64encode(subscription_data.encode("utf-8")).decode(
            "utf-8"
        )

        # Calculate traffic stats
        total_up = 0
        total_down = 0
        total_limit = 0
        is_unlimited = False

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

        # If any key is unlimited, the subscription is unlimited
        if is_unlimited or (total_limit == 0 and len(stats_source_keys) > 0):
            final_total = 1024**5  # 1 PB
        else:
            final_total = total_limit

        subscription_name = get_setting("subscription_name") or "AresVPN"
        filename = f"{subscription_name}.txt"

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Profile-Title": subscription_name,
            "Profile-Update-Interval": "12",
            "Subscription-Userinfo": f"upload={total_up}; download={total_down}; total={final_total}; expire=0",
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        }

        return Response(encoded_data, mimetype="text/plain", headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving subscription for token {token}: {e}")
        return Response("Internal Server Error", status=500)

import asyncio
import base64
import json
import logging
import time
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from flask import Blueprint, Response, request, abort
from werkzeug.exceptions import HTTPException
from shop_bot.data_manager.database import (
    get_user, get_user_paid_keys, get_all_settings,
    get_user_by_token, get_plans_for_host, get_all_hosts, add_new_key,
    get_missing_keys, get_setting, get_key_by_email, update_key_by_email, get_next_key_number
)
from shop_bot.modules import xui_api

logger = logging.getLogger(__name__)

from shop_bot.utils import time_utils


subscription_bp = Blueprint('subscription', __name__)

_XTLS_SYNC_INTERVAL_SECONDS = 300
_last_xtls_sync_by_host: dict[str, float] = {}
_SUBSCRIPTION_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_TRAFFIC_TIMEOUT_SECONDS = 2
_XTLS_SYNC_TIMEOUT_SECONDS = 5
_FALLBACK_TIMEOUT_SECONDS = 5
_PROVISION_TIMEOUT_SECONDS = 10

def _host_slug(host_name: str) -> str:
    return (host_name or "").replace(" ", "").lower()

def _pick_global_key_number(user_id: int, active_paid_keys: list[dict]) -> int:
    """
    Pick a stable key number for global auto-provisioning.
    Prefer existing user key numbers; fallback to next available.
    """
    pattern = re.compile(rf"^user{int(user_id)}-key(\d+)-", re.IGNORECASE)
    numbers: list[int] = []

    for key in active_paid_keys:
        email = str(key.get("key_email") or "")
        match = pattern.match(email)
        if not match:
            continue
        try:
            numbers.append(int(match.group(1)))
        except Exception:
            continue

    if numbers:
        # Keep continuity with already issued keys for this subscription.
        return max(set(numbers), key=numbers.count)

    try:
        return int(get_next_key_number(int(user_id)))
    except Exception:
        return 1

def _bool_setting(key: str, default: bool = False) -> bool:
    raw = get_setting(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}

def _call_with_timeout(func, timeout_seconds: int, *args, **kwargs):
    try:
        future = _SUBSCRIPTION_EXECUTOR.submit(func, *args, **kwargs)
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        logger.warning(f"Subscription: timeout calling {getattr(func, '__name__', 'callable')} after {timeout_seconds}s")
        return None
    except Exception as e:
        logger.error(f"Subscription: error calling {getattr(func, '__name__', 'callable')}: {e}", exc_info=True)
        return None

def _maybe_sync_xtls_for_hosts(host_names: set[str]) -> None:
    if not host_names:
        return

    now = time.time()
    to_sync = {h for h in host_names if now - _last_xtls_sync_by_host.get(h, 0) >= _XTLS_SYNC_INTERVAL_SECONDS}
    if not to_sync:
        return

    results = _call_with_timeout(xui_api.sync_inbounds_xtls_for_hosts, _XTLS_SYNC_TIMEOUT_SECONDS, to_sync)
    if results is None:
        return
    for host_name in to_sync:
        _last_xtls_sync_by_host[host_name] = now
    logger.info(f"Auto XTLS sync triggered from subscription: hosts={sorted(to_sync)} results={results}")

@subscription_bp.route('/sub/<token>', methods=['GET'])
def get_subscription(token):
    try:
        live_sync_enabled = _bool_setting("subscription_live_sync", default=False)
        live_stats_enabled = _bool_setting("subscription_live_stats", default=False)
        allow_fallback_fetch = _bool_setting("subscription_allow_fallback_host_fetch", default=False)
        auto_provision_enabled = _bool_setting("subscription_auto_provision", default=False)

        # Find user by subscription token
        user = get_user_by_token(token)
        
        if not user:
            logger.warning(f"Subscription token not found: {token}")
            abort(404, "Subscription not found")

        logger.info(f"Serving subscription for user {user['telegram_id']} (token prefix: {token[:5]}...)")

        user_id = user['telegram_id']
        keys = get_user_paid_keys(user_id)
        now = time_utils.get_msk_now()

        active_paid_keys = []
        for key in keys:
            try:
                dt = time_utils.parse_iso_to_msk(key.get('expiry_date'))
                if dt and dt > now:

                    active_paid_keys.append(key)
            except Exception:
                continue
        
        logger.info(f"User {user_id} has {len(keys)} total paid keys. Active keys (by date): {len(active_paid_keys)}")

        enabled_hosts = {h.get('host_name') for h in get_all_hosts(only_enabled=True) if h.get('host_name')}
        if live_sync_enabled:
            missing_emails = {m.get('key_email') for m in get_missing_keys()}
            missing_emails.discard(None)
            logger.info(f"Missing emails count: {len(missing_emails)}")
        else:
            missing_emails = set()

        logger.info(f"Enabled hosts: {enabled_hosts}")

        # Determine global plan ids to support global subscription behavior
        try:
            global_plan_ids = {
                int(p['plan_id'])
                for p in get_plans_for_host('ALL')
                if p.get('plan_id') is not None
            }
        except Exception:
            global_plan_ids = set()

        # Keys that are actually usable right now
        available_paid_keys = [
            k for k in active_paid_keys
            if k.get('host_name') in enabled_hosts
            and k.get('key_email') not in missing_emails
        ]

        # Auto-provision missing hosts for active global subscriptions
        active_global_keys = [
            k for k in active_paid_keys
            if global_plan_ids and k.get('plan_id') and int(k['plan_id']) in global_plan_ids
        ]

        # Fallback heuristic for legacy users whose plan_id не выставлен:
        # если у пользователя 2+ активных платных ключа и есть глобальные тарифы в панели,
        # считаем, что это глобальная подписка и используем эти ключи как источник срока.
        if not active_global_keys and global_plan_ids and len(active_paid_keys) >= 2:
            active_global_keys = active_paid_keys

        if active_global_keys and global_plan_ids and auto_provision_enabled:
            first_global_plan_id = int(next(iter(global_plan_ids)))
            key_number = _pick_global_key_number(user_id, active_paid_keys)
            # Remaining validity based on the soonest-expiring global key
            try:
                remaining_seconds = int(
                    (min(time_utils.parse_iso_to_msk(k['expiry_date']) for k in active_global_keys if time_utils.parse_iso_to_msk(k.get('expiry_date'))) - now).total_seconds()

                )
            except Exception:
                remaining_seconds = 0

            if remaining_seconds > 0:
                existing_hosts = {k.get('host_name') for k in available_paid_keys}
                logger.info(f"Global subscription detected. Existing hosts: {existing_hosts}. Remaining seconds: {remaining_seconds}")
                
                for host in get_all_hosts(only_enabled=True):
                    host_name = host.get('host_name')
                    if not host_name or host_name == 'ALL':
                        logger.debug(f"Skipping host '{host_name}' (not a regular host)")
                        continue
                    if host_name in existing_hosts:
                        logger.debug(f"Host '{host_name}' already has a key")
                        continue

                    email = f"user{user_id}-key{key_number}-{_host_slug(host_name)}"
                    logger.info(f"Auto-provisioning key for host '{host_name}' with email '{email}'")
                    
                    # Run async helper in a fresh loop (Flask view is sync)
                    async def _provision():
                        return await asyncio.wait_for(
                            xui_api.create_or_update_key_on_host_seconds(
                                host_name=host_name,
                                email=email,
                                seconds_to_add=remaining_seconds,
                                telegram_id=str(user_id)
                            ),
                            timeout=_PROVISION_TIMEOUT_SECONDS
                        )
                    try:
                        res = asyncio.run(_provision())
                    except Exception as e:
                        logger.error(f"Provisioning failed for host '{host_name}': {e}")
                        res = None
                    if res:
                        try:
                            existing_key = get_key_by_email(res['email'])
                            if existing_key:
                                update_key_by_email(
                                    key_email=res['email'],
                                    host_name=host_name,
                                    xui_client_uuid=res['client_uuid'],
                                    expiry_timestamp_ms=res['expiry_timestamp_ms'],
                                    connection_string=res.get('connection_string'),
                                    plan_id=first_global_plan_id
                                )
                            else:
                                add_new_key(
                                    user_id=user_id,
                                    host_name=host_name,
                                    xui_client_uuid=res['client_uuid'],
                                    key_email=res['email'],
                                    expiry_timestamp_ms=res['expiry_timestamp_ms'],
                                    connection_string=res.get('connection_string'),
                                    plan_id=first_global_plan_id
                                )
                            # Update local cache so that newly created keys appear in the same response
                            new_key = {
                                'host_name': host_name,
                                'key_email': res['email'],
                                'expiry_date': datetime.fromtimestamp(res['expiry_timestamp_ms'] / 1000).isoformat(),
                                'connection_string': res.get('connection_string'),
                                'plan_id': first_global_plan_id,
                            }
                            active_paid_keys.append(new_key)
                            available_paid_keys.append(new_key)
                            logger.info(f"Successfully added global key for host '{host_name}'")
                        except Exception as e:
                            logger.error(f"Failed to persist new global key for host {host_name}: {e}")
                    else:
                        logger.error(f"Failed to create key on host '{host_name}'")
        else:
            if active_global_keys and global_plan_ids and not auto_provision_enabled:
                logger.debug("Global auto-provision disabled (subscription_auto_provision=false).")
            if active_global_keys:
                logger.debug("Active global keys found but no global plan IDs configured")
            if global_plan_ids:
                logger.debug("Global plan IDs found but no active global keys")

        # Filter out disabled hosts and missing keys
        filtered_keys = []
        for k in active_paid_keys:
            h_name = k.get('host_name')
            k_email = k.get('key_email')
            if h_name not in enabled_hosts:
                logger.warning(f"Key {k_email} filtered out: Host '{h_name}' is not in enabled_hosts.")
                continue
            if k_email in missing_emails:
                logger.warning(f"Key {k_email} filtered out: Email is in missing_emails.")
                continue
            filtered_keys.append(k)
        
        active_paid_keys = filtered_keys
        logger.info(f"User {user_id}: Active keys after host/missing filter: {len(active_paid_keys)}")

        # Deduplicate by host_name: keep the key with the latest expiry per host
        keys_by_host = {}
        for key in active_paid_keys:
            host_name = key.get('host_name') or ''
            prev = keys_by_host.get(host_name)
            if not prev:
                keys_by_host[host_name] = key
                logger.debug(f"Added first key for host '{host_name}': {key.get('key_email')}")
                continue

            try:
                prev_expiry = time_utils.parse_iso_to_msk(prev.get('expiry_date'))
                cur_expiry = time_utils.parse_iso_to_msk(key.get('expiry_date'))

                if cur_expiry > prev_expiry:
                    logger.warning(f"Dedup: Replacing key {prev.get('key_email')} with newer key {key.get('key_email')} for host '{host_name}' (same host)")
                    keys_by_host[host_name] = key
                else:
                    logger.debug(f"Dedup: Keeping key {prev.get('key_email')} for host '{host_name}' (newer)")
            except Exception as e:
                logger.error(f"Error comparing expiry dates: {e}")
                continue
        
        logger.info(f"User {user_id}: After deduplication: {len(keys_by_host)} hosts with 1 key each. Total keys before dedup: {len(active_paid_keys)}")
        if len(active_paid_keys) > len(keys_by_host):
            logger.warning(f"DEDUP ALERT: {len(active_paid_keys) - len(keys_by_host)} duplicate keys were removed!")
        
        if live_sync_enabled:
            _maybe_sync_xtls_for_hosts({h for h in keys_by_host.keys() if h})

        configs = []
        for host_name in sorted(keys_by_host.keys()):
            key = keys_by_host[host_name]
            # If we have connection_string in DB (new keys), use it.
            # Otherwise we might need to fetch it (slow) or just skip it if it's legacy without cache.
            # Or better, we try to reconstruct it if missing, but we lack server keys.
            # So we rely on connection_string being present.
            if key.get('connection_string'):
                config = key['connection_string']
                configs.append(config)
                logger.debug(f"Added config for {key.get('key_email')} on host '{host_name}'")
            else:
                if not allow_fallback_fetch:
                    logger.warning(
                        f"Key {key.get('key_email')} on host {host_name} has NO connection_string; "
                        "fallback disabled, skipping."
                    )
                    continue
                logger.warning(f"Key {key.get('key_email')} on host {host_name} has NO connection_string, attempting fallback...")
                try:
                    async def _fetch_fallback():
                        return await asyncio.wait_for(
                            xui_api.get_key_details_from_host(key),
                            timeout=_FALLBACK_TIMEOUT_SECONDS
                        )
                    fallback_config = asyncio.run(_fetch_fallback())
                    if fallback_config and fallback_config.get('connection_string'):
                        config = fallback_config['connection_string']
                        configs.append(config)
                        logger.info(f"Successfully regenerated config for key {key.get('key_email')}")
                    else:
                        logger.warning(f"Failed to regenerate config for key {key.get('key_email')} on host {host_name}")
                except Exception as e:
                    logger.error(f"Fallback config regeneration failed for {key.get('key_email')}: {e}")
        
        logger.info(f"User {user_id}: Final config count: {len(configs)}")
        
        # Double-check: ensure no duplicate configs in the final list
        unique_configs = []
        seen_configs = set()
        for config in configs:
            config_hash = hash(config)
            if config_hash in seen_configs:
                logger.error(f"DUPLICATE CONFIG DETECTED! Config already exists in subscription. This should not happen!")
                # Skip this duplicate
                continue
            seen_configs.add(config_hash)
            unique_configs.append(config)
        
        if len(unique_configs) < len(configs):
            logger.error(f"REMOVED {len(configs) - len(unique_configs)} DUPLICATE CONFIGS from subscription!")
        
        # Join with newlines
        subscription_data = "\n".join(unique_configs)
        
        # Base64 encode for wide compatibility
        encoded_data = base64.b64encode(subscription_data.encode('utf-8')).decode('utf-8')
        
        # Calculate traffic stats
        total_up = 0
        total_down = 0
        total_limit = 0
        is_unlimited = False

        # Gather stats from XUI for active keys
        # Note: This might be slow if many keys. 
        # Ideally this should be cached or synced via scheduler.
        # For now, we fetch live to ensure "instant sync" as requested.
        if live_stats_enabled:
            stats_keys = active_paid_keys[:20]
            for key in stats_keys:
                try:
                    stats = _call_with_timeout(
                        xui_api._get_client_traffic_sync,
                        _TRAFFIC_TIMEOUT_SECONDS,
                        key
                    )
                    if stats:
                        total_up += stats.get('up', 0)
                        total_down += stats.get('down', 0)
                        limit = stats.get('total', 0)
                        if limit <= 0:
                            is_unlimited = True
                        else:
                            total_limit += limit
                except Exception as e:
                    logger.error(f"Failed to fetch stats for key {key.get('key_id')}: {e}")
        
        # If any key is unlimited, the subscription is unlimited
        if is_unlimited or (total_limit == 0 and len(active_paid_keys) > 0):
            final_total = 1024**5 # 1 PB
        else:
            final_total = total_limit

        subscription_name = get_setting("subscription_name") or "AresVPN"
        filename = f"{subscription_name}.txt"
        
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Profile-Title': subscription_name,
            'Profile-Update-Interval': '12',
            'Subscription-Userinfo': f'upload={total_up}; download={total_down}; total={final_total}; expire=0',
        }

        return Response(encoded_data, mimetype='text/plain', headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving subscription for token {token}: {e}")
        return Response("Internal Server Error", status=500)

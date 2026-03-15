import asyncio
import logging
import math
import time

from datetime import datetime, timedelta
from shop_bot.utils import time_utils

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import Bot

from shop_bot.bot_controller import BotController
from shop_bot.data_manager import database
from shop_bot.modules import xui_api
from shop_bot.bot import keyboards

CHECK_INTERVAL_SECONDS = 60
PAID_NOTIFY_HOURS = {24, 1, 0}
TRIAL_NOTIFY_HOURS = {1, 0}

_DEFAULT_PROVISION_TIMEOUT_SECONDS = 45


logger = logging.getLogger(__name__)

def _bool_setting(key: str, default: bool = False) -> bool:
    raw = database.get_setting(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}

def _provision_timeout_seconds() -> int:
    raw = database.get_setting("provision_timeout_seconds")
    try:
        timeout = int(raw) if raw is not None else _DEFAULT_PROVISION_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = _DEFAULT_PROVISION_TIMEOUT_SECONDS
    return max(10, min(timeout, 180))

def format_time_left(hours: int) -> str:
    if hours >= 24:
        days = hours // 24
        if days % 10 == 1 and days % 100 != 11:
            return f"{days} день"
        elif 2 <= days % 10 <= 4 and (days % 100 < 10 or days % 100 >= 20):
            return f"{days} дня"
        else:
            return f"{days} дней"
    else:
        if hours % 10 == 1 and hours % 100 != 11:
            return f"{hours} час"
        elif 2 <= hours % 10 <= 4 and (hours % 100 < 10 or hours % 100 >= 20):
            return f"{hours} часа"
        else:
            return f"{hours} часов"

async def send_subscription_notification(
    bot: Bot, user_id: int, key_id: int, time_left_hours: int, expiry_date: datetime, is_trial: bool = False
) -> bool:
    try:
        expiry_str = expiry_date.strftime('%d.%m.%Y в %H:%M')
        
        if time_left_hours > 0:
            time_text = format_time_left(time_left_hours)
            message = (
                f"⚠️ **Внимание!** ⚠️\n\n"
                f"Срок действия вашей подписки истекает через **{time_text}**.\n"
                f"Дата окончания: **{expiry_str}**\n\n"
                f"Продлите подписку, чтобы не остаться без доступа к VPN!"
            )
            btn_text = "➕ Продлить ключ"
            # If trial, direct to new purchase flow as requested
            callback_data = "buy_new_key" if is_trial else f"extend_key_{key_id}"
        elif time_left_hours == 0:
            message = (
                f"❌ **Срок действия вашей подписки истек!**\n\n"
                f"Ваш доступ к VPN на сервере временно ограничен.\n"
                f"Дата окончания: **{expiry_str}**\n\n"
                "Продлите подписку прямо сейчас, чтобы восстановить соединение!"
            )
            btn_text = "➕ Восстановить доступ"
            callback_data = "buy_new_key" if is_trial else f"extend_key_{key_id}"
        else: # -24 follow-up
            message = (
                f"👋 **Мы скучаем!**\n\n"
                f"Заметили, что вы не продлили подписку, которая истекла вчера ({expiry_str}).\n\n"
                f"Если у вас возникли трудности с оплатой или настройкой — напишите в нашу поддержку, мы обязательно поможем!"
            )
            btn_text = "➕ Купить подписку"
            callback_data = "buy_new_key"
        
        builder = InlineKeyboardBuilder()
        builder.button(text="🔑 Мои ключи", callback_data="manage_keys")
        builder.button(text=btn_text, callback_data=callback_data)
        builder.adjust(2)
        
        await bot.send_message(chat_id=user_id, text=message, reply_markup=builder.as_markup(), parse_mode='Markdown')
        logger.info(f"Sent subscription notification to user {user_id} for key {key_id} ({time_left_hours} hours left, trial={is_trial}).")
        return True
    except Exception as e:
        logger.error(f"Error sending subscription notification to user {user_id}: {e}")
        return False

async def send_global_subscription_notification(
    bot: Bot, user_id: int, time_left_hours: int, expiry_date: datetime, hosts_count: int
) -> bool:
    try:
        expiry_str = expiry_date.strftime('%d.%m.%Y в %H:%M')

        if time_left_hours > 0:
            time_text = format_time_left(time_left_hours)
            message = (
                f"⚠️ **Внимание!** ⚠️\n\n"
                f"Срок действия вашей **глобальной подписки** (на {hosts_count} сервер(ов)) истекает через **{time_text}**.\n"
                f"Дата окончания: **{expiry_str}**\n\n"
                f"Продлите подписку, чтобы не остаться без доступа к VPN!"
            )
            btn_text = "➕ Продлить подписку"
        elif time_left_hours == 0:
            message = (
                f"❌ **Ваша глобальная подписка истекла!**\n\n"
                f"Ваш доступ ко всем серверам ({hosts_count} шт.) ограничен.\n"
                f"Дата окончания: **{expiry_str}**\n\n"
                "Продлите подписку, чтобы вернуть доступ сразу ко всем серверам!"
            )
            btn_text = "➕ Восстановить доступ"
        else: # -24 follow-up
            message = (
                f"👋 **Мы скучаем!**\n\n"
                f"Заметили, что вы не продлили вашу глобальную подписку, которая истекла вчера ({expiry_str}).\n\n"
                f"Если у вас возникли трудности — наша поддержка всегда на связи!"
            )
            btn_text = "💳 Купить подписку"

        builder = InlineKeyboardBuilder()
        builder.button(text="🔑 Мои ключи", callback_data="manage_keys")
        builder.button(text=btn_text, callback_data="select_host_new_ALL")
        builder.adjust(2)

        await bot.send_message(chat_id=user_id, text=message, reply_markup=builder.as_markup(), parse_mode='Markdown')
        logger.info(
            f"Sent GLOBAL subscription notification to user {user_id} ({hosts_count} hosts, {time_left_hours} hours left)."
        )
        return True
    except Exception as e:
        logger.error(f"Error sending GLOBAL subscription notification to user {user_id}: {e}")
        return False


async def _process_notification(
    bot: Bot, user_id: int, key_id: int | None, expiry_date: datetime, is_trial: bool, hosts_count: int = 1
) -> bool:
    current_time = time_utils.get_msk_now()
    time_left = expiry_date - current_time
    total_hours_left = math.ceil(time_left.total_seconds() / 3600)
    
    marks = TRIAL_NOTIFY_HOURS if is_trial else PAID_NOTIFY_HOURS
    # Check regular expiry
    for hours_mark in marks:
        if hours_mark - 1 < total_hours_left <= hours_mark:
            notification_type = 'global_expiry' if key_id is None else 'expiry'
            
            already_sent = await asyncio.to_thread(
                database.is_notification_sent, user_id, key_id, notification_type, hours_mark
            )
            if already_sent:
                return True

            if key_id is None:  # Global
                sent_ok = await send_global_subscription_notification(bot, user_id, hours_mark, expiry_date, hosts_count)
            else:
                sent_ok = await send_subscription_notification(bot, user_id, key_id, hours_mark, expiry_date, is_trial)

            if sent_ok:
                await asyncio.to_thread(database.mark_notification_sent, user_id, key_id, notification_type, hours_mark)
                return True

            logger.warning(
                "Scheduler: Notification send failed for user=%s key_id=%s type=%s mark=%s; not marking as sent.",
                user_id,
                key_id,
                notification_type,
                hours_mark,
            )
            return False
    return False

async def check_expiring_subscriptions(bot: Bot):
    logger.info("Scheduler: Checking for expiring subscriptions...")
    all_keys = await asyncio.to_thread(database.get_all_keys)

    # Determine global plan ids (host_name == 'ALL')
    global_plan_ids: set[int] = set()
    try:
        global_plans = await asyncio.to_thread(database.get_plans_for_host, 'ALL')
        for p in global_plans:
            try:
                global_plan_ids.add(int(p.get('plan_id')))
            except Exception:
                continue
    except Exception:
        global_plan_ids = set()

    # Build per-user buckets for global subscription keys
    global_keys_by_user: dict[int, list[dict]] = {}
    remaining_keys: list[dict] = []

    for key in all_keys:
        try:
            plan_id = key.get('plan_id', 0)
            if plan_id is not None and int(plan_id) in global_plan_ids and int(plan_id) > 0:
                global_keys_by_user.setdefault(int(key['user_id']), []).append(key)
            else:
                remaining_keys.append(key)
        except Exception:
            remaining_keys.append(key)

    # 1. Process GLOBAL notifications
    processed_global_users: set[int] = set()
    for user_id, keys in global_keys_by_user.items():
        try:
            expiry_dates: list[datetime] = []
            for k in keys:
                if not k.get('expiry_date'):
                    continue
                dt = time_utils.parse_iso_to_msk(k['expiry_date'])
                if dt:
                    expiry_dates.append(dt)

            if not expiry_dates:
                continue

            earliest_expiry = min(expiry_dates)
            global_window_processed = await _process_notification(
                bot, user_id, None, earliest_expiry, is_trial=False, hosts_count=len(keys)
            )
            if global_window_processed:
                processed_global_users.add(user_id)

        except Exception as e:
            logger.error(f"Error processing GLOBAL expiry for user {user_id}: {e}")
    
    # 2. Process Regular & Trial keys (SKIP users already notified globally)
    for key in remaining_keys:
        try:
            if not key.get('expiry_date'):
                continue
                
            expiry_date = time_utils.parse_iso_to_msk(key['expiry_date'])
            if not expiry_date:
                continue

            user_id = key['user_id']
            
            # Skip users who were already notified via global subscription
            if user_id in processed_global_users:
                continue
                
            key_id = key['key_id']
            plan_id = int(key.get('plan_id', 0) or 0)
            is_trial = (plan_id == 0)

            await _process_notification(bot, user_id, key_id, expiry_date, is_trial)
                    
        except Exception as e:
            logger.error(f"Error processing expiry for key {key.get('key_id')}: {e}")

async def enforce_clients_state_from_db() -> None:
    """
    Source of truth is DB: enforce enabled/disabled + expiry + unlimited traffic
    on all enabled hosts every scheduler cycle.
    """
    logger.info("Scheduler: Enforcing client states from DB...")
    all_hosts = await asyncio.to_thread(database.get_all_hosts, True)
    if not all_hosts:
        logger.info("Scheduler: No enabled hosts configured. Enforce skipped.")
        return

    total_checked = 0
    total_updated = 0
    total_already_ok = 0
    total_not_found = 0
    total_errors = 0
    total_expired_preserved = 0

    now = time_utils.get_msk_now()

    for host in all_hosts:
        host_name = host.get("host_name")
        if not host_name:
            continue

        keys_in_db = await asyncio.to_thread(database.get_keys_for_host, host_name)
        desired_by_email: dict[str, dict] = {}
        for db_key in keys_in_db:
            key_email = db_key.get("key_email")
            if not key_email:
                continue

            expiry_date = time_utils.parse_iso_to_msk(db_key.get("expiry_date"))
            if not expiry_date:
                logger.error(
                    f"Scheduler: Invalid expiry date for key '{key_email}': {db_key.get('expiry_date')}"
                )
                total_errors += 1
                continue

            # Preserve expired keys in DB and on panel.
            # Admin may intentionally move expiry into the past (e.g. reducing term),
            # and hard-deleting keys here causes data loss and confusing "key not found" flows.
            if expiry_date <= now:
                total_expired_preserved += 1

            desired_by_email[key_email] = {
                "enabled": expiry_date > now,
                "expiry_timestamp_ms": time_utils.get_timestamp_ms(expiry_date),
                "force_unlimited": True,
            }

        if desired_by_email:
            host_result = await xui_api.sync_clients_state_on_host(host_name, desired_by_email)
            total_checked += int(host_result.get("checked", 0))
            total_updated += int(host_result.get("updated", 0))
            total_already_ok += int(host_result.get("already_ok", 0))
            total_not_found += int(host_result.get("not_found", 0))
            total_errors += int(host_result.get("errors", 0))

    logger.info(
        "Scheduler: DB enforce finished. checked=%s updated=%s already_ok=%s not_found=%s expired_preserved=%s errors=%s",
        total_checked,
        total_updated,
        total_already_ok,
        total_not_found,
        total_expired_preserved,
        total_errors,
    )

async def sync_keys_with_panels():
    logger.info("Scheduler: Starting sync with XUI panels...")
    total_affected_records = 0
    failed_hosts = []  # Collect failed hosts for summary log
    
    all_hosts = await asyncio.to_thread(database.get_all_hosts, True)
    if not all_hosts:
        logger.info("Scheduler: No hosts configured in the database. Sync skipped.")
        return

    for host in all_hosts:
        host_name = host['host_name']
        
        try:
            api, inbound = xui_api.login_to_host(
                host_url=host['host_url'],
                username=host['host_username'],
                password=host['host_pass'],
                inbound_id=host['host_inbound_id']
            )

            if not api or not inbound:
                failed_hosts.append(host_name)
                continue
            
            full_inbound_details = api.inbound.get_by_id(inbound.id)
            if not full_inbound_details or not getattr(full_inbound_details, "settings", None):
                logger.error(
                    f"Scheduler: Failed to load full inbound details for host '{host_name}' "
                    f"(inbound_id={host.get('host_inbound_id')})."
                )
                failed_hosts.append(host_name)
                continue

            clients_on_server = {
                client.email: client
                for client in (full_inbound_details.settings.clients or [])
                if getattr(client, "email", None)
            }
            logger.info(f"Scheduler: Found {len(clients_on_server)} clients on the '{host_name}' panel.")


            keys_in_db = await asyncio.to_thread(database.get_keys_for_host, host_name)
            
            for db_key in keys_in_db:
                key_email = db_key['key_email']
                expiry_date = time_utils.parse_iso_to_msk(db_key['expiry_date'])
                if not expiry_date:
                    logger.error(f"Scheduler: Invalid expiry date for key '{key_email}': {db_key.get('expiry_date')}")
                    continue

                server_client = clients_on_server.pop(key_email, None)

                if server_client:
                    # Determine country flag based on server name
                    country_flag = xui_api.get_country_flag_by_host(host_name)
                    # Clean server name
                    clean_server_name = host_name.replace(' ', '').encode('ascii', 'ignore').decode('ascii')
                    clean_server_name = ''.join(c for c in clean_server_name if c.isalnum() or c == '_').lstrip('_')
                    server_remark = f"{country_flag}{clean_server_name}"
                    
                    # Generate fresh connection string
                    new_connection_string = xui_api.get_connection_string(
                        full_inbound_details, 
                        server_client.id, 
                        host['host_url'], 
                        remark=server_remark
                    )

                    # Compare expiry times directly (no reset field logic)
                    server_expiry_ms = int(getattr(server_client, "expiry_time", 0) or 0)
                    local_expiry_dt = expiry_date
                    local_expiry_ms = int(local_expiry_dt.timestamp() * 1000)
                    
                    # Update if expiry changed OR connection string needs update (e.g. flag changed)
                    current_db_string = db_key.get('connection_string')
                    if server_expiry_ms <= 0:
                        # Defensive guard: some panels can transiently return invalid expiry=0.
                        # Never overwrite DB expiry with epoch-like values.
                        logger.warning(
                            "Scheduler: Invalid server expiry for key '%s' on host '%s' (expiry_ms=%s). "
                            "Skipping expiry sync for this key.",
                            key_email,
                            host_name,
                            server_expiry_ms,
                        )
                        if new_connection_string and new_connection_string != current_db_string:
                            await asyncio.to_thread(
                                database.update_key_connection_string,
                                db_key['key_id'],
                                new_connection_string,
                            )
                            total_affected_records += 1
                        continue
                    
                    if (abs(server_expiry_ms - local_expiry_ms) > 1000) or (new_connection_string and new_connection_string != current_db_string):
                        # Use update_key_info to update both expiry and string if needed
                        # Convert server expiry to datetime
                        new_expiry_date_dt = time_utils.from_timestamp_ms(server_expiry_ms)
                        await asyncio.to_thread(database.update_key_info, db_key['key_id'], new_expiry_date_dt, new_connection_string)
                        
                        # Also sync UUID if changed (rare but possible)
                        if db_key['xui_client_uuid'] != server_client.id:
                             await asyncio.to_thread(database.update_key_status_from_server, key_email, server_client)

                        total_affected_records += 1
                        logger.info(f"Scheduler: Synced key '{key_email}' for host '{host_name}'.")
                else:
                    # Soft-delete: mark missing, recheck next cycle before removal
                    now_ts = time_utils.get_msk_now().isoformat()
                    await asyncio.to_thread(database.mark_key_missing, key_email, now_ts, host_name)
                    logger.warning(f"Scheduler: Key '{key_email}' for host '{host_name}' not found on server. Marked missing for recheck.")
                    total_affected_records += 1

            if clients_on_server:
                for orphan_email in clients_on_server.keys():
                    logger.warning(f"Scheduler: Found orphan client '{orphan_email}' on host '{host_name}' that is not tracked by the bot.")

        except Exception as e:
            logger.error(f"Scheduler: An unexpected error occurred while processing host '{host_name}': {e}", exc_info=True)
    
    # Log summary of failed hosts (single line instead of multiple errors)
    if failed_hosts:
        logger.warning(f"Scheduler: {len(failed_hosts)} host(s) unavailable: {', '.join(failed_hosts)}")
            
    logger.info(f"Scheduler: Sync with XUI panels finished. Total records affected: {total_affected_records}.")


async def cleanup_old_notifications():
    """Delete sent_notifications older than 30 days to keep DB size manageable."""
    try:
        await asyncio.to_thread(
            database.cleanup_notifications,
            days_to_keep=30
        )
    except Exception as e:
        logger.error(f"Scheduler: Failed to cleanup old notifications: {e}")


def _host_slug(host_name: str) -> str:
    """Generate a slug from host name for email generation."""
    return (host_name or "").replace(" ", "").lower()


async def auto_provision_new_hosts_for_global_users():
    """
    Auto-provision keys on new hosts for all users with active global subscriptions.
    
    This function is called periodically by the scheduler to ensure that when
    new hosts are added via the web admin panel, existing users with global
    subscriptions automatically get keys on the new hosts.
    """
    logger.info("Scheduler: Checking for new hosts to auto-provision for global users...")
    
    # Get all enabled hosts
    all_hosts = await asyncio.to_thread(database.get_all_hosts, True)
    if not all_hosts:
        logger.debug("Scheduler: No enabled hosts found.")
        return
    
    enabled_host_names = {h.get("host_name") for h in all_hosts if h.get("host_name") and h.get("host_name") != "ALL"}
    if not enabled_host_names:
        logger.debug("Scheduler: No regular enabled hosts found.")
        return
    
    # Get global plan IDs
    global_plan_ids = set()
    try:
        global_plans = await asyncio.to_thread(database.get_plans_for_host, "ALL")
        for p in global_plans:
            try:
                global_plan_ids.add(int(p.get("plan_id")))
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logger.error(f"Scheduler: Failed to get global plans: {e}")
        return
    
    if not global_plan_ids:
        logger.debug("Scheduler: No global plan IDs configured. Skipping auto-provision.")
        return
    
    # Get all keys and group by user
    all_keys = await asyncio.to_thread(database.get_all_keys)
    
    # Group keys by user_id
    keys_by_user: dict[int, list] = {}
    for key in all_keys:
        user_id = key.get("user_id")
        if user_id is None:
            continue
        if user_id not in keys_by_user:
            keys_by_user[user_id] = []
        keys_by_user[user_id].append(key)
    
    # Track statistics
    total_users_processed = 0
    total_keys_created = 0
    total_errors = 0
    
    provision_timeout = _provision_timeout_seconds()

    for user_id, user_keys in keys_by_user.items():
        try:
            # Filter to active paid keys (global subscription)
            now = time_utils.get_msk_now()
            active_paid_keys = []
            for k in user_keys:
                try:
                    expiry = time_utils.parse_iso_to_msk(k.get("expiry_date"))
                    if expiry and expiry > now:
                        plan_id = k.get("plan_id")
                        # Check if key belongs to global subscription
                        is_global = (plan_id is not None and int(plan_id) in global_plan_ids)
                        if is_global:
                            active_paid_keys.append(k)
                except (ValueError, TypeError):
                    continue
            
            if not active_paid_keys:
                continue  # No active global subscription for this user
            
            # Get hosts this user already has keys for
            existing_hosts = {k.get("host_name") for k in active_paid_keys if k.get("host_name")}
            
            # Find missing hosts
            missing_hosts = enabled_host_names - existing_hosts
            if not missing_hosts:
                continue  # User has keys on all hosts
            
            # Calculate target expiry from the soonest-expiring global key
            try:
                min_expiry_dt = min(
                    time_utils.parse_iso_to_msk(k["expiry_date"])
                    for k in active_paid_keys
                    if time_utils.parse_iso_to_msk(k.get("expiry_date"))
                )
                remaining_seconds = int((min_expiry_dt - now).total_seconds())
            except (ValueError, TypeError):
                min_expiry_dt = None
                remaining_seconds = 0
            
            if remaining_seconds <= 0 or not min_expiry_dt:
                logger.warning(f"Scheduler: User {user_id} has global subscription but no valid expiry. Skipping.")
                continue

            target_expiry_ms = time_utils.get_timestamp_ms(min_expiry_dt)
            
            # Pick deterministic global plan id to avoid inconsistent plan assignment across cycles.
            first_global_plan_id = int(min(global_plan_ids))
            
            logger.info(f"Scheduler: User {user_id} has global subscription. Missing hosts: {missing_hosts}. Auto-provisioning...")
            total_users_processed += 1
            
            # Provision keys on missing hosts
            for host_name in missing_hosts:
                try:
                    email = f"user{user_id}-global-{_host_slug(host_name)}"
                    logger.info(f"Scheduler: Auto-provisioning key for user {user_id} on host '{host_name}' with email '{email}'")
                    
                    # Create key on host
                    res = await asyncio.wait_for(
                        xui_api.create_or_update_key_on_host_absolute_expiry(
                            host_name=host_name,
                            email=email,
                            target_expiry_ms=target_expiry_ms,
                            telegram_id=str(user_id)
                        ),
                        timeout=provision_timeout
                    )
                    
                    if res:
                        # Persist to database
                        existing_key = await asyncio.to_thread(database.get_key_by_email, res["email"])
                        if existing_key:
                            await asyncio.to_thread(
                                database.update_key_by_email,
                                key_email=res["email"],
                                host_name=host_name,
                                xui_client_uuid=res["client_uuid"],
                                expiry_timestamp_ms=res["expiry_timestamp_ms"],
                                connection_string=res.get("connection_string"),
                                plan_id=first_global_plan_id
                            )
                        else:
                            await asyncio.to_thread(
                                database.add_new_key,
                                user_id=user_id,
                                host_name=host_name,
                                xui_client_uuid=res["client_uuid"],
                                key_email=res["email"],
                                expiry_timestamp_ms=res["expiry_timestamp_ms"],
                                connection_string=res.get("connection_string"),
                                plan_id=first_global_plan_id
                            )
                        total_keys_created += 1
                        logger.info(f"Scheduler: Successfully created key for user {user_id} on host '{host_name}'")
                    else:
                        logger.error(f"Scheduler: Failed to create key for user {user_id} on host '{host_name}'")
                        total_errors += 1
                        
                except asyncio.TimeoutError:
                    logger.error(
                        f"Scheduler: Timeout provisioning key for user {user_id} on host '{host_name}' "
                        f"after {provision_timeout}s"
                    )
                    total_errors += 1
                except Exception as e:
                    logger.error(f"Scheduler: Error provisioning key for user {user_id} on host '{host_name}': {e}")
                    total_errors += 1
                    
        except Exception as e:
            logger.error(f"Scheduler: Error processing user {user_id} for auto-provision: {e}")
            total_errors += 1
    
    logger.info(f"Scheduler: Auto-provision finished. Users processed: {total_users_processed}, Keys created: {total_keys_created}, Errors: {total_errors}")


async def periodic_xtls_sync():
    """
    Periodically synchronize XTLS settings across all hosts.
    
    Ensures that:
    - Reality TCP protocol clients have XTLS-Vision flow enabled
    - gRPC protocol clients don't have XTLS flow
    - Settings match between app config and actual 3xui panel settings
    
    Runs every 5-10 minutes and at bot startup.
    """
    try:
        logger.info("Starting periodic XTLS synchronization across all hosts...")
        sync_results = await xui_api.sync_inbounds_xtls_from_all_hosts()
        
        # Log results
        if sync_results and isinstance(sync_results, dict):
            total_fixed = 0
            for host_name, result in sync_results.items():
                if isinstance(result, dict):
                    fixed = result.get('fixed', 0)
                    status = result.get('status', 'unknown')
                    
                    if fixed > 0:
                        logger.info(f"XTLS sync for '{host_name}': {fixed} clients fixed. Status: {status}")
                        total_fixed += fixed
                    elif status == 'success':
                        logger.debug(f"XTLS sync for '{host_name}': no fixes needed.")
                    else:
                        logger.warning(f"XTLS sync for '{host_name}': status={status}")
            
            if total_fixed > 0:
                logger.info(f"Periodic XTLS sync completed: {total_fixed} total clients fixed across all hosts")
            else:
                logger.debug("Periodic XTLS sync completed: all clients have correct settings")
        else:
            logger.warning(f"Unexpected XTLS sync result format: {sync_results}")
    
    except Exception as e:
        logger.error(f"Scheduler: Failed to perform periodic XTLS sync: {e}", exc_info=True)

async def periodic_subscription_check(bot_controller: BotController):
    logger.info("Scheduler has been started.")
    await asyncio.sleep(10)

    # Track when XTLS sync was last performed (run every 5 min instead of every CHECK_INTERVAL)
    xtls_sync_interval = 300  # 5 minutes
    last_xtls_sync_time = 0

    while True:
        try:
            # Always enforce access state by DB even if panel_sync_enabled is disabled.
            await enforce_clients_state_from_db()

            if _bool_setting("panel_sync_enabled", default=False):
                await sync_keys_with_panels()
            else:
                logger.debug("Scheduler: panel sync disabled (panel_sync_enabled=false).")
            
            # Run cleanup once per cycle (or could be less frequent, but this is cheap)
            await cleanup_old_notifications()

            # Auto-provision new hosts for global subscription users
            await auto_provision_new_hosts_for_global_users()

            # Run XTLS sync separately on its own interval (every 5 minutes)
            current_time = time.time()
            if _bool_setting("xtls_sync_enabled", default=False) and current_time - last_xtls_sync_time >= xtls_sync_interval:
                await periodic_xtls_sync()
                last_xtls_sync_time = current_time
            elif current_time - last_xtls_sync_time >= xtls_sync_interval:
                logger.debug("Scheduler: XTLS sync disabled (xtls_sync_enabled=false).")

            if bot_controller.get_status().get("is_running"):
                bot = bot_controller.get_bot_instance()
                if bot:
                    await check_expiring_subscriptions(bot)
                else:
                    logger.warning("Scheduler: Bot is marked as running, but instance is not available.")
            else:
                logger.info("Scheduler: Bot is stopped, skipping user notifications.")

        except Exception as e:
            logger.error(f"Scheduler: An unhandled error occurred in the main loop: {e}", exc_info=True)
            
        logger.info(f"Scheduler: Cycle finished. Next check in {CHECK_INTERVAL_SECONDS} seconds.")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

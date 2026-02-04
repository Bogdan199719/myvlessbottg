import asyncio
import logging
import math

from datetime import datetime, timedelta
from shop_bot.utils import time_utils

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import Bot

from shop_bot.bot_controller import BotController
from shop_bot.data_manager import database
from shop_bot.modules import xui_api
from shop_bot.bot import keyboards

CHECK_INTERVAL_SECONDS = 300
PAID_NOTIFY_HOURS = {24, 1, 0}
TRIAL_NOTIFY_HOURS = {1, 0}


logger = logging.getLogger(__name__)

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

async def send_subscription_notification(bot: Bot, user_id: int, key_id: int, time_left_hours: int, expiry_date: datetime, is_trial: bool = False):
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
        
    except Exception as e:
        logger.error(f"Error sending subscription notification to user {user_id}: {e}")

async def send_global_subscription_notification(bot: Bot, user_id: int, time_left_hours: int, expiry_date: datetime, hosts_count: int):
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
    except Exception as e:
        logger.error(f"Error sending GLOBAL subscription notification to user {user_id}: {e}")


async def _process_notification(bot: Bot, user_id: int, key_id: int | None, expiry_date: datetime, is_trial: bool, hosts_count: int = 1):
    current_time = time_utils.get_msk_now()
    time_left = expiry_date - current_time
    total_hours_left = math.ceil(time_left.total_seconds() / 3600)
    
    marks = TRIAL_NOTIFY_HOURS if is_trial else PAID_NOTIFY_HOURS
    # Check regular expiry
    for hours_mark in marks:
        if hours_mark - 1 < total_hours_left <= hours_mark:
            notification_type = 'global_expiry' if key_id is None else 'expiry'
            
            if not await asyncio.to_thread(database.is_notification_sent, user_id, key_id, notification_type, hours_mark):
                if key_id is None: # Global
                     await send_global_subscription_notification(bot, user_id, hours_mark, expiry_date, hosts_count)
                else:
                     await send_subscription_notification(bot, user_id, key_id, hours_mark, expiry_date, is_trial)
                
                await asyncio.to_thread(database.mark_notification_sent, user_id, key_id, notification_type, hours_mark)
            return

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
            await _process_notification(bot, user_id, None, earliest_expiry, is_trial=False, hosts_count=len(keys))
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

async def sync_keys_with_panels():
    logger.info("Scheduler: Starting sync with XUI panels...")
    total_affected_records = 0
    failed_hosts = []  # Collect failed hosts for summary log
    
    all_hosts = await asyncio.to_thread(database.get_all_hosts)
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
            clients_on_server = {client.email: client for client in (full_inbound_details.settings.clients or [])}
            logger.info(f"Scheduler: Found {len(clients_on_server)} clients on the '{host_name}' panel.")


            keys_in_db = await asyncio.to_thread(database.get_keys_for_host, host_name)
            
            for db_key in keys_in_db:
                key_email = db_key['key_email']
                expiry_date = time_utils.parse_iso_to_msk(db_key['expiry_date'])
                if not expiry_date:
                    logger.error(f"Scheduler: Invalid expiry date for key '{key_email}': {db_key.get('expiry_date')}")
                    continue

                now = time_utils.get_msk_now()
                if expiry_date < now - timedelta(days=5):
                    logger.info(f"Scheduler: Key '{key_email}' expired more than 5 days ago. Deleting from panel and DB.")
                    try:
                        await xui_api.delete_client_on_host(host_name, key_email)
                    except Exception as e:
                        logger.error(f"Scheduler: Failed to delete client '{key_email}' from panel: {e}")
                    await asyncio.to_thread(database.delete_key_by_email, key_email)
                    total_affected_records += 1
                    continue

                server_client = clients_on_server.pop(key_email, None)

                if server_client:
                    # Compare expiry times directly (no reset field logic)
                    server_expiry_ms = server_client.expiry_time
                    local_expiry_dt = expiry_date
                    local_expiry_ms = int(local_expiry_dt.timestamp() * 1000)

                    if abs(server_expiry_ms - local_expiry_ms) > 1000:
                        await asyncio.to_thread(database.update_key_status_from_server, key_email, server_client)
                        total_affected_records += 1
                        logger.info(f"Scheduler: Synced (updated) key '{key_email}' for host '{host_name}'.")
                else:
                    # Soft-delete: mark missing, recheck next cycle before removal
                    now_ts = time_utils.get_msk_now().isoformat()
                    await asyncio.to_thread(database.mark_key_missing, key_email, now_ts)
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

async def periodic_subscription_check(bot_controller: BotController):
    logger.info("Scheduler has been started.")
    await asyncio.sleep(10)

    while True:
        try:
            await sync_keys_with_panels()
            
            # Run cleanup once per cycle (or could be less frequent, but this is cheap)
            await cleanup_old_notifications()

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
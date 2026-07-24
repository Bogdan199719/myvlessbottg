import asyncio
import html
import logging
import math
import time

from datetime import datetime, timedelta
from shop_bot.utils import time_utils

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from yookassa import Configuration, Payment

from shop_bot.bot_controller import BotController
from shop_bot.data_manager import database
from shop_bot.data_manager.database import host_slug as _host_slug
from shop_bot.modules import xui_api
from shop_bot.modules import mtg_api
from shop_bot.modules import host_health
from shop_bot.bot import handlers, keyboards

CHECK_INTERVAL_SECONDS = 60
PAID_NOTIFY_HOURS = {24, 1, 0, -24, -72, -168}
TRIAL_NOTIFY_HOURS = {1, 0, -24, -72}
ONBOARDING_IDLE_NOTIFY_HOURS = (3, 24, 72)
ONBOARDING_IDLE_WINDOW_HOURS = 2
ONBOARDING_IDLE_NOTIFICATION_TYPE = "onboarding_idle"

_DEFAULT_PROVISION_TIMEOUT_SECONDS = 45
_HOST_FAILURE_BACKOFF_SECONDS = 15 * 60
_HOST_STATE_SYNC_TIMEOUT_SECONDS = 120
_host_failure_backoff: dict[str, float] = {}
_PANEL_SNAPSHOT_TIMEOUT_SECONDS = 120
_MTG_ENFORCE_REQUEST_TIMEOUT_SECONDS = 20
_MTG_ENFORCE_MAX_CONCURRENCY = 8
_MTG_ENFORCE_PER_HOST_CONCURRENCY = 2
_MTG_FAILURE_BACKOFF_SECONDS = 15 * 60
_mtg_failure_backoff: dict[str, float] = {}
_HOST_HEALTH_INTERVAL_SECONDS = 5 * 60
_HOST_HEALTH_TIMEOUT_SECONDS = 45
_HOST_HEALTH_MAX_CONCURRENCY = 4
_IP_LIMIT_INTERVAL_SECONDS = 5 * 60
_IP_LIMIT_PANEL_TIMEOUT_SECONDS = 90
_IP_LIMIT_MAX_CONCURRENCY = 4


logger = logging.getLogger(__name__)


def _bool_setting(key: str, default: bool = False) -> bool:
    raw = database.get_setting(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_setting(key: str, default: int, minimum: int, maximum: int) -> int:
    raw = database.get_setting(key)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _provision_timeout_seconds() -> int:
    raw = database.get_setting("provision_timeout_seconds")
    try:
        timeout = int(raw) if raw is not None else _DEFAULT_PROVISION_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = _DEFAULT_PROVISION_TIMEOUT_SECONDS
    return max(10, min(timeout, 180))


def _is_host_in_failure_backoff(host_name: str) -> bool:
    retry_at = _host_failure_backoff.get(host_name)
    if not retry_at:
        return False
    if time.monotonic() >= retry_at:
        _host_failure_backoff.pop(host_name, None)
        return False
    return True


def _mark_host_failure(host_name: str, reason: str) -> None:
    was_in_backoff = _is_host_in_failure_backoff(host_name)
    _host_failure_backoff[host_name] = (
        time.monotonic() + _HOST_FAILURE_BACKOFF_SECONDS
    )
    if not was_in_backoff:
        logger.warning(
            "Scheduler: Host '%s' temporarily backed off for %s seconds after failure: %s",
            host_name,
            _HOST_FAILURE_BACKOFF_SECONDS,
            reason,
        )


def _mark_host_success(host_name: str) -> None:
    _host_failure_backoff.pop(host_name, None)


def _is_mtg_host_in_failure_backoff(host_name: str) -> bool:
    retry_at = _mtg_failure_backoff.get(host_name)
    if not retry_at:
        return False
    if time.monotonic() >= retry_at:
        _mtg_failure_backoff.pop(host_name, None)
        return False
    return True


def _mark_mtg_host_failure(host_name: str, reason: str) -> None:
    was_in_backoff = _is_mtg_host_in_failure_backoff(host_name)
    _mtg_failure_backoff[host_name] = (
        time.monotonic() + _MTG_FAILURE_BACKOFF_SECONDS
    )
    if not was_in_backoff:
        logger.warning(
            "Scheduler: MTG host '%s' temporarily backed off for %s seconds after failure: %s",
            host_name,
            _MTG_FAILURE_BACKOFF_SECONDS,
            reason,
        )


def _mark_mtg_host_success(host_name: str) -> None:
    _mtg_failure_backoff.pop(host_name, None)


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


def _notification_type_for_expiry_cycle(
    base_type: str, expiry_date: datetime
) -> str:
    expiry_ms = time_utils.get_timestamp_ms(expiry_date)
    return f"{base_type}:{expiry_ms}"


async def send_subscription_notification(
    bot: Bot,
    user_id: int,
    key_id: int,
    time_left_hours: int,
    expiry_date: datetime,
    is_trial: bool = False,
) -> bool:
    try:
        expiry_str = expiry_date.strftime("%d.%m.%Y в %H:%M")

        if time_left_hours > 0:
            time_text = format_time_left(time_left_hours)
            message = (
                f"✨ <b>Внимание!</b> ✨\n\n"
                f"☀️ Солнышко, твоя подписка закончится через <b>{time_text}</b>! "
                f"Продли её, чтобы всё было хорошо 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Продлить подписку"
            callback_data = "select_host_new_ALL"
        elif time_left_hours == 0:
            message = (
                f"✨ <b>Внимание!</b> ✨\n\n"
                f"☀️ Солнышко, твоя подписка уже закончилась. "
                f"Продли её, чтобы всё снова было хорошо 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Восстановить доступ"
            callback_data = "select_host_new_ALL"
        elif time_left_hours == -24:
            message = (
                f"✨ <b>Внимание!</b> ✨\n\n"
                f"☀️ Солнышко, твоя подписка закончилась вчера. "
                f"Продли её, чтобы всё снова было хорошо 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Купить подписку"
            callback_data = "select_host_new_ALL"
        elif time_left_hours == -72:
            message = (
                "☀️ <b>Проверка связи!</b>\n\n"
                "Твоя подписка уже 3 дня отдыхает без тебя. "
                "Если интернет снова начал капризничать, можно быстро вернуть доступ одной кнопкой 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Вернуть доступ"
            callback_data = "select_host_new_ALL"
        else:  # -168 follow-up
            message = (
                "📡 <b>Твой VPN передаёт сигнал</b>\n\n"
                "Подписка закончилась неделю назад. Если доступ ещё нужен, "
                "его можно восстановить без новой настройки — просто продли подписку.\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "💳 Восстановить VPN"
            callback_data = "select_host_new_ALL"

        builder = InlineKeyboardBuilder()
        builder.button(text=btn_text, callback_data=callback_data)
        builder.adjust(1)

        await bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        logger.info(
            f"Sent subscription notification to user {user_id} for key {key_id} ({time_left_hours} hours left, trial={is_trial})."
        )
        return True
    except TelegramForbiddenError as e:
        logger.warning(
            f"Cannot send subscription notification to user {user_id}: {e}. Marking as handled."
        )
        return True
    except Exception as e:
        logger.error(f"Error sending subscription notification to user {user_id}: {e}")
        return False


async def send_global_subscription_notification(
    bot: Bot,
    user_id: int,
    time_left_hours: int,
    expiry_date: datetime,
    hosts_count: int,
) -> bool:
    try:
        expiry_str = expiry_date.strftime("%d.%m.%Y в %H:%M")

        if time_left_hours > 0:
            time_text = format_time_left(time_left_hours)
            message = (
                f"✨ <b>Внимание!</b> ✨\n\n"
                f"☀️ Солнышко, твоя подписка закончится через <b>{time_text}</b>! "
                f"Продли её, чтобы всё было хорошо 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Продлить подписку"
        elif time_left_hours == 0:
            message = (
                f"✨ <b>Внимание!</b> ✨\n\n"
                f"☀️ Солнышко, твоя подписка уже закончилась. "
                f"Продли её, чтобы всё снова было хорошо 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Восстановить доступ"
        elif time_left_hours == -24:
            message = (
                f"✨ <b>Внимание!</b> ✨\n\n"
                f"☀️ Солнышко, твоя подписка закончилась вчера. "
                f"Продли её, чтобы всё снова было хорошо 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "💳 Купить подписку"
        elif time_left_hours == -72:
            message = (
                "☀️ <b>Проверка связи!</b>\n\n"
                "Твоя подписка уже 3 дня отдыхает без тебя. "
                "Если интернет снова начал капризничать, можно быстро вернуть доступ одной кнопкой 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Вернуть доступ"
        else:  # -168 follow-up
            message = (
                "📡 <b>Твой VPN передаёт сигнал</b>\n\n"
                "Подписка закончилась неделю назад. Если доступ ещё нужен, "
                "его можно восстановить без новой настройки — просто продли подписку.\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "💳 Восстановить VPN"

        builder = InlineKeyboardBuilder()
        builder.button(text=btn_text, callback_data="select_host_new_ALL")
        builder.adjust(1)

        await bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        logger.info(
            f"Sent GLOBAL subscription notification to user {user_id} ({hosts_count} hosts, {time_left_hours} hours left)."
        )
        return True
    except TelegramForbiddenError as e:
        logger.warning(
            f"Cannot send GLOBAL subscription notification to user {user_id}: {e}. Marking as handled."
        )
        return True
    except Exception as e:
        logger.error(
            f"Error sending GLOBAL subscription notification to user {user_id}: {e}"
        )
        return False


async def send_proxy_expiry_notification(
    bot: Bot, user_id: int, key_id: int, time_left_hours: int, expiry_date: datetime
) -> bool:
    try:
        expiry_str = expiry_date.strftime("%d.%m.%Y в %H:%M")

        if time_left_hours > 0:
            time_text = format_time_left(time_left_hours)
            message = (
                f"✨ <b>Внимание!</b> ✨\n\n"
                f"☀️ Солнышко, твой Telegram-прокси закончится через <b>{time_text}</b>! "
                f"Продли его, чтобы всё было хорошо 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Продлить прокси"
            callback_data = f"extend_key_{key_id}"
        elif time_left_hours == 0:
            message = (
                f"✨ <b>Внимание!</b> ✨\n\n"
                f"☀️ Солнышко, твой Telegram-прокси уже закончился. "
                f"Продли его, чтобы всё снова было хорошо 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Активировать прокси"
            callback_data = f"extend_key_{key_id}"
        elif time_left_hours == -24:
            message = (
                f"✨ <b>Внимание!</b> ✨\n\n"
                f"☀️ Солнышко, твой Telegram-прокси закончился вчера. "
                f"Продли его, чтобы всё снова было хорошо 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Купить прокси"
            callback_data = "buy_proxy"
        elif time_left_hours == -72:
            message = (
                "☀️ <b>Проверка связи!</b>\n\n"
                "Твой Telegram-прокси уже 3 дня отдыхает без тебя. "
                "Если он снова нужен, можно быстро вернуть доступ одной кнопкой 💕\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Вернуть прокси"
            callback_data = f"extend_key_{key_id}"
        else:  # -168 follow-up
            message = (
                "📡 <b>Твой прокси передаёт сигнал</b>\n\n"
                "Доступ закончился неделю назад. Если прокси ещё нужен, "
                "его можно восстановить без новой настройки.\n\n"
                f"Дата окончания: <b>{expiry_str}</b>"
            )
            btn_text = "➕ Купить прокси"
            callback_data = "buy_proxy"

        builder = InlineKeyboardBuilder()
        builder.button(text="📡 Мои прокси", callback_data="manage_keys")
        builder.button(text=btn_text, callback_data=callback_data)
        builder.adjust(2)

        await bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        logger.info(
            f"Sent proxy expiry notification to user {user_id} for key {key_id} ({time_left_hours} hours left)."
        )
        return True
    except TelegramForbiddenError as e:
        logger.warning(
            f"Cannot send proxy expiry notification to user {user_id}: {e}. Marking as handled."
        )
        return True
    except Exception as e:
        logger.error(f"Error sending proxy expiry notification to user {user_id}: {e}")
        return False


async def send_idle_onboarding_notification(
    bot: Bot, user_id: int, hours_mark: int
) -> bool:
    try:
        trial_enabled = _bool_setting("trial_enabled", default=True)

        if hours_mark == 3:
            message = (
                "☀️ <b>Ты заходил посмотреть VPN, но ещё не включил доступ.</b>\n\n"
                "Можно начать без оплаты: забери пробный период и проверь скорость "
                "на своих устройствах."
            )
            primary_text = "🎁 Попробовать бесплатно"
        elif hours_mark == 24:
            message = (
                "✨ <b>Маленькое напоминание</b>\n\n"
                "VPN пригодится, когда сайты не открываются, видео тормозит или "
                "нужен стабильный доступ в дороге.\n\n"
                "У тебя всё ещё доступен бесплатный пробный период — можно проверить без оплаты."
            )
            primary_text = "🎁 Забрать пробный доступ"
        else:
            message = (
                "👋 <b>Последнее напоминание</b>\n\n"
                "Ты заходил в бот, но так и не подключил VPN. Если доступ ещё актуален, "
                "можно начать с пробного периода или сразу выбрать подписку."
            )
            primary_text = "🎁 Попробовать бесплатно"

        builder = InlineKeyboardBuilder()
        if trial_enabled:
            builder.button(text=primary_text, callback_data="get_trial")
        builder.button(text="💳 Купить подписку", callback_data="buy_subscription")
        builder.adjust(1)

        await bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        logger.info(
            "Sent idle onboarding notification to user %s (%s hours after registration).",
            user_id,
            hours_mark,
        )
        return True
    except TelegramForbiddenError as e:
        logger.warning(
            "Cannot send idle onboarding notification to user %s: %s. Marking as handled.",
            user_id,
            e,
        )
        return True
    except Exception as e:
        logger.error(
            "Error sending idle onboarding notification to user %s: %s",
            user_id,
            e,
        )
        return False


async def check_idle_onboarding_users(bot: Bot):
    logger.info("Scheduler: Checking for idle onboarding users...")
    for hours_mark in ONBOARDING_IDLE_NOTIFY_HOURS:
        min_age = hours_mark
        max_age = hours_mark + ONBOARDING_IDLE_WINDOW_HOURS
        candidates = await asyncio.to_thread(
            database.get_idle_onboarding_users,
            min_age,
            max_age,
            100,
        )
        if not candidates:
            continue

        for user in candidates:
            try:
                user_id = int(user["telegram_id"])
            except (TypeError, ValueError):
                continue

            already_sent = await asyncio.to_thread(
                database.is_notification_sent,
                user_id,
                None,
                ONBOARDING_IDLE_NOTIFICATION_TYPE,
                hours_mark,
            )
            if already_sent:
                continue

            sent_ok = await send_idle_onboarding_notification(
                bot, user_id, hours_mark
            )
            if sent_ok:
                await asyncio.to_thread(
                    database.mark_notification_sent,
                    user_id,
                    None,
                    ONBOARDING_IDLE_NOTIFICATION_TYPE,
                    hours_mark,
                )
            else:
                logger.warning(
                    "Scheduler: Idle onboarding notification failed for user=%s mark=%s; not marking as sent.",
                    user_id,
                    hours_mark,
                )


async def _process_notification(
    bot: Bot,
    user_id: int,
    key_id: int | None,
    expiry_date: datetime,
    is_trial: bool,
    hosts_count: int = 1,
    service_type: str = "xui",
) -> bool:
    current_time = time_utils.get_msk_now()
    time_left = expiry_date - current_time
    total_hours_left = math.ceil(time_left.total_seconds() / 3600)

    marks = TRIAL_NOTIFY_HOURS if is_trial else PAID_NOTIFY_HOURS
    # Check regular expiry
    for hours_mark in marks:
        if hours_mark - 1 < total_hours_left <= hours_mark:
            base_notification_type = "global_expiry" if key_id is None else "expiry"
            notification_type = _notification_type_for_expiry_cycle(
                base_notification_type, expiry_date
            )

            already_sent = await asyncio.to_thread(
                database.is_notification_sent,
                user_id,
                key_id,
                notification_type,
                hours_mark,
            )
            if already_sent:
                return True

            legacy_sent = await asyncio.to_thread(
                database.is_legacy_notification_sent_for_expiry_window,
                user_id,
                key_id,
                base_notification_type,
                hours_mark,
                expiry_date,
            )
            if legacy_sent:
                await asyncio.to_thread(
                    database.mark_notification_sent,
                    user_id,
                    key_id,
                    notification_type,
                    hours_mark,
                )
                return True

            if key_id is None:  # Global
                sent_ok = await send_global_subscription_notification(
                    bot, user_id, hours_mark, expiry_date, hosts_count
                )
            elif service_type == "mtg":
                sent_ok = await send_proxy_expiry_notification(
                    bot, user_id, key_id, hours_mark, expiry_date
                )
            else:
                sent_ok = await send_subscription_notification(
                    bot, user_id, key_id, hours_mark, expiry_date, is_trial
                )

            if sent_ok:
                await asyncio.to_thread(
                    database.mark_notification_sent,
                    user_id,
                    key_id,
                    notification_type,
                    hours_mark,
                )
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
    current_time = time_utils.get_msk_now()

    # Determine global plan ids (host_name == 'ALL')
    try:
        global_plan_ids = await asyncio.to_thread(database.get_global_plan_ids)
    except Exception:
        global_plan_ids = set()

    # Build per-user buckets for global subscription keys. Trial XUI access is
    # also a global subscription in the product flow, even though its DB keys
    # have plan_id=0 on each technical host.
    paid_global_keys_by_user: dict[int, list[dict]] = {}
    trial_global_keys_by_user: dict[int, list[dict]] = {}
    remaining_keys: list[dict] = []

    for key in all_keys:
        try:
            service_type = key.get("service_type", "xui")
            plan_id = int(key.get("plan_id", 0) or 0)
            if service_type == "xui" and plan_id == 0:
                trial_global_keys_by_user.setdefault(int(key["user_id"]), []).append(
                    key
                )
            elif database.is_global_xui_key(key, global_plan_ids):
                paid_global_keys_by_user.setdefault(int(key["user_id"]), []).append(
                    key
                )
            else:
                remaining_keys.append(key)
        except Exception:
            remaining_keys.append(key)

    # 1. Process GLOBAL paid and trial notifications
    processed_global_users: set[int] = set()
    active_global_users: set[int] = set()

    async def _process_global_bucket(
        user_id: int, keys: list[dict], *, is_trial: bool
    ) -> None:
        try:
            expiry_dates: list[datetime] = []
            for k in keys:
                if not k.get("expiry_date"):
                    continue
                dt = time_utils.parse_iso_to_msk(k["expiry_date"])
                if dt:
                    expiry_dates.append(dt)
                    if dt > current_time:
                        active_global_users.add(user_id)

            if not expiry_dates:
                return

            earliest_expiry = min(expiry_dates)
            if is_trial:
                time_left = earliest_expiry - current_time
                total_hours_left = math.ceil(time_left.total_seconds() / 3600)
                for hours_mark in TRIAL_NOTIFY_HOURS:
                    if hours_mark - 1 < total_hours_left <= hours_mark:
                        old_type = _notification_type_for_expiry_cycle(
                            "expiry", earliest_expiry
                        )
                        already_handled_by_old_per_host_flow = False
                        for key in keys:
                            key_id = key.get("key_id")
                            if key_id is None:
                                continue
                            already_sent = await asyncio.to_thread(
                                database.is_notification_sent,
                                user_id,
                                int(key_id),
                                old_type,
                                hours_mark,
                            )
                            if already_sent:
                                already_handled_by_old_per_host_flow = True
                                break

                            legacy_sent = await asyncio.to_thread(
                                database.is_legacy_notification_sent_for_expiry_window,
                                user_id,
                                int(key_id),
                                "expiry",
                                hours_mark,
                                earliest_expiry,
                            )
                            if legacy_sent:
                                already_handled_by_old_per_host_flow = True
                                break

                        if already_handled_by_old_per_host_flow:
                            new_type = _notification_type_for_expiry_cycle(
                                "global_expiry", earliest_expiry
                            )
                            await asyncio.to_thread(
                                database.mark_notification_sent,
                                user_id,
                                None,
                                new_type,
                                hours_mark,
                            )
                            processed_global_users.add(user_id)
                            return

            global_window_processed = await _process_notification(
                bot,
                user_id,
                None,
                earliest_expiry,
                is_trial=is_trial,
                hosts_count=len(keys),
            )
            if global_window_processed:
                processed_global_users.add(user_id)

        except Exception as e:
            label = "TRIAL GLOBAL" if is_trial else "GLOBAL"
            logger.error(f"Error processing {label} expiry for user {user_id}: {e}")

    for user_id, keys in paid_global_keys_by_user.items():
        await _process_global_bucket(user_id, keys, is_trial=False)

    for user_id, keys in trial_global_keys_by_user.items():
        if user_id in active_global_users or user_id in processed_global_users:
            continue
        await _process_global_bucket(user_id, keys, is_trial=True)

    # 2. Process Regular, Trial and MTG keys.
    # If the user has an active global VPN subscription, skip other XUI
    # reminders for old per-host/trial keys. MTG proxy expiry is a separate
    # product and still needs its own reminder.
    for key in remaining_keys:
        try:
            if not key.get("expiry_date"):
                continue

            expiry_date = time_utils.parse_iso_to_msk(key["expiry_date"])
            if not expiry_date:
                continue

            user_id = key["user_id"]

            key_id = key["key_id"]
            plan_id = int(key.get("plan_id", 0) or 0)
            is_trial = plan_id == 0
            service_type = key.get("service_type", "xui")

            if (
                user_id in active_global_users or user_id in processed_global_users
            ) and service_type != "mtg":
                continue

            await _process_notification(
                bot,
                user_id,
                key_id,
                expiry_date,
                is_trial,
                service_type=service_type,
            )

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
    total_traffic_fixed = 0
    total_ip_limit_fixed = 0
    total_expired_preserved = 0

    now = time_utils.get_msk_now()
    ip_limit_enabled = _bool_setting("ip_limit_enabled", default=True)
    configured_ip_limit = _int_setting("ip_limit_max_ips", 10, 1, 100)
    # Keep native blocking disabled until this key has received a warning and
    # exhausted its grace period. The panel still records IP observations with
    # limitIp=0; this behavior is verified by the bulk clientIps API.
    warning_ip_limit = 0
    enforced_key_ids = (
        await asyncio.to_thread(database.get_enforced_xui_ip_limit_key_ids)
        if ip_limit_enabled
        else set()
    )

    for host in all_hosts:
        host_name = host.get("host_name")
        if not host_name:
            continue
        if _is_host_in_failure_backoff(host_name):
            logger.debug(
                "Scheduler: Host '%s' is in failure backoff; enforce skipped.",
                host_name,
            )
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
                "ip_limit": (
                    configured_ip_limit
                    if db_key.get("key_id") in enforced_key_ids
                    else warning_ip_limit
                )
                if ip_limit_enabled
                else 0,
            }

        if desired_by_email:
            try:
                host_result = await asyncio.wait_for(
                    xui_api.sync_clients_state_on_host(host_name, desired_by_email),
                    timeout=_HOST_STATE_SYNC_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                _mark_host_failure(host_name, "client state sync timed out")
                total_errors += 1
                logger.warning(
                    "Scheduler: Client state sync timed out for host '%s' after %s seconds.",
                    host_name,
                    _HOST_STATE_SYNC_TIMEOUT_SECONDS,
                )
                continue
            if int(host_result.get("errors", 0)) and not int(
                host_result.get("checked", 0)
            ):
                _mark_host_failure(host_name, "client state sync failed")
            else:
                _mark_host_success(host_name)
            total_checked += int(host_result.get("checked", 0))
            total_updated += int(host_result.get("updated", 0))
            total_already_ok += int(host_result.get("already_ok", 0))
            total_not_found += int(host_result.get("not_found", 0))
            total_traffic_fixed += int(host_result.get("traffic_fixed", 0))
            total_ip_limit_fixed += int(host_result.get("ip_limit_fixed", 0))
            total_errors += int(host_result.get("errors", 0))

    logger.info(
        "Scheduler: DB enforce finished. checked=%s updated=%s already_ok=%s not_found=%s traffic_fixed=%s ip_limit_fixed=%s expired_preserved=%s errors=%s",
        total_checked,
        total_updated,
        total_already_ok,
        total_not_found,
        total_traffic_fixed,
        total_ip_limit_fixed,
        total_expired_preserved,
        total_errors,
    )


def _load_xui_panel_snapshot(host: dict):
    """Load one panel snapshot in a worker thread.

    ``login_to_host`` and the py3xui inbound client use blocking ``requests``
    internally, including retry sleeps. Keep the complete network operation out
    of the scheduler's asyncio event loop.
    """
    api, inbound = xui_api.login_to_host(
        host_url=host["host_url"],
        username=host["host_username"],
        password=host["host_pass"],
        inbound_id=host["host_inbound_id"],
        api_token=host.get("api_token"),
    )
    if not api or not inbound:
        return None

    full_inbound_details = api.inbound.get_by_id(inbound.id)
    if not full_inbound_details or not getattr(
        full_inbound_details, "settings", None
    ):
        return None
    return full_inbound_details


async def sync_keys_with_panels():
    logger.info("Scheduler: Starting sync with XUI panels...")
    total_affected_records = 0
    failed_hosts = []  # Collect failed hosts for summary log

    all_hosts = await asyncio.to_thread(database.get_all_hosts, True)
    if not all_hosts:
        logger.info("Scheduler: No hosts configured in the database. Sync skipped.")
        return

    for host in all_hosts:
        host_name = host["host_name"]
        if _is_host_in_failure_backoff(host_name):
            logger.debug(
                "Scheduler: Host '%s' is in failure backoff; sync skipped.",
                host_name,
            )
            continue

        try:
            full_inbound_details = await asyncio.wait_for(
                asyncio.to_thread(_load_xui_panel_snapshot, host),
                timeout=_PANEL_SNAPSHOT_TIMEOUT_SECONDS,
            )
            if not full_inbound_details:
                _mark_host_failure(host_name, "panel sync login/inbound lookup failed")
                failed_hosts.append(host_name)
                continue
            _mark_host_success(host_name)

            clients_on_server = {
                client.email: client
                for client in (full_inbound_details.settings.clients or [])
                if getattr(client, "email", None)
            }
            logger.info(
                f"Scheduler: Found {len(clients_on_server)} clients on the '{host_name}' panel."
            )

            keys_in_db = await asyncio.to_thread(database.get_keys_for_host, host_name)

            for db_key in keys_in_db:
                key_email = db_key["key_email"]
                expiry_date = time_utils.parse_iso_to_msk(db_key["expiry_date"])
                if not expiry_date:
                    logger.error(
                        f"Scheduler: Invalid expiry date for key '{key_email}': {db_key.get('expiry_date')}"
                    )
                    continue

                server_client = clients_on_server.pop(key_email, None)

                if server_client:
                    await asyncio.to_thread(database.purge_missing_key, key_email)
                    # Determine country flag based on server name
                    country_flag = xui_api.get_country_flag_by_host(host_name)
                    # Clean server name
                    clean_server_name = (
                        host_name.replace(" ", "")
                        .encode("ascii", "ignore")
                        .decode("ascii")
                    )
                    clean_server_name = "".join(
                        c for c in clean_server_name if c.isalnum() or c == "_"
                    ).lstrip("_")
                    server_remark = f"{country_flag}{clean_server_name}"

                    # Generate fresh connection string
                    new_connection_string = xui_api.get_connection_string(
                        full_inbound_details,
                        server_client.id,
                        host["host_url"],
                        remark=server_remark,
                    )

                    # Compare expiry times directly (no reset field logic)
                    server_expiry_ms = int(
                        getattr(server_client, "expiry_time", 0) or 0
                    )
                    local_expiry_dt = expiry_date
                    local_expiry_ms = int(local_expiry_dt.timestamp() * 1000)

                    # Update if expiry changed OR connection string needs update (e.g. flag changed)
                    current_db_string = db_key.get("connection_string")
                    connection_string_changed = (
                        new_connection_string
                        and not xui_api.connection_strings_equivalent(
                            new_connection_string, current_db_string
                        )
                    )
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
                        if connection_string_changed:
                            await asyncio.to_thread(
                                database.update_key_connection_string,
                                db_key["key_id"],
                                new_connection_string,
                            )
                            total_affected_records += 1
                        continue

                    expiry_diff_ms = server_expiry_ms - local_expiry_ms
                    if expiry_diff_ms < -1000:
                        logger.warning(
                            "Scheduler: Panel expiry for key '%s' on host '%s' is earlier than DB "
                            "(panel=%s, db=%s). Keeping DB expiry as source of truth.",
                            key_email,
                            host_name,
                            server_expiry_ms,
                            local_expiry_ms,
                        )
                        if connection_string_changed:
                            await asyncio.to_thread(
                                database.update_key_connection_string,
                                db_key["key_id"],
                                new_connection_string,
                            )
                            total_affected_records += 1
                    elif abs(expiry_diff_ms) > 1000 or connection_string_changed:
                        new_expiry_date_dt = time_utils.from_timestamp_ms(
                            server_expiry_ms
                        )
                        connection_string_to_store = (
                            new_connection_string
                            if connection_string_changed
                            else current_db_string
                        )
                        await asyncio.to_thread(
                            database.update_key_info,
                            db_key["key_id"],
                            new_expiry_date_dt,
                            connection_string_to_store,
                        )

                        # Also sync UUID if changed (rare but possible)
                        if db_key["xui_client_uuid"] != server_client.id:
                            await asyncio.to_thread(
                                database.update_key_status_from_server,
                                key_email,
                                server_client,
                            )

                        total_affected_records += 1
                        logger.info(
                            f"Scheduler: Synced key '{key_email}' for host '{host_name}'."
                        )
                else:
                    # Soft-delete: mark missing, recheck next cycle before removal
                    now_ts = time_utils.get_msk_now().isoformat()
                    await asyncio.to_thread(
                        database.mark_key_missing, key_email, now_ts, host_name
                    )
                    logger.warning(
                        f"Scheduler: Key '{key_email}' for host '{host_name}' not found on server. Marked missing for recheck."
                    )
                    total_affected_records += 1

            if clients_on_server:
                for orphan_email in clients_on_server.keys():
                    logger.warning(
                        f"Scheduler: Found orphan client '{orphan_email}' on host '{host_name}' that is not tracked by the bot."
                    )

        except asyncio.TimeoutError:
            _mark_host_failure(host_name, "panel snapshot timed out")
            failed_hosts.append(host_name)
            logger.warning(
                "Scheduler: Panel snapshot timed out for host '%s' after %s seconds.",
                host_name,
                _PANEL_SNAPSHOT_TIMEOUT_SECONDS,
            )
        except Exception as e:
            _mark_host_failure(host_name, "unexpected panel sync error")
            logger.error(
                f"Scheduler: An unexpected error occurred while processing host '{host_name}': {e}",
                exc_info=True,
            )

    # Log summary of failed hosts (single line instead of multiple errors)
    if failed_hosts:
        logger.warning(
            f"Scheduler: {len(failed_hosts)} host(s) unavailable: {', '.join(failed_hosts)}"
        )

    logger.info(
        f"Scheduler: Sync with XUI panels finished. Total records affected: {total_affected_records}."
    )


async def cleanup_old_notifications():
    """Delete sent_notifications older than 30 days to keep DB size manageable."""
    try:
        await asyncio.to_thread(database.cleanup_notifications, days_to_keep=30)
    except Exception as e:
        logger.error(f"Scheduler: Failed to cleanup old notifications: {e}")


async def auto_provision_new_hosts_for_global_users():
    """
    Auto-provision keys on new hosts for all users with active global subscriptions.

    This function is called periodically by the scheduler to ensure that when
    new hosts are added via the web admin panel, existing users with global
    subscriptions automatically get keys on the new hosts.
    """
    logger.info(
        "Scheduler: Checking for new hosts to auto-provision for global users..."
    )

    # Get all enabled hosts
    all_hosts = await asyncio.to_thread(database.get_all_hosts, True)
    if not all_hosts:
        logger.debug("Scheduler: No enabled hosts found.")
        return

    enabled_host_names = {
        h.get("host_name")
        for h in all_hosts
        if h.get("host_name") and h.get("host_name") != "ALL"
    }
    if not enabled_host_names:
        logger.debug("Scheduler: No regular enabled hosts found.")
        return

    try:
        global_plan_ids = await asyncio.to_thread(database.get_global_plan_ids)
        current_global_plan_ids = await asyncio.to_thread(
            database.get_global_plan_ids, False
        )
    except Exception as e:
        logger.error(f"Scheduler: Failed to get global plans: {e}")
        return

    if not global_plan_ids:
        logger.debug(
            "Scheduler: No global plan IDs configured. Paid auto-provision will be skipped; active trials can still be checked."
        )

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
            # Filter active global access keys. Paid global keys and trial keys
            # both represent access to all enabled XUI hosts.
            now = time_utils.get_msk_now()
            active_paid_global_keys = []
            active_trial_keys = []
            active_xui_keys = []
            for k in user_keys:
                try:
                    expiry = time_utils.parse_iso_to_msk(k.get("expiry_date"))
                    if not expiry or expiry <= now:
                        continue
                    if k.get("service_type", "xui") != "xui":
                        continue
                    active_xui_keys.append(k)
                    plan_id = int(k.get("plan_id") or 0)
                    if plan_id == 0:
                        active_trial_keys.append(k)
                    elif database.is_global_xui_key(k, global_plan_ids):
                        active_paid_global_keys.append(k)
                except (ValueError, TypeError):
                    continue

            provisioning_source_keys = active_paid_global_keys or active_trial_keys
            if not provisioning_source_keys:
                continue  # No active global access for this user

            # Count any active XUI key on the host as existing access. This
            # avoids duplicate panel clients and also lets trial self-heal.
            existing_hosts = {
                k.get("host_name") for k in active_xui_keys if k.get("host_name")
            }

            # Find missing hosts
            missing_hosts = enabled_host_names - existing_hosts
            if not missing_hosts:
                continue  # User has keys on all hosts

            # Calculate target expiry from the soonest-expiring source key.
            try:
                min_expiry_dt = min(
                    time_utils.parse_iso_to_msk(k["expiry_date"])
                    for k in provisioning_source_keys
                    if time_utils.parse_iso_to_msk(k.get("expiry_date"))
                )
                remaining_seconds = int((min_expiry_dt - now).total_seconds())
            except (ValueError, TypeError):
                min_expiry_dt = None
                remaining_seconds = 0

            if remaining_seconds <= 0 or not min_expiry_dt:
                logger.warning(
                    f"Scheduler: User {user_id} has global subscription but no valid expiry. Skipping."
                )
                continue

            target_expiry_ms = time_utils.get_timestamp_ms(min_expiry_dt)

            # Pick deterministic global plan id for paid users. Trial keys stay
            # plan_id=0 so later paid conversion still recognizes them as trial.
            if active_paid_global_keys:
                legacy_plan_ids = set()
                for key in active_paid_global_keys:
                    try:
                        candidate_plan_id = int(key.get("plan_id") or 0)
                    except (TypeError, ValueError):
                        candidate_plan_id = 0
                    if candidate_plan_id > 0:
                        legacy_plan_ids.add(candidate_plan_id)
                current_user_plan_ids = legacy_plan_ids & current_global_plan_ids
                if current_user_plan_ids:
                    first_global_plan_id = int(min(current_user_plan_ids))
                elif legacy_plan_ids:
                    first_global_plan_id = int(min(legacy_plan_ids))
                elif current_global_plan_ids:
                    first_global_plan_id = int(min(current_global_plan_ids))
                else:
                    first_global_plan_id = 0
            else:
                first_global_plan_id = 0

            logger.info(
                f"Scheduler: User {user_id} has global access. Missing hosts: {missing_hosts}. Auto-provisioning..."
            )
            total_users_processed += 1

            # Provision keys on missing hosts
            for host_name in missing_hosts:
                if _is_host_in_failure_backoff(host_name):
                    logger.debug(
                        "Scheduler: Host '%s' is in failure backoff; auto-provision skipped.",
                        host_name,
                    )
                    continue
                try:
                    email = f"user{user_id}-global-{_host_slug(host_name)}"
                    logger.info(
                        f"Scheduler: Auto-provisioning key for user {user_id} on host '{host_name}' with email '{email}'"
                    )

                    # Create key on host
                    res = await asyncio.wait_for(
                        xui_api.create_or_update_key_on_host_absolute_expiry(
                            host_name=host_name,
                            email=email,
                            target_expiry_ms=target_expiry_ms,
                            telegram_id=str(user_id),
                        ),
                        timeout=provision_timeout,
                    )

                    if res:
                        # Persist to database
                        existing_key = await asyncio.to_thread(
                            database.get_key_by_email, res["email"]
                        )
                        if existing_key:
                            await asyncio.to_thread(
                                database.update_key_by_email,
                                key_email=res["email"],
                                host_name=host_name,
                                xui_client_uuid=res["client_uuid"],
                                expiry_timestamp_ms=res["expiry_timestamp_ms"],
                                connection_string=res.get("connection_string"),
                                plan_id=first_global_plan_id,
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
                                plan_id=first_global_plan_id,
                            )
                        total_keys_created += 1
                        _mark_host_success(host_name)
                        logger.info(
                            f"Scheduler: Successfully created key for user {user_id} on host '{host_name}'"
                        )
                    else:
                        _mark_host_failure(host_name, "auto-provision returned no result")
                        logger.error(
                            f"Scheduler: Failed to create key for user {user_id} on host '{host_name}'"
                        )
                        total_errors += 1

                except asyncio.TimeoutError:
                    _mark_host_failure(host_name, "auto-provision timed out")
                    logger.error(
                        f"Scheduler: Timeout provisioning key for user {user_id} on host '{host_name}' "
                        f"after {provision_timeout}s"
                    )
                    total_errors += 1
                except Exception as e:
                    _mark_host_failure(host_name, str(e))
                    logger.error(
                        f"Scheduler: Error provisioning key for user {user_id} on host '{host_name}': {e}"
                    )
                    total_errors += 1

        except Exception as e:
            logger.error(
                f"Scheduler: Error processing user {user_id} for auto-provision: {e}"
            )
            total_errors += 1

    logger.info(
        f"Scheduler: Auto-provision finished. Users processed: {total_users_processed}, Keys created: {total_keys_created}, Errors: {total_errors}"
    )


async def refresh_xui_host_health() -> dict:
    """Refresh cached read-only load metrics without delaying subscriptions."""
    hosts = await asyncio.to_thread(database.get_all_hosts, True)
    hosts = [host for host in hosts if host.get("host_name")]
    result = {"checked": 0, "healthy": 0, "failed": 0}
    if not hosts:
        return result

    semaphore = asyncio.Semaphore(_HOST_HEALTH_MAX_CONCURRENCY)
    host_groups: dict[str, list[dict]] = {}
    for host in hosts:
        host_groups.setdefault(str(host.get("host_url") or host["host_name"]), []).append(
            host
        )

    async def _probe(group_hosts: list[dict]) -> None:
        host_name = group_hosts[0]["host_name"]
        result["checked"] += len(group_hosts)
        try:
            async with semaphore:
                metrics = await asyncio.wait_for(
                    host_health.collect_host_health(host_name),
                    timeout=_HOST_HEALTH_TIMEOUT_SECONDS,
                )
            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        database.update_xui_host_health,
                        group_host["host_name"],
                        metrics,
                    )
                    for group_host in group_hosts
                )
            )
            if metrics.get("is_available") and metrics.get("xray_running"):
                result["healthy"] += len(group_hosts)
            else:
                result["failed"] += len(group_hosts)
        except asyncio.TimeoutError:
            result["failed"] += len(group_hosts)
            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        database.record_xui_host_health_failure,
                        group_host["host_name"],
                        "health probe timed out",
                    )
                    for group_host in group_hosts
                )
            )
            logger.warning(
                "Scheduler: Health probe timed out for host '%s'.", host_name
            )
        except Exception as e:
            result["failed"] += len(group_hosts)
            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        database.record_xui_host_health_failure,
                        group_host["host_name"],
                        f"{type(e).__name__}: health probe failed",
                    )
                    for group_host in group_hosts
                )
            )
            logger.warning(
                "Scheduler: Health probe failed for host '%s': %s",
                host_name,
                e,
            )

    await asyncio.gather(*(_probe(group) for group in host_groups.values()))
    logger.info(
        "Scheduler: Host health refresh finished. checked=%s healthy=%s failed=%s",
        result["checked"],
        result["healthy"],
        result["failed"],
    )
    return result


async def monitor_xui_ip_limits(bot: Bot | None = None) -> dict:
    """Collect per-client IP counts once per physical panel and process breaches."""
    result = {
        "panels": 0,
        "keys": 0,
        "warnings": 0,
        "enforced": 0,
        "resolved": 0,
        "errors": 0,
    }
    if not _bool_setting("ip_limit_enabled", default=True):
        return result

    limit_count = _int_setting("ip_limit_max_ips", 10, 1, 100)
    grace_hours = _int_setting("ip_limit_warning_grace_hours", 24, 1, 168)
    hosts = await asyncio.to_thread(database.get_all_hosts, True)
    if not hosts:
        return result

    # Several logical locations may point to different inbounds on one panel.
    # The bulk IP endpoint is panel-wide, so query each physical panel once.
    host_groups: dict[str, list[dict]] = {}
    for host in hosts:
        group_key = str(host.get("host_url") or host.get("host_name") or "")
        host_groups.setdefault(group_key, []).append(host)

    now = time_utils.get_msk_now()
    semaphore = asyncio.Semaphore(_IP_LIMIT_MAX_CONCURRENCY)
    observations: list[dict] = []

    async def _collect(group: list[dict]) -> None:
        representative = group[0]
        keys_by_email: dict[str, dict] = {}
        for host in group:
            host_name = host.get("host_name")
            if not host_name:
                continue
            host_keys = await asyncio.to_thread(
                database.get_keys_for_host, host_name
            )
            for key in host_keys:
                email = key.get("key_email")
                expiry = time_utils.parse_iso_to_msk(key.get("expiry_date"))
                if not email or not expiry or expiry <= now:
                    continue
                keys_by_email[str(email)] = key

        if not keys_by_email:
            return

        try:
            async with semaphore:
                panel_result = await asyncio.wait_for(
                    xui_api.get_client_ip_counts(
                        representative["host_name"], set(keys_by_email)
                    ),
                    timeout=_IP_LIMIT_PANEL_TIMEOUT_SECONDS,
                )
        except Exception as e:
            result["errors"] += 1
            logger.warning(
                "Scheduler: IP-limit collection failed for panel group '%s': %s",
                representative.get("host_name"),
                e,
            )
            return

        result["panels"] += 1
        fail2ban = panel_result.get("fail2ban") or {}
        if not fail2ban.get("usable"):
            result["errors"] += 1
            logger.warning(
                "Scheduler: IP limiting is not usable on panel group '%s'; "
                "observations skipped to avoid promising unenforced limits.",
                representative.get("host_name"),
            )
            return

        counts = panel_result.get("counts") or {}
        for email, key in keys_by_email.items():
            observations.append(
                {
                    "key_id": key.get("key_id"),
                    "user_id": key.get("user_id"),
                    "host_name": key.get("host_name"),
                    "key_email": email,
                    "ip_count": int(counts.get(email, 0) or 0),
                }
            )

    await asyncio.gather(*(_collect(group) for group in host_groups.values()))
    result["keys"] = len(observations)
    actions = await asyncio.to_thread(
        database.process_xui_ip_limit_observations,
        observations,
        limit_count,
        grace_hours,
    )
    result["warnings"] = len(actions["warnings"])
    result["enforced"] = len(actions["enforced"])
    result["resolved"] = len(actions["resolved"])

    warnings_by_user: dict[int, list[dict]] = {}
    for warning in actions["warnings"]:
        warnings_by_user.setdefault(int(warning["user_id"]), []).append(warning)

    for user_id, user_warnings in warnings_by_user.items():
        if bot is None:
            continue
        details = "\n".join(
            f"• <b>{html.escape(str(item['host_name']))}</b>: "
            f"{item['ip_count']} IP при лимите {item['limit_count']}"
            for item in user_warnings[:10]
        )
        message = (
            "⚠️ <b>Предупреждение о подключениях</b>\n\n"
            "Обнаружено превышение количества одновременно используемых "
            f"IP-адресов:\n{details}\n\n"
            "Сейчас доступ ещё не ограничен. Пожалуйста, отключите лишние "
            f"устройства в течение {grace_hours} ч. После этого 3x-ui будет "
            "автоматически отключать подключения сверх лимита.\n\n"
            "Если это ваши устройства или мобильная сеть часто меняет IP, "
            "напишите в поддержку."
        )
        try:
            await bot.send_message(user_id, message, parse_mode="HTML")
            for warning in user_warnings:
                await asyncio.to_thread(
                    database.mark_xui_ip_limit_warning_result,
                    warning["key_id"],
                    None,
                )
        except TelegramForbiddenError as e:
            for warning in user_warnings:
                await asyncio.to_thread(
                    database.mark_xui_ip_limit_warning_result,
                    warning["key_id"],
                    f"Telegram delivery forbidden: {e}",
                )
        except Exception as e:
            result["errors"] += 1
            logger.warning(
                "Scheduler: Could not send IP-limit warning for user %s (%s key(s)): %s",
                user_id,
                len(user_warnings),
                e,
            )

    if actions["enforced"]:
        logger.warning(
            "Scheduler: IP limit moved to enforcement for %s key(s).",
            len(actions["enforced"]),
        )
    logger.info(
        "Scheduler: IP-limit monitor finished. panels=%s keys=%s warnings=%s "
        "enforced=%s resolved=%s errors=%s",
        result["panels"],
        result["keys"],
        result["warnings"],
        result["enforced"],
        result["resolved"],
        result["errors"],
    )
    return result


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
                    fixed = result.get("fixed", 0)
                    status = result.get("status", "unknown")

                    if fixed > 0:
                        logger.info(
                            f"XTLS sync for '{host_name}': {fixed} clients fixed. Status: {status}"
                        )
                        total_fixed += fixed
                    elif status == "success":
                        logger.debug(f"XTLS sync for '{host_name}': no fixes needed.")
                    else:
                        logger.warning(f"XTLS sync for '{host_name}': status={status}")

            if total_fixed > 0:
                logger.info(
                    f"Periodic XTLS sync completed: {total_fixed} total clients fixed across all hosts"
                )
            else:
                logger.debug(
                    "Periodic XTLS sync completed: all clients have correct settings"
                )
        else:
            logger.warning(f"Unexpected XTLS sync result format: {sync_results}")

    except Exception as e:
        logger.error(
            f"Scheduler: Failed to perform periodic XTLS sync: {e}", exc_info=True
        )


async def enforce_mtg_proxies_state() -> None:
    """
    Enforce MTG proxy enabled/disabled state from DB.
    - Expired keys: stop proxy on panel (safety net; panel auto-stops too).
    - Active keys:  start proxy on panel (ensures post-renewal keys are running).
    """
    mtg_keys = await asyncio.to_thread(database.get_keys_by_service_type, "mtg")
    if not mtg_keys:
        return

    now = time_utils.get_msk_now()
    enabled_hosts = {
        h["host_name"]
        for h in await asyncio.to_thread(database.get_all_mtg_hosts, True)
    }
    global_limit = asyncio.Semaphore(_MTG_ENFORCE_MAX_CONCURRENCY)
    host_limits: dict[str, asyncio.Semaphore] = {}
    attempted_by_host: dict[str, int] = {}
    failed_by_host: dict[str, int] = {}

    async def enforce_one(
        host_name: str,
        proxy_name: str,
        node_id: int,
        should_enable: bool,
    ) -> None:
        host_limit = host_limits.setdefault(
            host_name, asyncio.Semaphore(_MTG_ENFORCE_PER_HOST_CONCURRENCY)
        )
        attempted_by_host[host_name] = attempted_by_host.get(host_name, 0) + 1
        try:
            async with host_limit:
                async with global_limit:
                    operation = (
                        mtg_api.enable_proxy_for_user
                        if should_enable
                        else mtg_api.disable_proxy_for_user
                    )
                    succeeded = await asyncio.wait_for(
                        operation(host_name, proxy_name, node_id),
                        timeout=_MTG_ENFORCE_REQUEST_TIMEOUT_SECONDS,
                    )
            if not succeeded:
                raise RuntimeError("panel rejected the state change")
        except asyncio.TimeoutError:
            failed_by_host[host_name] = failed_by_host.get(host_name, 0) + 1
            logger.warning(
                "Scheduler: MTG state enforce timed out for proxy '%s' on host '%s' after %s seconds.",
                proxy_name,
                host_name,
                _MTG_ENFORCE_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as e:
            failed_by_host[host_name] = failed_by_host.get(host_name, 0) + 1
            logger.warning(
                "Scheduler: MTG state enforce failed for proxy '%s' on host '%s': %s",
                proxy_name,
                host_name,
                e,
            )

    pending = []

    for key in mtg_keys:
        host_name = key.get("host_name")
        if host_name not in enabled_hosts:
            continue
        if _is_mtg_host_in_failure_backoff(host_name):
            continue
        proxy_name = key.get("key_email")
        node_id_str = key.get("xui_client_uuid")
        if not proxy_name or not node_id_str:
            continue
        try:
            node_id = int(node_id_str)
        except (ValueError, TypeError):
            continue

        expiry_date = time_utils.parse_iso_to_msk(key.get("expiry_date"))
        if not expiry_date:
            continue

        pending.append(
            enforce_one(
                host_name,
                proxy_name,
                node_id,
                should_enable=expiry_date > now,
            )
        )

    if pending:
        await asyncio.gather(*pending)

    for host_name, attempted in attempted_by_host.items():
        failures = failed_by_host.get(host_name, 0)
        # Back off the whole host only when every attempted operation failed.
        # An isolated missing/broken proxy must not delay enforcement for the
        # other proxies on an otherwise healthy panel.
        if failures == attempted:
            _mark_mtg_host_failure(
                host_name, f"{failures} of {attempted} state changes failed"
            )
        else:
            _mark_mtg_host_success(host_name)

    logger.debug(
        "Scheduler: MTG proxy state enforce finished. attempted=%s failures=%s",
        sum(attempted_by_host.values()),
        sum(failed_by_host.values()),
    )


async def process_pending_yookassa_payments(bot: Bot) -> None:
    """Safety net for YooKassa webhooks: verify and fulfill paid pending payments."""
    shop_id = await asyncio.to_thread(database.get_setting, "yookassa_shop_id")
    secret_key = await asyncio.to_thread(database.get_setting, "yookassa_secret_key")
    if not shop_id or not secret_key:
        return

    pending = await asyncio.to_thread(database.get_pending_yookassa_transactions, 20)
    if not pending:
        return

    logger.info(
        "Scheduler: Checking %s pending YooKassa payment(s) via API.", len(pending)
    )

    Configuration.account_id = shop_id
    Configuration.secret_key = secret_key

    for tx in pending:
        payment_id = str(tx.get("payment_id") or "").strip()
        if not payment_id:
            continue

        try:
            payment = await asyncio.to_thread(Payment.find_one, payment_id)
        except Exception as e:
            logger.warning(
                "Scheduler: YooKassa API check failed for payment %s: %s",
                payment_id,
                e,
            )
            continue

        payment_status = getattr(payment, "status", None)
        if payment_status == "canceled":
            metadata = dict(tx.get("metadata_dict") or {})
            metadata["payment_method"] = "YooKassa"
            metadata["provider_payment_id"] = payment_id
            marked = await asyncio.to_thread(
                database.mark_pending_transaction_status,
                payment_id,
                "canceled",
                metadata=metadata,
                payment_method="YooKassa",
            )
            if marked:
                logger.info(
                    "Scheduler: YooKassa payment %s is canceled by provider; marked local transaction as canceled.",
                    payment_id,
                )
            continue

        if not payment or payment_status != "succeeded":
            logger.debug(
                "Scheduler: YooKassa payment %s is not succeeded yet (status=%s).",
                payment_id,
                payment_status,
            )
            continue

        metadata = {}
        if hasattr(payment, "metadata") and payment.metadata:
            metadata = dict(payment.metadata)
        if not metadata:
            metadata = dict(tx.get("metadata_dict") or {})
        if not metadata:
            logger.error(
                "Scheduler: YooKassa payment %s succeeded but has no metadata.",
                payment_id,
            )
            continue

        api_amount = getattr(getattr(payment, "amount", None), "value", None)
        api_currency = getattr(getattr(payment, "amount", None), "currency", None)
        meta_price = metadata.get("price")
        if api_amount and meta_price is not None:
            try:
                if abs(float(api_amount) - float(meta_price)) > 0.01:
                    logger.error(
                        "Scheduler: YooKassa amount mismatch for %s: API=%s metadata=%s",
                        payment_id,
                        api_amount,
                        meta_price,
                    )
                    continue
            except (TypeError, ValueError):
                logger.warning(
                    "Scheduler: Could not compare YooKassa amount for %s: API=%s metadata=%s",
                    payment_id,
                    api_amount,
                    meta_price,
                )

        metadata["provider_payment_id"] = payment_id
        metadata["payment_method"] = "YooKassa"

        reserved_metadata = await asyncio.to_thread(
            database.reserve_pending_transaction,
            payment_id,
            metadata,
            payment_method="YooKassa",
            amount_currency=float(api_amount) if api_amount is not None else None,
            currency_name=api_currency,
        )
        if reserved_metadata is None:
            logger.info(
                "Scheduler: YooKassa payment %s is no longer pending; skipping.",
                payment_id,
            )
            continue

        processed_ok = False
        try:
            processed_ok = await handlers.process_successful_payment(
                bot, reserved_metadata
            )
        except Exception as e:
            logger.error(
                "Scheduler: YooKassa fallback processing failed for %s: %s",
                payment_id,
                e,
                exc_info=True,
            )

        finalized = await asyncio.to_thread(
            database.finalize_reserved_transaction,
            payment_id,
            success=bool(processed_ok),
            metadata=reserved_metadata,
            payment_method="YooKassa",
            amount_currency=float(api_amount) if api_amount is not None else None,
            currency_name=api_currency,
        )
        if not finalized:
            logger.error(
                "Scheduler: Failed to finalize YooKassa fallback transaction %s after processing=%s.",
                payment_id,
                processed_ok,
            )
            continue

        if processed_ok:
            await asyncio.to_thread(
                database.set_webhook_processed, "yookassa", payment_id
            )
            logger.info(
                "Scheduler: YooKassa fallback fulfilled payment %s successfully.",
                payment_id,
            )
        else:
            logger.warning(
                "Scheduler: YooKassa payment %s is paid but fulfillment failed; will retry later.",
                payment_id,
            )


async def process_pending_paid_provider_retries(bot: Bot) -> None:
    """Retry already-paid Stars/CryptoBot fulfillments that failed after payment confirmation."""
    pending = await asyncio.to_thread(database.get_pending_paid_retry_transactions, 20)
    if not pending:
        return

    logger.info(
        "Scheduler: Retrying %s paid non-YooKassa fulfillment(s).", len(pending)
    )

    for tx in pending:
        payment_id = str(tx.get("payment_id") or "").strip()
        if not payment_id:
            continue

        metadata = dict(tx.get("metadata_dict") or {})
        method = metadata.get("payment_method") or tx.get("payment_method") or "Unknown"
        currency_name = tx.get("currency_name")
        amount_currency = tx.get("amount_currency")

        reserved_metadata = await asyncio.to_thread(
            database.reserve_pending_transaction,
            payment_id,
            metadata,
            payment_method=method,
            amount_currency=amount_currency,
            currency_name=currency_name,
        )
        if reserved_metadata is None:
            logger.info(
                "Scheduler: paid retry transaction %s is no longer pending; skipping.",
                payment_id,
            )
            continue

        processed_ok = False
        try:
            processed_ok = await handlers.process_successful_payment(
                bot, reserved_metadata
            )
        except Exception as e:
            logger.error(
                "Scheduler: paid retry fulfillment failed for %s: %s",
                payment_id,
                e,
                exc_info=True,
            )

        finalized = await asyncio.to_thread(
            database.finalize_reserved_transaction,
            payment_id,
            success=bool(processed_ok),
            metadata=reserved_metadata,
            payment_method=method,
            amount_currency=amount_currency,
            currency_name=currency_name,
        )
        if not finalized:
            logger.error(
                "Scheduler: Failed to finalize paid retry transaction %s after processing=%s.",
                payment_id,
                processed_ok,
            )
            continue

        if processed_ok:
            logger.info(
                "Scheduler: paid retry fulfilled transaction %s successfully.",
                payment_id,
            )
        else:
            logger.warning(
                "Scheduler: paid retry transaction %s still failed; will retry later.",
                payment_id,
            )


async def recover_interrupted_payment_processing() -> None:
    recovered = await asyncio.to_thread(
        database.recover_stale_processing_transactions, 15
    )
    if recovered:
        logger.warning(
            "Scheduler: Recovered %s interrupted payment fulfillment(s).",
            recovered,
        )


async def expire_stale_unpaid_stars_payments() -> None:
    expired_count = await asyncio.to_thread(
        database.expire_stale_unpaid_stars_transactions, 48
    )
    if expired_count:
        logger.info(
            "Scheduler: Marked %s stale unpaid Telegram Stars invoice(s) as expired.",
            expired_count,
        )


async def periodic_subscription_check(bot_controller: BotController):
    logger.info("Scheduler has been started.")
    await asyncio.sleep(10)

    # Track when XTLS sync was last performed (run every 5 min instead of every CHECK_INTERVAL)
    xtls_sync_interval = 300  # 5 minutes
    last_xtls_sync_time = 0
    last_host_health_time = 0
    last_ip_limit_time = 0

    while True:
        try:
            # Always enforce access state by DB even if panel_sync_enabled is disabled.
            await enforce_clients_state_from_db()

            # Enforce MTG proxy states (start active, stop expired)
            await enforce_mtg_proxies_state()

            if _bool_setting("panel_sync_enabled", default=False):
                await sync_keys_with_panels()
            else:
                logger.debug(
                    "Scheduler: panel sync disabled (panel_sync_enabled=false)."
                )

            # Run cleanup once per cycle (or could be less frequent, but this is cheap)
            await cleanup_old_notifications()

            # Auto-provision new hosts for global subscription users
            await auto_provision_new_hosts_for_global_users()

            # Run XTLS sync separately on its own interval (every 5 minutes)
            current_time = time.time()
            if (
                current_time - last_host_health_time
                >= _HOST_HEALTH_INTERVAL_SECONDS
            ):
                await refresh_xui_host_health()
                last_host_health_time = time.time()

            if current_time - last_ip_limit_time >= _IP_LIMIT_INTERVAL_SECONDS:
                warning_bot = None
                if bot_controller.get_status().get("is_running"):
                    warning_bot = bot_controller.get_bot_instance()
                await monitor_xui_ip_limits(warning_bot)
                last_ip_limit_time = time.time()

            if (
                _bool_setting("xtls_sync_enabled", default=False)
                and current_time - last_xtls_sync_time >= xtls_sync_interval
            ):
                await periodic_xtls_sync()
                last_xtls_sync_time = current_time
            elif current_time - last_xtls_sync_time >= xtls_sync_interval:
                logger.debug("Scheduler: XTLS sync disabled (xtls_sync_enabled=false).")

            if bot_controller.get_status().get("is_running"):
                bot = bot_controller.get_bot_instance()
                if bot:
                    await recover_interrupted_payment_processing()
                    await process_pending_yookassa_payments(bot)
                    await process_pending_paid_provider_retries(bot)
                    await expire_stale_unpaid_stars_payments()
                    await check_expiring_subscriptions(bot)
                    await check_idle_onboarding_users(bot)
                else:
                    logger.warning(
                        "Scheduler: Bot is marked as running, but instance is not available."
                    )
            else:
                logger.info("Scheduler: Bot is stopped, skipping user notifications.")

        except Exception as e:
            logger.error(
                f"Scheduler: An unhandled error occurred in the main loop: {e}",
                exc_info=True,
            )

        logger.info(
            f"Scheduler: Cycle finished. Next check in {CHECK_INTERVAL_SECONDS} seconds."
        )
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

import asyncio
import html
import json
import logging
from json import JSONDecodeError

from aiogram import Bot, F, Router, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramMigrateToChat
from aiogram.filters import CommandStart

from shop_bot.data_manager import database
from shop_bot.utils import time_utils

logger = logging.getLogger(__name__)

SUPPORT_GROUP_ID = None
_ticket_locks: dict[int, asyncio.Lock] = {}


def _ticket_lock(user_id: int) -> asyncio.Lock:
    lock = _ticket_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _ticket_locks[user_id] = lock
    return lock


def _error_text(error: Exception) -> str:
    return str(error).strip()


def _is_missing_topic_error(error: Exception) -> bool:
    text = _error_text(error).lower()
    return _is_missing_topic_text(text) and isinstance(error, TelegramBadRequest)


def _is_missing_topic_text(text: str) -> bool:
    markers = (
        "thread not found",
        "message thread not found",
        "topic_deleted",
        "topic deleted",
        "topic_closed",
        "topic closed",
        "forum topic is closed",
    )
    return any(marker in text for marker in markers)


def _is_unavailable_chat_error(error: Exception) -> bool:
    text = _error_text(error).lower()
    markers = (
        "chat not found",
        "forbidden",
        "bot was kicked",
        "have no rights",
        "topic creation failed",
        "not enough rights",
    )
    return any(marker in text for marker in markers)


def _message_text_for_log(message: types.Message) -> str:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return f"[{message.content_type}]"


def _format_history_message(entry: dict) -> str:
    created = time_utils.parse_iso_to_msk(entry.get("created_at"))
    timestamp = time_utils.format_msk(created, "%d.%m %H:%M") if created else "N/A"
    direction = (
        "Пользователь" if entry.get("direction") == "user_to_support" else "Саппорт"
    )
    text = (
        entry.get("text") or ""
    ).strip() or f"[{entry.get('message_type') or 'message'}]"
    return f"[{timestamp}] {direction}: {text}"


async def get_user_summary(user_id: int, username: str) -> str:
    keys = database.get_user_keys(user_id)
    latest_transaction = database.get_latest_transaction(user_id)
    now = time_utils.get_msk_now()

    active_keys = []
    for key in keys:
        expiry_dt = time_utils.parse_iso_to_msk(key.get("expiry_date"))
        if expiry_dt and expiry_dt > now:
            active_keys.append((key, expiry_dt))

    summary_parts = [
        f"<b>Новый тикет от пользователя:</b> @{html.escape(username)} (ID: <code>{user_id}</code>)\n"
    ]

    if active_keys:
        summary_parts.append("<b>🔑 Активные ключи:</b>")
        for key, expiry_dt in active_keys:
            expiry = time_utils.format_msk(expiry_dt, "%d.%m.%Y")
            summary_parts.append(
                f"- <code>{html.escape(key['key_email'])}</code> (до {expiry} на хосте {html.escape(key['host_name'])})"
            )
    else:
        summary_parts.append("<b>🔑 Активные ключи:</b> Нет")

    if latest_transaction:
        summary_parts.append("\n<b>💸 Последняя транзакция:</b>")
        try:
            metadata = json.loads(latest_transaction.get("metadata", "{}") or "{}")
        except JSONDecodeError:
            metadata = {}
        plan_name = metadata.get("plan_name", "N/A")
        price = latest_transaction.get("amount_rub", "N/A")
        tx_date = time_utils.parse_iso_to_msk(latest_transaction.get("created_date"))
        date = time_utils.format_msk(tx_date, "%d.%m.%Y") if tx_date else "N/A"
        summary_parts.append(f"- {html.escape(str(plan_name))} за {price} RUB ({date})")
    else:
        summary_parts.append("\n<b>💸 Последняя транзакция:</b> Нет")

    return "\n".join(summary_parts)


async def _send_ticket_context(
    bot: Bot,
    user_id: int,
    username: str,
    thread_id: int,
    restored: bool,
):
    summary_text = await get_user_summary(user_id, username)
    await bot.send_message(
        chat_id=SUPPORT_GROUP_ID,
        message_thread_id=thread_id,
        text=summary_text,
        parse_mode=ParseMode.HTML,
    )

    if not restored:
        return

    history = database.get_support_ticket(user_id)
    if not history:
        return

    messages = database.get_support_messages(history["ticket_id"], limit=10)
    if not messages:
        return

    lines = ["Тикет был восстановлен после потери Telegram topic. Последние сообщения:"]
    for entry in messages:
        lines.append(_format_history_message(entry))

    payload = "\n".join(lines)
    if len(payload) > 3500:
        payload = payload[:3497] + "..."

    await bot.send_message(
        chat_id=SUPPORT_GROUP_ID,
        message_thread_id=thread_id,
        text=payload,
    )


async def _create_or_restore_thread(
    bot: Bot,
    user_id: int,
    username: str,
    restored: bool,
) -> int | None:
    global SUPPORT_GROUP_ID

    if not SUPPORT_GROUP_ID:
        logger.error("Support bot: SUPPORT_GROUP_ID is not configured.")
        return None

    thread_name = f"Тикет от @{username} ({user_id})"
    try:
        new_thread = await bot.create_forum_topic(
            chat_id=SUPPORT_GROUP_ID, name=thread_name
        )
    except TelegramMigrateToChat as migrate_error:
        new_id = getattr(migrate_error, "migrate_to_chat_id", None)
        if new_id is None:
            logger.error("Support group migration did not include a target chat ID.")
            return None
        database.update_setting("support_group_id", str(new_id))
        SUPPORT_GROUP_ID = new_id
        new_thread = await bot.create_forum_topic(
            chat_id=SUPPORT_GROUP_ID, name=thread_name
        )
    except Exception as error:
        logger.error(
            f"Failed to create support thread for user {user_id}: {error}",
            exc_info=True,
        )
        database.mark_support_ticket_waiting_reopen(user_id, _error_text(error))
        return None

    thread_id = new_thread.message_thread_id
    database.bind_support_thread(
        user_id=user_id,
        thread_id=thread_id,
        username=username,
        reopened=restored,
    )
    await _send_ticket_context(
        bot=bot,
        user_id=user_id,
        username=username,
        thread_id=thread_id,
        restored=restored,
    )
    logger.info(
        f"{'Restored' if restored else 'Created'} support thread {thread_id} for user {user_id}"
    )
    return thread_id


async def _probe_thread(bot: Bot, thread_id: int) -> tuple[bool, str | None]:
    global SUPPORT_GROUP_ID

    try:
        probe_msg = await bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            text=".",
            message_thread_id=thread_id,
            disable_notification=True,
        )
        try:
            await bot.delete_message(
                chat_id=SUPPORT_GROUP_ID, message_id=probe_msg.message_id
            )
        except Exception:
            pass

        if probe_msg.message_thread_id != thread_id:
            return False, (
                f"Telegram redirected probe from topic {thread_id} "
                f"to {probe_msg.message_thread_id}"
            )
        return True, None
    except TelegramMigrateToChat as migrate_error:
        new_id = getattr(migrate_error, "migrate_to_chat_id", None)
        if new_id is None:
            return False, "Support group migration did not include a target chat ID"
        database.update_setting("support_group_id", str(new_id))
        SUPPORT_GROUP_ID = new_id
        return await _probe_thread(bot, thread_id)
    except Exception as error:
        return False, _error_text(error)


async def _deliver_user_message(
    bot: Bot, message: types.Message, thread_id: int
) -> tuple[bool, str | None]:
    global SUPPORT_GROUP_ID

    probe_ok, probe_error = await _probe_thread(bot, thread_id)
    if not probe_ok:
        return False, probe_error

    try:
        await bot.copy_message(
            chat_id=SUPPORT_GROUP_ID,
            from_chat_id=message.from_user.id,
            message_id=message.message_id,
            message_thread_id=thread_id,
        )
        return True, None
    except TelegramMigrateToChat as migrate_error:
        new_id = getattr(migrate_error, "migrate_to_chat_id", None)
        if new_id is None:
            return False, "Support group migration did not include a target chat ID"
        database.update_setting("support_group_id", str(new_id))
        SUPPORT_GROUP_ID = new_id
        return await _deliver_user_message(bot, message, thread_id)
    except Exception as error:
        return False, _error_text(error)


async def _is_support_operator(bot: Bot, chat_id: int, message: types.Message) -> bool:
    if message.sender_chat and message.sender_chat.id == chat_id:
        return True

    if not message.from_user:
        return False

    if message.from_user.id == bot.id:
        return True

    try:
        member = await bot.get_chat_member(chat_id, message.from_user.id)
    except Exception as error:
        logger.warning(
            "Failed to verify support operator permissions for user %s: %s",
            message.from_user.id,
            _error_text(error),
        )
        return False

    return getattr(member, "status", None) in {"administrator", "creator"}


async def _ensure_thread_and_deliver(
    bot: Bot,
    message: types.Message,
    user_id: int,
    username: str,
) -> tuple[bool, str | None]:
    ticket = database.ensure_support_ticket(user_id, username=username)
    if not ticket:
        return False, "Не удалось подготовить тикет в БД"

    thread_id = ticket.get("current_thread_id") or database.get_support_thread_id(
        user_id
    )
    if thread_id:
        delivered, error_text = await _deliver_user_message(bot, message, thread_id)
        if delivered:
            return True, None
        if error_text and _is_missing_topic_text(error_text.lower()):
            database.mark_support_ticket_waiting_reopen(user_id, error_text)
            thread_id = None
        elif error_text and "redirected probe" in error_text.lower():
            database.mark_support_ticket_waiting_reopen(user_id, error_text)
            thread_id = None
        else:
            return False, error_text

    if not thread_id:
        restored = bool(
            ticket
            and (
                ticket.get("last_message_at")
                or ticket.get("reopen_count")
                or ticket.get("status") in {"waiting_reopen", "closed"}
            )
        )
        thread_id = await _create_or_restore_thread(
            bot=bot,
            user_id=user_id,
            username=username,
            restored=restored,
        )
        if not thread_id:
            return False, "Не удалось создать новый Telegram topic для тикета"

    delivered, error_text = await _deliver_user_message(bot, message, thread_id)
    if delivered:
        return True, None

    if error_text and (
        "redirected probe" in error_text.lower()
        or "thread not found" in error_text.lower()
        or "topic" in error_text.lower()
    ):
        database.mark_support_ticket_waiting_reopen(user_id, error_text)
        replacement_thread_id = await _create_or_restore_thread(
            bot=bot,
            user_id=user_id,
            username=username,
            restored=True,
        )
        if replacement_thread_id:
            delivered, error_text = await _deliver_user_message(
                bot, message, replacement_thread_id
            )
            if delivered:
                return True, None

    if error_text and (
        _is_missing_topic_text(error_text.lower())
        or "redirected probe" in error_text.lower()
    ):
        database.mark_support_ticket_waiting_reopen(user_id, error_text)
    return False, error_text


def _is_service_topic_event(message: types.Message) -> bool:
    return any(
        getattr(message, attr, None) is not None
        for attr in (
            "forum_topic_created",
            "forum_topic_closed",
            "forum_topic_reopened",
            "forum_topic_edited",
            "general_forum_topic_hidden",
            "general_forum_topic_unhidden",
            "write_access_allowed",
        )
    )


def get_support_router() -> Router:
    support_router = Router()

    @support_router.message(CommandStart())
    async def handle_start(message: types.Message, bot: Bot):
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name

        async with _ticket_lock(user_id):
            ticket = database.ensure_support_ticket(user_id, username=username)
            thread_id = ticket.get("current_thread_id") if ticket else None
            if not thread_id:
                restored = bool(
                    ticket
                    and (
                        ticket.get("last_message_at")
                        or ticket.get("reopen_count")
                        or ticket.get("status") in {"waiting_reopen", "closed"}
                    )
                )
                thread_id = await _create_or_restore_thread(
                    bot=bot,
                    user_id=user_id,
                    username=username,
                    restored=restored,
                )
                if not thread_id:
                    await message.answer(
                        "Извините, служба поддержки временно недоступна."
                    )
                    return

        await message.answer(
            "Напишите ваш вопрос, и администратор скоро с вами свяжется."
        )

    @support_router.message(F.chat.type == "private")
    async def from_user_to_admin(message: types.Message, bot: Bot):
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name

        ticket = database.ensure_support_ticket(user_id, username=username)
        if not ticket:
            await message.answer(
                "⚠️ Не удалось подготовить тикет поддержки. Пожалуйста, попробуйте позже."
            )
            return

        message_log_id = database.log_support_message(
            ticket_id=ticket["ticket_id"],
            user_id=user_id,
            direction="user_to_support",
            sender_telegram_id=user_id,
            sender_name=username,
            message_type=message.content_type,
            text=_message_text_for_log(message),
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            source_thread_id=database.get_support_thread_id(user_id),
        )

        async with _ticket_lock(user_id):
            success, error_text = await _ensure_thread_and_deliver(
                bot=bot,
                message=message,
                user_id=user_id,
                username=username,
            )

        if success:
            if message_log_id:
                database.update_support_message_delivery(message_log_id, "delivered")
            return

        if message_log_id:
            database.update_support_message_delivery(
                message_log_id, "failed", error_text or "delivery failed"
            )

        logger.error(
            f"Failed to route support message from user {user_id}: {error_text}"
        )
        await message.answer(
            "⚠️ Не удалось отправить сообщение в поддержку. Сообщение сохранено, попробуйте ещё раз позже."
        )

    @support_router.message(F.message_thread_id)
    async def from_admin_to_user(message: types.Message, bot: Bot):
        current_group_id = SUPPORT_GROUP_ID or int(
            database.get_setting("support_group_id") or 0
        )
        if message.chat.id != current_group_id:
            return

        thread_id = message.message_thread_id
        ticket = database.get_support_ticket_by_thread(thread_id)

        if _is_service_topic_event(message):
            if ticket and getattr(message, "forum_topic_closed", None) is not None:
                database.mark_support_ticket_closed(
                    ticket["user_id"], "Forum topic was closed in Telegram"
                )
            elif ticket and getattr(message, "forum_topic_reopened", None) is not None:
                database.bind_support_thread(
                    ticket["user_id"],
                    thread_id,
                    username=ticket.get("username"),
                    reopened=False,
                )
            return

        if message.from_user and message.from_user.id == bot.id:
            return

        if not await _is_support_operator(bot, current_group_id, message):
            logger.warning(
                "Ignoring support reply from non-operator in chat %s thread %s",
                current_group_id,
                thread_id,
            )
            return

        if not ticket:
            logger.warning(
                f"Support topic message in thread {thread_id} has no linked ticket."
            )
            return

        user_id = ticket["user_id"]
        sender_name = message.from_user.full_name if message.from_user else "Support"
        message_log_id = database.log_support_message(
            ticket_id=ticket["ticket_id"],
            user_id=user_id,
            direction="support_to_user",
            sender_telegram_id=message.from_user.id if message.from_user else None,
            sender_name=sender_name,
            message_type=message.content_type,
            text=_message_text_for_log(message),
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            source_thread_id=thread_id,
        )

        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            if message_log_id:
                database.update_support_message_delivery(message_log_id, "delivered")
        except Exception as error:
            error_text = _error_text(error)
            logger.error(f"Failed to send message to user {user_id}: {error_text}")
            if message_log_id:
                database.update_support_message_delivery(
                    message_log_id, "failed", error_text
                )
            await message.reply(
                "❌ Не удалось доставить сообщение пользователю. История сохранена в БД."
            )

    return support_router

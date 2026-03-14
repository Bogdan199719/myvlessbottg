import logging
import json
from json import JSONDecodeError

from aiogram import Bot, Router, F, types
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode

from shop_bot.data_manager import database

logger = logging.getLogger(__name__)

SUPPORT_GROUP_ID = None

router = Router()


async def get_user_summary(user_id: int, username: str) -> str:
    keys = database.get_user_keys(user_id)
    latest_transaction = database.get_latest_transaction(user_id)

    summary_parts = [
        f"<b>Новый тикет от пользователя:</b> @{username} (ID: <code>{user_id}</code>)\n"
    ]

    if keys:
        summary_parts.append("<b>🔑 Активные ключи:</b>")
        for key in keys:
            if key.get('expiry_date') and len(key['expiry_date']) > 10:
                 expiry = key['expiry_date'].split(' ')[0]
            else:
                 expiry = "Invalid Date"

            summary_parts.append(f"- <code>{key['key_email']}</code> (до {expiry} на хосте {key['host_name']})")
    else:
        summary_parts.append("<b>🔑 Активные ключи:</b> Нет")

    if latest_transaction:
        summary_parts.append("\n<b>💸 Последняя транзакция:</b>")
        try:
            metadata = json.loads(latest_transaction.get('metadata', '{}') or '{}')
        except JSONDecodeError:
            metadata = {}
        plan_name = metadata.get('plan_name', 'N/A')
        price = latest_transaction.get('amount_rub', 'N/A')
        date = latest_transaction.get('created_date', '').split(' ')[0]
        summary_parts.append(f"- {plan_name} за {price} RUB ({date})")
    else:
        summary_parts.append("\n<b>💸 Последняя транзакция:</b> Нет")

    return "\n".join(summary_parts)


def get_support_router() -> Router:
    support_router = Router()

    @support_router.message(CommandStart())
    async def handle_start(message: types.Message, bot: Bot):
        global SUPPORT_GROUP_ID
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        
        thread_id = database.get_support_thread_id(user_id)
        
        if not thread_id:
            if not SUPPORT_GROUP_ID:
                logger.error("Support bot: SUPPORT_GROUP_ID is not configured!")
                await message.answer("Извините, служба поддержки временно недоступна.")
                return

            try:
                thread_name = f"Тикет от @{username} ({user_id})"
                new_thread = await bot.create_forum_topic(chat_id=SUPPORT_GROUP_ID, name=thread_name)
                thread_id = new_thread.message_thread_id
                
                database.add_support_thread(user_id, thread_id)
                
                summary_text = await get_user_summary(user_id, username)
                await bot.send_message(
                    chat_id=SUPPORT_GROUP_ID,
                    message_thread_id=thread_id,
                    text=summary_text,
                    parse_mode=ParseMode.HTML
                )
                logger.info(f"Created new support thread {thread_id} for user {user_id}")
                
            except Exception as e:
                # Автоматическая обработка миграции группы в супергруппу
                from aiogram.exceptions import TelegramMigrateToChat
                if isinstance(e, TelegramMigrateToChat):
                    new_id = getattr(e, "migrate_to_chat_id", None)
                    if new_id is None:
                        logger.error("Support group migration exception did not include a target chat ID.")
                        await message.answer("Не удалось создать тикет в поддержке. Пожалуйста, попробуйте позже.")
                        return
                    logger.info(f"Support group migrated to supergroup. Updating ID to {new_id}")
                    database.update_setting("support_group_id", str(new_id))
                    SUPPORT_GROUP_ID = new_id
                    # Повторная попытка с новым ID
                    try:
                        new_thread = await bot.create_forum_topic(chat_id=SUPPORT_GROUP_ID, name=thread_name)
                        thread_id = new_thread.message_thread_id
                        database.add_support_thread(user_id, thread_id)
                        summary_text = await get_user_summary(user_id, username)
                        await bot.send_message(
                            chat_id=SUPPORT_GROUP_ID,
                            message_thread_id=thread_id,
                            text=summary_text,
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception as retry_e:
                        logger.error(f"Failed retry after migration: {retry_e}")
                else:
                    logger.error(f"Failed to create support thread for user {user_id}: {e}", exc_info=True)
                    await message.answer("Не удалось создать тикет в поддержке. Пожалуйста, попробуйте позже.")
                    return

        await message.answer("Напишите ваш вопрос, и администратор скоро с вами свяжется.")

    @support_router.message(F.chat.type == "private")
    async def from_user_to_admin(message: types.Message, bot: Bot):
        global SUPPORT_GROUP_ID
        user_id = message.from_user.id
        thread_id = database.get_support_thread_id(user_id)
        
        async def AttemptCopy(target_thread_id):
            global SUPPORT_GROUP_ID
            try:
                # PROBE: Send a temporary invisible-ish message to check where it lands.
                # copy_message returns MessageId which doesn't have thread info.
                # send_message returns Message which DOES have thread info.
                probe_msg = await bot.send_message(
                    chat_id=SUPPORT_GROUP_ID, 
                    text=".", 
                    message_thread_id=target_thread_id,
                    disable_notification=True
                )
                
                # Check if probe landed in correct thread
                real_thread_id = probe_msg.message_thread_id
                
                # Cleanup probe immediately
                try:
                    await bot.delete_message(chat_id=SUPPORT_GROUP_ID, message_id=probe_msg.message_id)
                except Exception:
                    pass

                if target_thread_id is not None and real_thread_id != target_thread_id:
                    logger.warning(f"Probe message fell back to General/Other (Expected {target_thread_id}, got {real_thread_id}). Thread is effectively dead.")
                    return False

                # If probe succeeded, safe to copy
                await bot.copy_message(
                    chat_id=SUPPORT_GROUP_ID,
                    from_chat_id=user_id,
                    message_id=message.message_id,
                    message_thread_id=target_thread_id
                )
                return True

            except Exception as e:
                from aiogram.exceptions import TelegramMigrateToChat, TelegramBadRequest
                if isinstance(e, TelegramMigrateToChat):
                    new_id = getattr(e, "migrate_to_chat_id", None)
                    if new_id is None:
                        logger.error("Support group migration during copy did not include a target chat ID.")
                        return None
                    logger.info(f"Support group migrated to {new_id} during copy. Updating...")
                    database.update_setting("support_group_id", str(new_id))
                    SUPPORT_GROUP_ID = new_id
                    return await AttemptCopy(target_thread_id)
                
                error_msg = str(e).lower()
                if isinstance(e, TelegramBadRequest) and ("thread not found" in error_msg or "topic_deleted" in error_msg):
                    logger.warning(f"Thread {target_thread_id} explicitly not found/deleted. Recreating...")
                    database.delete_support_thread(user_id)
                    return False
                
                logger.error(f"Failed to copy message to admin: {e}")
                return None

        # Validate thread existence if we have one
        if thread_id and SUPPORT_GROUP_ID:
            try:
                # Probe the thread state using 'typing' action.
                # If thread is deleted, this usually raises TelegramBadRequest
                await bot.send_chat_action(chat_id=SUPPORT_GROUP_ID, message_thread_id=thread_id, action="typing")
            except Exception as e:
                logger.warning(f"Thread {thread_id} probe failed (likely deleted): {e}")
                database.delete_support_thread(user_id)
                thread_id = None # Force recreation
        
        # If no valid thread (or just deleted), create new one
        if not thread_id:
             logger.info(f"No valid thread for user {user_id}. Creating new ticket.")
             await handle_start(message, bot)
             thread_id = database.get_support_thread_id(user_id)

        # Now attempt to copy logic
        success = False
        if thread_id and SUPPORT_GROUP_ID:
             # We assume thread is valid now (freshly created or probed)
             # But we keep AttemptCopy logic just in case probing missed something
             success = await AttemptCopy(thread_id)
        
        if not success:
             await message.answer("⚠️ Не удалось отправить сообщение в поддержку. Пожалуйста, попробуйте еще раз позже.")
             logger.error(f"Failed to copy message from user {user_id} to thread {thread_id}")

    @support_router.message(F.message_thread_id)
    async def from_admin_to_user(message: types.Message, bot: Bot):
        # Используем динамическую проверку ID группы из настроек, на случай если переменная еще не обновилась
        current_group_id = SUPPORT_GROUP_ID or int(database.get_setting("support_group_id") or 0)
        
        if message.chat.id != current_group_id:
            return

        thread_id = message.message_thread_id
        user_id = database.get_user_id_by_thread(thread_id)
        
        if message.from_user.id == bot.id:
            return
            
        if user_id:
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
            except Exception as e:
                logger.error(f"Failed to send message to user {user_id}: {e}")
                await message.reply("❌ Не удалось доставить сообщение (возможно, бот заблокирован).")
    return support_router

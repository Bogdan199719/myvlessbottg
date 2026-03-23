from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from shop_bot.data_manager.database import get_user


def _is_ignorable_telegram_bad_request(error_text: str) -> bool:
    text = (error_text or "").lower()
    return (
        "query is too old" in text
        or "query id is invalid" in text
        or "response timeout expired" in text
        or "message is not modified" in text
        or "specified new message content and reply markup are exactly the same" in text
    )


class SafeCallbackMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramBadRequest as e:
            if _is_ignorable_telegram_bad_request(str(e)):
                # Ignore benign Telegram callback/edit races to reduce log noise.
                return
            raise


class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        user_data = get_user(user.id)
        if user_data and user_data.get("is_banned"):
            ban_message_text = "Вы заблокированы и не можете использовать этого бота."
            if isinstance(event, CallbackQuery):
                await event.answer(ban_message_text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(ban_message_text)
            return

        return await handler(event, data)

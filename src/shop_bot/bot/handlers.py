import logging
import uuid
import qrcode
import aiohttp
import re
import hashlib
import json
import base64
import asyncio
import sqlite3

from functools import wraps
from yookassa import Payment
from io import BytesIO
from datetime import datetime, timedelta
from aiosend import CryptoPay
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, InvalidOperation
from typing import Dict
from shop_bot.utils import time_utils
from aiogram import Bot, Router, F, types, html
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.bot import keyboards
from shop_bot.modules import xui_api
from shop_bot.modules import mtg_api
from shop_bot.data_manager.database import (
    get_user,
    add_new_key,
    get_user_keys,
    update_user_stats,
    register_user_if_not_exists,
    get_next_key_number,
    get_key_by_id,
    update_key_info,
    update_key_plan_id,
    set_trial_used,
    set_terms_agreed,
    get_setting,
    get_all_hosts,
    get_plans_for_host,
    get_plan_by_id,
    log_transaction,
    get_referral_count,
    add_to_referral_balance,
    create_pending_transaction,
    reserve_pending_transaction,
    finalize_reserved_transaction,
    get_all_users,
    set_referral_balance,
    set_referral_balance_all,
    DB_FILE,
    get_user_paid_keys,
    get_user_trial_keys,
    set_pending_payment,
    clear_all_pending_payments,
    get_or_create_subscription_token,
    get_all_mtg_hosts,
    get_payment_rules_for_context,
    create_p2p_request,
    get_p2p_request,
    get_active_p2p_request_for_user,
    mark_p2p_request_submitted,
    delete_p2p_request,
)

from shop_bot.config import (
    get_profile_text,
    get_vpn_active_text,
    VPN_INACTIVE_TEXT,
    VPN_NO_DATA_TEXT,
    get_key_info_text,
    CHOOSE_PAYMENT_METHOD_MESSAGE,
    get_purchase_success_text,
    get_proxy_purchase_success_text,
    get_proxy_info_text,
    CHOOSE_PROXY_HOST_MESSAGE,
)

TELEGRAM_BOT_USERNAME = None
ADMIN_ID = None


def _build_subscription_link(domain: str | None, token: str | None) -> str | None:
    domain_value = (domain or "").strip()
    token_value = (token or "").strip()
    if not domain_value or not token_value:
        return None
    if not domain_value.startswith(("http://", "https://")):
        domain_value = f"https://{domain_value}"
    return f"{domain_value.rstrip('/')}/sub/{token_value}"


def _build_payment_context(
    service_type: str | None, host_name: str | None
) -> str | None:
    """Build context_key string for payment rule lookup."""
    if not service_type or not host_name:
        return None
    if service_type == "mtg":
        return f"mtg:{host_name}"
    if host_name == "ALL":
        return "global"
    return f"xui:{host_name}"


def _get_vpn_purchase_plans() -> list[dict]:
    hosts = get_all_hosts(only_enabled=True)
    plans_for_display: list[dict] = []

    enable_global = get_setting("enable_global_plans")
    is_global_enabled = True if not enable_global or enable_global == "true" else False

    if is_global_enabled:
        for plan in get_plans_for_host("ALL", service_type="xui"):
            plan_copy = dict(plan)
            plan_copy["host_name"] = "ALL"
            plan_copy["display_name"] = (
                f"🌍 {plan_copy['plan_name']} · Все серверы — {plan_copy['price']:.0f} ₽"
            )
            plans_for_display.append(plan_copy)

    for host in hosts:
        host_name = host["host_name"]
        for plan in get_plans_for_host(host_name, service_type="xui"):
            plan_copy = dict(plan)
            plan_copy["host_name"] = host_name
            plan_copy["display_name"] = (
                f"{plan_copy['plan_name']} · {host_name} — {plan_copy['price']:.0f} ₽"
            )
            plans_for_display.append(plan_copy)

    plans_for_display.sort(
        key=lambda item: (
            0 if item.get("host_name") == "ALL" else 1,
            int(item.get("months") or 0),
            str(item.get("host_name") or ""),
            str(item.get("plan_name") or ""),
        )
    )
    return plans_for_display


async def _show_vpn_purchase_plans(
    callback: types.CallbackQuery, action: str = "new", key_id: int = 0
):
    plans = _get_vpn_purchase_plans()
    if not plans:
        await callback.message.edit_text(
            "❌ В данный момент нет доступных тарифов для покупки."
        )
        return

    await callback.message.edit_text(
        "Выберите тариф VPN:",
        reply_markup=keyboards.create_plans_keyboard(
            plans=plans, action=action, host_name="", key_id=key_id
        ),
    )


logger = logging.getLogger(__name__)
admin_router = Router()
user_router = Router()
_LAST_MAIN_MENU_MESSAGE_ID: dict[int, int] = {}


class KeyPurchase(StatesGroup):
    waiting_for_host_selection = State()
    waiting_for_plan_selection = State()


class Onboarding(StatesGroup):
    waiting_for_subscription_and_agreement = State()


class PaymentProcess(StatesGroup):
    waiting_for_email = State()
    waiting_for_payment_method = State()


def get_active_payment_methods(
    context_key: str | None = None, plan_id: int | None = None
) -> Dict[str, bool]:
    """
    Return active payment methods, optionally filtered by context rules.
    Priority (highest wins): plan rule > context (host/product) rule > global setting.
    context_key examples: 'global', 'xui:Сервер Riga', 'mtg:finland'
    """
    methods: Dict[str, bool] = {}

    # --- Global settings (base layer) ---
    if get_setting("yookassa_enabled") == "true":
        shop_id = get_setting("yookassa_shop_id")
        secret = get_setting("yookassa_secret_key")
        if shop_id and secret:
            methods["yookassa"] = True
    if get_setting("stars_enabled") == "true":
        methods["stars"] = True
    if get_setting("cryptobot_enabled") == "true":
        token = get_setting("cryptobot_token")
        if token:
            methods["cryptobot"] = True
    if get_setting("p2p_enabled") == "true":
        methods["p2p"] = True

    # --- Context rules (host/product layer) ---
    if context_key:
        ctx_rules = get_payment_rules_for_context(context_key)
        if ctx_rules is not None:
            for method, enabled in ctx_rules.items():
                if not enabled:
                    methods.pop(method, None)
                elif enabled and get_setting(f"{method}_enabled") == "true":
                    methods[method] = True

    # --- Plan rules (most specific, highest priority) ---
    if plan_id:
        plan_rules = get_payment_rules_for_context(f"plan:{plan_id}")
        if plan_rules is not None:
            for method, enabled in plan_rules.items():
                if not enabled:
                    methods.pop(method, None)
                elif enabled and get_setting(f"{method}_enabled") == "true":
                    methods[method] = True

    return methods


def has_active_global_subscription(active_paid_keys: list[dict]) -> bool:
    """Detect active global subscription based on global plan ids and non-expired keys."""
    try:
        global_plan_ids = {
            int(p["plan_id"])
            for p in get_plans_for_host("ALL", service_type="xui")
            if p.get("plan_id") is not None
        }
    except Exception as e:
        logger.warning(f"Error getting global plan IDs: {e}")
        global_plan_ids = set()

    if not global_plan_ids:
        return False

    for key in active_paid_keys:
        try:
            if int(key.get("plan_id", 0)) in global_plan_ids:
                return True
        except (ValueError, TypeError) as e:
            logger.debug(f"Error checking plan_id: {e}")
            continue
    return False


def get_active_trial_keys(user_id: int) -> list[dict]:
    now = time_utils.get_msk_now()
    active_keys: list[dict] = []
    for key in get_user_trial_keys(user_id):
        expiry_dt = time_utils.parse_iso_to_msk(key.get("expiry_date"))
        if expiry_dt and expiry_dt > now:
            active_keys.append(key)
    return _dedupe_paid_keys_by_host(active_keys)


def has_active_global_trial(active_trial_keys: list[dict]) -> bool:
    return bool(active_trial_keys)


def has_active_global_access(
    active_paid_keys: list[dict], active_trial_keys: list[dict] | None = None
) -> bool:
    return has_active_global_subscription(active_paid_keys) or has_active_global_trial(
        active_trial_keys or []
    )


def _find_existing_xui_key_for_host(user_id: int, host_name: str) -> dict | None:
    paid_match = None
    trial_match = None
    for key in get_user_keys(user_id):
        if key.get("service_type", "xui") != "xui":
            continue
        if key.get("host_name") != host_name:
            continue
        if int(key.get("plan_id", 0) or 0) > 0:
            paid_match = key
            break
        if trial_match is None:
            trial_match = key
    return paid_match or trial_match


def _dedupe_paid_keys_by_host(keys: list[dict]) -> list[dict]:
    """Keep a single paid key per host (the one with the latest expiry)."""
    best_by_host: dict[str, dict] = {}

    def _expiry_ts(raw_value) -> float:
        dt = time_utils.parse_iso_to_msk(raw_value)
        return dt.timestamp() if dt else 0.0

    for key in keys:
        host_name = key.get("host_name")
        if not host_name:
            continue
        prev = best_by_host.get(host_name)
        if not prev or _expiry_ts(key.get("expiry_date")) >= _expiry_ts(
            prev.get("expiry_date")
        ):
            best_by_host[host_name] = key

    return list(best_by_host.values())


def get_active_paid_keys(user_id: int) -> list[dict]:
    now = time_utils.get_msk_now()
    active_keys: list[dict] = []
    for key in get_user_paid_keys(user_id):
        expiry_dt = time_utils.parse_iso_to_msk(key.get("expiry_date"))
        if expiry_dt and expiry_dt > now:
            active_keys.append(key)
    return _dedupe_paid_keys_by_host(active_keys)


def _stars_is_pending_transaction(payment_id: str) -> bool:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM transactions WHERE payment_id = ?", (payment_id,)
            )
            row = cursor.fetchone()
            return bool(row and row[0] == "pending")
    except sqlite3.Error as e:
        logger.error(f"Stars: Failed to check pending transaction {payment_id}: {e}")
        return False


def _get_transaction_status(payment_id: str) -> str | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM transactions WHERE payment_id = ?", (payment_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except sqlite3.Error as e:
        logger.error(f"Failed to get transaction status for {payment_id}: {e}")
        return None


def _cryptobot_build_payload(payment_id: str) -> str:
    return json.dumps({"tx_id": payment_id}, separators=(",", ":"), ensure_ascii=True)


def _stars_complete_transaction(
    payment_id: str, paid_stars: int, telegram_payment_charge_id: str | None
) -> dict | None:
    try:
        if paid_stars <= 0:
            logger.error(
                f"Stars: Invalid paid_stars={paid_stars} for payment {payment_id}"
            )
            return None

        metadata = reserve_pending_transaction(
            payment_id,
            payment_method="Telegram Stars",
            amount_currency=int(paid_stars),
            currency_name="XTR",
        )
        if metadata is None:
            return None

        if telegram_payment_charge_id:
            metadata["telegram_payment_charge_id"] = telegram_payment_charge_id
        metadata["paid_stars"] = int(paid_stars)
        metadata["payment_method"] = "Telegram Stars"
        metadata["provider_payment_id"] = payment_id
        return metadata
    except Exception as e:
        logger.error(f"Stars: Failed to complete transaction {payment_id}: {e}")
        return None


def _extract_withdraw_command_user_id(
    message_text: str | None, command_name: str
) -> int | None:
    if not message_text:
        return None

    match = re.match(
        rf"^/{re.escape(command_name)}_(\d+)(?:@\w+)?$",
        message_text.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None

    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


class Broadcast(StatesGroup):
    waiting_for_message = State()
    waiting_for_button_option = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()
    waiting_for_confirmation = State()


class WithdrawStates(StatesGroup):
    waiting_for_details = State()


def is_valid_email(email: str) -> bool:
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return re.match(pattern, email) is not None


async def show_main_menu(message: types.Message, edit_message: bool = False):
    user_id = message.chat.id
    user_db_data = get_user(user_id)
    user_keys = get_user_keys(user_id)

    trial_available = not (user_db_data and user_db_data.get("trial_used"))
    is_admin = str(user_id) == str(get_setting("admin_telegram_id") or "")

    text = "🏠 <b>Главное меню</b>"
    keyboard = keyboards.create_main_menu_keyboard(user_keys, trial_available, is_admin)

    if edit_message:
        try:
            await message.edit_text(text, reply_markup=keyboard)
            _LAST_MAIN_MENU_MESSAGE_ID[user_id] = message.message_id
        except TelegramBadRequest:
            pass
    else:
        prev_menu_msg_id = _LAST_MAIN_MENU_MESSAGE_ID.get(user_id)
        if prev_menu_msg_id:
            try:
                await message.bot.delete_message(
                    chat_id=user_id, message_id=prev_menu_msg_id
                )
            except Exception:
                # Old menu message may already be deleted or not editable anymore.
                pass

        sent = await message.answer(text, reply_markup=keyboard)
        _LAST_MAIN_MENU_MESSAGE_ID[user_id] = sent.message_id


def registration_required(f):
    @wraps(f)
    async def decorated_function(event: types.Update, *args, **kwargs):
        user_id = event.from_user.id
        user_data = get_user(user_id)
        if user_data:
            try:
                return await f(event, *args, **kwargs)
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    return
                raise
        else:
            message_text = (
                "Пожалуйста, для начала работы со мной, отправьте команду /start"
            )
            if isinstance(event, types.CallbackQuery):
                await event.answer(message_text, show_alert=True)
            else:
                await event.answer(message_text)

    return decorated_function


def get_user_router() -> Router:
    user_router = Router()

    @user_router.message(CommandStart())
    async def start_handler(
        message: types.Message, state: FSMContext, bot: Bot, command: CommandObject
    ):
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        referrer_id = None

        if command.args and command.args.startswith("ref_"):
            try:
                potential_referrer_id = int(command.args.split("_")[1])
                if potential_referrer_id != user_id:
                    referrer_id = potential_referrer_id
                    logger.info(f"New user {user_id} was referred by {referrer_id}")
            except (IndexError, ValueError):
                logger.warning(f"Invalid referral code received: {command.args}")

        register_user_if_not_exists(user_id, username, referrer_id)
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        user_data = get_user(user_id)

        if user_data and user_data.get("agreed_to_terms"):
            await message.answer(
                f"👋 Снова здравствуйте, {html.bold(message.from_user.full_name)}!",
                reply_markup=keyboards.main_reply_keyboard,
            )
            await show_main_menu(message)
            return

        terms_url = get_setting("terms_url")
        privacy_url = get_setting("privacy_url")
        channel_url = get_setting("channel_url")

        if not channel_url or not terms_url or not privacy_url:
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        is_subscription_forced = get_setting("force_subscription") == "true"

        show_welcome_screen = (is_subscription_forced and channel_url) or (
            terms_url and privacy_url
        )

        if not show_welcome_screen:
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        welcome_parts = ["<b>Добро пожаловать!</b>\n"]

        if is_subscription_forced and channel_url:
            welcome_parts.append(
                "Для доступа ко всем функциям, пожалуйста, подпишитесь на наш канал.\n"
            )

        if terms_url and privacy_url:
            welcome_parts.append(
                "Также необходимо ознакомиться с нашими Условиями использования и Политикой конфиденциальности."
            )
        elif terms_url:
            welcome_parts.append(
                "Также необходимо ознакомиться и принять наши Условия использования."
            )
        elif privacy_url:
            welcome_parts.append(
                "Также необходимо ознакомиться с нашей Политикой конфиденциальности."
            )

        welcome_parts.append("\nПосле этого нажмите кнопку ниже.")
        final_text = "\n".join(welcome_parts)

        await message.answer(
            final_text,
            reply_markup=keyboards.create_welcome_keyboard(
                channel_url=channel_url,
                is_subscription_forced=is_subscription_forced,
                terms_url=terms_url,
                privacy_url=privacy_url,
            ),
            disable_web_page_preview=True,
        )
        await state.set_state(Onboarding.waiting_for_subscription_and_agreement)

    @user_router.callback_query(
        Onboarding.waiting_for_subscription_and_agreement,
        F.data == "check_subscription_and_agree",
    )
    async def check_subscription_handler(
        callback: types.CallbackQuery, state: FSMContext, bot: Bot
    ):
        user_id = callback.from_user.id
        channel_url = get_setting("channel_url")
        is_subscription_forced = get_setting("force_subscription") == "true"

        if not is_subscription_forced or not channel_url:
            await process_successful_onboarding(callback, state)
            return

        try:
            if "@" not in channel_url and "t.me/" not in channel_url:
                logger.error(
                    f"Неверный формат URL канала: {channel_url}. Пропускаем проверку подписки."
                )
                await process_successful_onboarding(callback, state)
                return

            channel_id = (
                "@" + channel_url.split("/")[-1]
                if "t.me/" in channel_url
                else channel_url
            )
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)

            if member.status in [
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            ]:
                await process_successful_onboarding(callback, state)
            else:
                await callback.answer(
                    "Вы еще не подписались на канал. Пожалуйста, подпишитесь и попробуйте снова.",
                    show_alert=True,
                )

        except Exception as e:
            logger.error(
                f"Ошибка при проверке подписки для user_id {user_id} на канал {channel_url}: {e}"
            )
            await callback.answer(
                "Не удалось проверить подписку. Убедитесь, что бот является администратором канала. Попробуйте позже.",
                show_alert=True,
            )

    @user_router.message(Onboarding.waiting_for_subscription_and_agreement)
    async def onboarding_fallback_handler(message: types.Message):
        await message.answer(
            "Пожалуйста, выполните требуемые действия и нажмите на кнопку в сообщении выше."
        )

    @user_router.message(F.text == "🏠 Главное меню")
    @registration_required
    async def main_menu_handler(message: types.Message, state: FSMContext):
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        await show_main_menu(message)

    @user_router.callback_query(F.data.startswith("global_qr_"))
    async def show_qr_token_handler(callback: types.CallbackQuery, bot: Bot):
        token = callback.data[len("global_qr_") :]
        domain = get_setting("domain")
        user_id = callback.from_user.id
        expected_token = get_or_create_subscription_token(user_id)
        active_paid_keys = get_active_paid_keys(user_id)
        active_trial_keys = get_active_trial_keys(user_id)

        if not domain:
            await callback.answer("Домен не настроен", show_alert=True)
            return

        if not has_active_global_access(active_paid_keys, active_trial_keys):
            await callback.answer("У вас нет активной VPN-подписки.", show_alert=True)
            return

        if not expected_token or token != expected_token:
            await callback.answer(
                "Ссылка подписки недействительна. Откройте профиль и получите новую.",
                show_alert=True,
            )
            return

        if not domain.startswith("http"):
            sub_link = f"https://{domain}/sub/{token}"
        else:
            sub_link = f"{domain}/sub/{token}"

        img = qrcode.make(sub_link)
        bio = BytesIO()
        img.save(bio, "PNG")
        bio.seek(0)

        await bot.send_photo(
            chat_id=callback.from_user.id,
            photo=types.BufferedInputFile(bio.getvalue(), filename="qrcode.png"),
            caption="📱 <b>QR-код для подписки</b>",
        )
        await callback.answer()

    @user_router.callback_query(F.data.startswith("global_link_"))
    async def show_link_token_handler(callback: types.CallbackQuery):
        token = callback.data[len("global_link_") :]
        domain = get_setting("domain")
        user_id = callback.from_user.id
        expected_token = get_or_create_subscription_token(user_id)
        active_paid_keys = get_active_paid_keys(user_id)
        active_trial_keys = get_active_trial_keys(user_id)

        if not domain:
            await callback.answer("Домен не настроен", show_alert=True)
            return

        if not has_active_global_access(active_paid_keys, active_trial_keys):
            await callback.answer("У вас нет активной VPN-подписки.", show_alert=True)
            return

        if not expected_token or token != expected_token:
            await callback.answer(
                "Ссылка подписки недействительна. Откройте профиль и получите новую.",
                show_alert=True,
            )
            return

        sub_link = _build_subscription_link(domain, token)
        if not sub_link:
            await callback.answer(
                "Не удалось сформировать ссылку подписки.", show_alert=True
            )
            return

        await callback.message.answer(
            "🔗 <b>Ссылка-подписка:</b>\n"
            f"<code>{sub_link}</code>\n\n"
            "<blockquote>📋 Скопируйте ссылку → откройте Happ → нажмите <b>+</b> → вставьте ссылку и при необходимости обновите подписку.</blockquote>",
            reply_markup=keyboards.create_global_link_keyboard(sub_link, token),
            disable_web_page_preview=True,
        )
        await callback.answer()

    @user_router.callback_query(F.data == "global_howto")
    async def howto_vless_global_handler(callback: types.CallbackQuery):
        await callback.message.edit_text(
            "<b>🌍 Что такое глобальная подписка</b>\n\n"
            "Это одна ссылка вида <code>https://.../sub/...</code>, в которой уже собраны все ваши серверы.\n"
            "Её удобно добавлять в <a href='https://www.happ.su/main/ru'>Happ</a> как одну подписку.\n\n"
            "<b>Как подключить:</b>\n"
            "1. Откройте в боте профиль и скопируйте ссылку подписки или покажите QR.\n"
            "2. Скопируйте ссылку подписки.\n"
            "3. Откройте Happ и нажмите <b>+</b>.\n"
            "4. Добавьте ссылку из буфера обмена.\n"
            "5. Нажмите <b>Обновить подписку</b>, если Happ попросит обновление.\n"
            "6. Выберите любой сервер из списка и подключитесь.\n\n"
            "<b>Важно:</b> обычный ключ <code>vless://</code> добавляет один сервер,\n"
            "а ссылка <code>https://.../sub/...</code> добавляет сразу всю подписку целиком.",
            reply_markup=keyboards.create_howto_vless_keyboard(),
        )

    @user_router.callback_query(F.data == "back_to_main_menu")
    @registration_required
    async def back_to_main_menu_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer()
        await state.clear()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_profile")
    @registration_required
    async def profile_handler_callback(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_db_data = get_user(user_id)
        all_keys = get_user_keys(user_id)
        paid_keys = get_user_paid_keys(user_id)
        trial_keys = get_user_trial_keys(user_id)
        if not user_db_data:
            await callback.answer(
                "Не удалось получить данные профиля.", show_alert=True
            )
            return
        username = html.bold(user_db_data.get("username", "Пользователь"))
        total_spent = user_db_data.get("total_spent", 0)
        now = time_utils.get_msk_now()

        # Separate xui VPN keys and MTG proxy keys
        xui_paid_keys = [k for k in paid_keys if k.get("service_type", "xui") == "xui"]
        mtg_keys = [k for k in all_keys if k.get("service_type") == "mtg"]

        active_paid_keys = []
        for key in xui_paid_keys:
            dt = time_utils.parse_iso_to_msk(key.get("expiry_date"))
            if dt and dt > now:
                active_paid_keys.append(key)
        active_paid_keys = _dedupe_paid_keys_by_host(active_paid_keys)

        active_trial_keys = get_active_trial_keys(user_id)

        # VPN status only from xui keys
        active_xui_keys = []
        for key in xui_paid_keys + trial_keys:
            dt = time_utils.parse_iso_to_msk(key.get("expiry_date"))
            if dt and dt > now:
                active_xui_keys.append(key)

        active_mtg_keys = []
        for key in mtg_keys:
            dt = time_utils.parse_iso_to_msk(key.get("expiry_date"))
            if dt and dt > now:
                active_mtg_keys.append(key)

        is_global_active = has_active_global_access(active_paid_keys, active_trial_keys)
        profile_vpn_link = None
        profile_link_label = None

        subscription_token = user_db_data.get("subscription_token")
        if not subscription_token:
            subscription_token = get_or_create_subscription_token(user_id)
        domain = get_setting("domain")

        if is_global_active:
            profile_vpn_link = _build_subscription_link(domain, subscription_token)
            if profile_vpn_link:
                profile_link_label = "Ссылка VPN подписки"
        else:
            candidate_keys = active_paid_keys or active_trial_keys
            primary_key = next(
                (
                    key
                    for key in candidate_keys
                    if (key.get("connection_string") or "").strip()
                ),
                None,
            )
            if primary_key:
                profile_vpn_link = primary_key["connection_string"].strip()
                profile_link_label = "VPN ключ"

        # ── Build profile card with blockquote sections ───────────────────────
        parts = [f"👤 <b>{username}</b>\n"]

        # VPN card
        if has_active_global_subscription(active_paid_keys):
            min_exp = min(
                time_utils.parse_iso_to_msk(k["expiry_date"])
                for k in active_paid_keys
                if k.get("expiry_date")
            )
            time_left = min_exp - now
            exp_str = time_utils.format_msk(min_exp, "%d.%m.%Y")
            parts.append(
                f"<blockquote>🔒 <b>VPN подписка</b>  ✅ активна\n"
                f"до {exp_str}  ·  осталось {time_left.days} дн.</blockquote>"
            )
        elif active_trial_keys:
            best_trial = max(
                active_trial_keys,
                key=lambda k: time_utils.parse_iso_to_msk(k["expiry_date"]),
            )
            exp_dt = time_utils.parse_iso_to_msk(best_trial["expiry_date"])
            exp_str = time_utils.format_msk(exp_dt, "%d.%m.%Y")
            time_left = exp_dt - now
            parts.append(
                f"<blockquote>🔒 <b>VPN подписка</b>  🎁 пробный\n"
                f"до {exp_str}  ·  осталось {time_left.days} дн.</blockquote>"
            )
        elif xui_paid_keys or trial_keys:
            parts.append("<blockquote>🔒 <b>VPN подписка</b>  ❌ истёкла</blockquote>")
        else:
            parts.append(
                "<blockquote>🔒 <b>VPN подписка</b>  — не приобретена</blockquote>"
            )

        # Proxy card
        if active_mtg_keys:
            best_proxy = max(
                active_mtg_keys,
                key=lambda k: time_utils.parse_iso_to_msk(k["expiry_date"]),
            )
            exp_dt = time_utils.parse_iso_to_msk(best_proxy["expiry_date"])
            exp_str = time_utils.format_msk(exp_dt, "%d.%m.%Y")
            time_left = exp_dt - now
            parts.append(
                f"<blockquote>📡 <b>Telegram Proxy</b>  ✅ активен\n"
                f"до {exp_str}  ·  осталось {time_left.days} дн.</blockquote>"
            )
        elif mtg_keys:
            parts.append("<blockquote>📡 <b>Telegram Proxy</b>  ❌ истёк</blockquote>")

        parts.append(
            f"\n💰 Потрачено: <b>{total_spent:,.0f} ₽</b>".replace(",", "\u202f")
        )
        if profile_vpn_link and profile_link_label:
            parts.append(
                f"\n🔗 <b>{profile_link_label}:</b>\n<code>{profile_vpn_link}</code>"
            )

        final_text = "\n".join(parts)

        # ── Keyboard: context-aware, minimal ─────────────────────────────────
        profile_kb = InlineKeyboardBuilder()

        if is_global_active and subscription_token:
            profile_kb.button(
                text="🔗 Ссылка VPN подписки",
                callback_data=f"global_link_{subscription_token}",
            )
        if active_paid_keys or active_trial_keys or xui_paid_keys or trial_keys:
            profile_kb.button(text="📦 Мои VPN подписки", callback_data="manage_keys")
        if mtg_keys:
            label = "📡 Мой Proxy" if active_mtg_keys else "📡 Продлить Proxy"
            profile_kb.button(text=label, callback_data="show_proxy_keys")
        profile_kb.button(text="🏠 В меню", callback_data="back_to_main_menu")

        profile_kb.adjust(1)
        await callback.message.edit_text(
            final_text, reply_markup=profile_kb.as_markup()
        )

    @user_router.callback_query(F.data == "start_broadcast")
    @registration_required
    async def start_broadcast_handler(callback: types.CallbackQuery, state: FSMContext):
        admin_id = get_setting("admin_telegram_id")
        if not admin_id or str(callback.from_user.id) != str(admin_id):
            await callback.answer("У вас нет прав.", show_alert=True)
            return

        await callback.answer()
        await callback.message.edit_text(
            "Пришлите сообщение, которое вы хотите разослать всем пользователям.\n"
            "Вы можете использовать форматирование (<b>жирный</b>, <i>курсив</i>).\n"
            "Также поддерживаются фото, видео и документы.\n",
            reply_markup=keyboards.create_broadcast_cancel_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_message)

    @user_router.message(Broadcast.waiting_for_message)
    async def broadcast_message_received_handler(
        message: types.Message, state: FSMContext
    ):
        message_dict = message.model_dump(mode="json", exclude_unset=True)
        await state.update_data(message_to_send=json.dumps(message_dict))

        await message.answer(
            "Сообщение получено. Хотите добавить к нему кнопку со ссылкой?",
            reply_markup=keyboards.create_broadcast_options_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_button_option)

    @user_router.callback_query(
        Broadcast.waiting_for_button_option, F.data == "broadcast_add_button"
    )
    async def add_button_prompt_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer()
        await callback.message.edit_text(
            "Хорошо. Теперь отправьте мне текст для кнопки.",
            reply_markup=keyboards.create_broadcast_cancel_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_button_text)

    @user_router.message(Broadcast.waiting_for_button_text)
    async def button_text_received_handler(message: types.Message, state: FSMContext):
        await state.update_data(button_text=message.text)
        await message.answer(
            "Текст кнопки получен. Теперь отправьте ссылку (URL), куда она будет вести.",
            reply_markup=keyboards.create_broadcast_cancel_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_button_url)

    @user_router.message(Broadcast.waiting_for_button_url)
    async def button_url_received_handler(
        message: types.Message, state: FSMContext, bot: Bot
    ):
        url_to_check = message.text

        is_valid = await is_url_reachable(url_to_check)

        if not is_valid:
            await message.answer(
                "❌ **Ссылка не прошла проверку.**\n\n"
                "Пожалуйста, убедитесь, что:\n"
                "1. Ссылка начинается с `http://` или `https://`.\n"
                "2. Доменное имя корректно (например, `example.com`).\n"
                "3. Сайт доступен в данный момент.\n\n"
                "Попробуйте еще раз."
            )
            return

        await state.update_data(button_url=url_to_check)
        await show_broadcast_preview(message, state, bot)

    @user_router.callback_query(
        Broadcast.waiting_for_button_option, F.data == "broadcast_skip_button"
    )
    async def skip_button_handler(
        callback: types.CallbackQuery, state: FSMContext, bot: Bot
    ):
        await callback.answer()
        await state.update_data(button_text=None, button_url=None)
        await show_broadcast_preview(callback.message, state, bot)

    async def show_broadcast_preview(
        message: types.Message, state: FSMContext, bot: Bot
    ):
        data = await state.get_data()
        message_json = data.get("message_to_send")
        original_message = types.Message.model_validate_json(message_json)

        button_text = data.get("button_text")
        button_url = data.get("button_url")

        preview_keyboard = None
        if button_text and button_url:
            builder = InlineKeyboardBuilder()
            builder.button(text=button_text, url=button_url)
            preview_keyboard = builder.as_markup()

        await message.answer(
            "Вот так будет выглядеть ваше сообщение. Отправляем?",
            reply_markup=keyboards.create_broadcast_confirmation_keyboard(),
        )

        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=original_message.chat.id,
            message_id=original_message.message_id,
            reply_markup=preview_keyboard,
        )

        await state.set_state(Broadcast.waiting_for_confirmation)

    @user_router.callback_query(
        Broadcast.waiting_for_confirmation, F.data == "confirm_broadcast"
    )
    async def confirm_broadcast_handler(
        callback: types.CallbackQuery, state: FSMContext, bot: Bot
    ):
        await callback.message.edit_text(
            "⏳ Начинаю рассылку... Это может занять некоторое время."
        )

        data = await state.get_data()
        message_json = data.get("message_to_send")
        original_message = types.Message.model_validate_json(message_json)

        button_text = data.get("button_text")
        button_url = data.get("button_url")

        final_keyboard = None
        if button_text and button_url:
            builder = InlineKeyboardBuilder()
            builder.button(text=button_text, url=button_url)
            final_keyboard = builder.as_markup()

        await state.clear()

        users = get_all_users()
        logger.info(f"Broadcast: Starting to iterate over {len(users)} users.")

        sent_count = 0
        failed_count = 0
        banned_count = 0

        for user in users:
            user_id = user["telegram_id"]
            if user.get("is_banned"):
                banned_count += 1
                continue

            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=original_message.chat.id,
                    message_id=original_message.message_id,
                    reply_markup=final_keyboard,
                )

                sent_count += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                failed_count += 1
                logger.warning(
                    f"Failed to send broadcast message to user {user_id}: {e}"
                )

        await callback.message.answer(
            f"✅ Рассылка завершена!\n\n"
            f"👍 Отправлено: {sent_count}\n"
            f"👎 Не удалось отправить: {failed_count}\n"
            f"🚫 Пропущено (забанены): {banned_count}"
        )
        await show_main_menu(callback.message)

    @user_router.callback_query(StateFilter(Broadcast), F.data == "cancel_broadcast")
    async def cancel_broadcast_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer("Рассылка отменена.")
        await state.clear()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_referral_program")
    @registration_required
    async def referral_program_handler(callback: types.CallbackQuery):
        await callback.answer()

        # Проверяем включена ли реферальная система
        if get_setting("enable_referrals") != "true":
            await callback.message.edit_text(
                "❌ Реферальная программа временно недоступна.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        user_id = callback.from_user.id
        user_data = get_user(user_id)
        bot_username = (await callback.bot.get_me()).username

        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        referral_count = get_referral_count(user_id)
        balance = user_data.get("referral_balance", 0)

        text = (
            "🤝 <b>Реферальная программа</b>\n\n"
            "Приглашайте друзей и получайте вознаграждение с <b>каждой</b> их покупки!\n\n"
            f"<b>Ваша реферальная ссылка:</b>\n<code>{referral_link}</code>\n\n"
            f"<b>Приглашено пользователей:</b> {referral_count}\n"
            f"<b>Ваш баланс:</b> {balance:.2f} RUB"
        )

        builder = InlineKeyboardBuilder()
        if balance >= 100:
            builder.button(
                text="💸 Оставить заявку на вывод", callback_data="withdraw_request"
            )
        builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
        await callback.message.edit_text(text, reply_markup=builder.as_markup())

    @user_router.callback_query(F.data == "withdraw_request")
    @registration_required
    async def withdraw_request_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer()
        await callback.message.edit_text(
            "Пожалуйста, отправьте ваши реквизиты для вывода (номер карты или номер телефона и банк):",
            reply_markup=keyboards.create_back_to_menu_keyboard(),
        )
        await state.set_state(WithdrawStates.waiting_for_details)

    @user_router.message(WithdrawStates.waiting_for_details)
    @registration_required
    async def process_withdraw_details(message: types.Message, state: FSMContext):
        user_id = message.from_user.id
        user = get_user(user_id)
        balance = user.get("referral_balance", 0)
        details = message.text.strip()
        if balance < 100:
            await message.answer("❌ Ваш баланс менее 100 руб. Вывод недоступен.")
            await state.clear()
            return

        admin_id_str = get_setting("admin_telegram_id")
        if not admin_id_str:
            await message.answer(
                "❌ Ошибка: Администратор не настроен. Обратитесь в поддержку."
            )
            await state.clear()
            return
        admin_id = int(admin_id_str)
        text = (
            f"💸 <b>Заявка на вывод реферальных средств</b>\n"
            f"👤 Пользователь: @{user.get('username', 'N/A')} (ID: <code>{user_id}</code>)\n"
            f"💰 Сумма: <b>{balance:.2f} RUB</b>\n"
            f"📄 Реквизиты: <code>{details}</code>\n\n"
            f"/approve_withdraw_{user_id} /decline_withdraw_{user_id}"
        )
        await message.answer("Ваша заявка отправлена администратору. Ожидайте ответа.")
        await message.bot.send_message(admin_id, text, parse_mode="HTML")
        await state.clear()

    @user_router.message(F.text.regexp(r"^/approve_withdraw_\d+(?:@\w+)?$"))
    async def approve_withdraw_handler(message: types.Message):
        admin_id_str = get_setting("admin_telegram_id")
        if not admin_id_str:
            return
        admin_id = int(admin_id_str)
        if message.from_user.id != admin_id:
            return
        try:
            user_id = _extract_withdraw_command_user_id(
                message.text, "approve_withdraw"
            )
            if user_id is None:
                await message.answer("Неверный формат команды.")
                return
            user = get_user(user_id)
            balance = user.get("referral_balance", 0)
            if balance < 100:
                await message.answer("Баланс пользователя менее 100 руб.")
                return
            set_referral_balance(user_id, 0)
            # referral_balance_all — lifetime-счётчик, не сбрасывается при выводе
            await message.answer(
                f"✅ Выплата {balance:.2f} RUB пользователю {user_id} подтверждена."
            )
            await message.bot.send_message(
                user_id,
                f"✅ Ваша заявка на вывод {balance:.2f} RUB одобрена. Деньги будут переведены в ближайшее время.",
            )
        except Exception as e:
            await message.answer(f"Ошибка: {e}")

    @user_router.message(F.text.regexp(r"^/decline_withdraw_\d+(?:@\w+)?$"))
    async def decline_withdraw_handler(message: types.Message):
        admin_id_str = get_setting("admin_telegram_id")
        if not admin_id_str:
            return
        admin_id = int(admin_id_str)
        if message.from_user.id != admin_id:
            return
        try:
            user_id = _extract_withdraw_command_user_id(
                message.text, "decline_withdraw"
            )
            if user_id is None:
                await message.answer("Неверный формат команды.")
                return
            await message.answer(f"❌ Заявка пользователя {user_id} отклонена.")
            await message.bot.send_message(
                user_id,
                "❌ Ваша заявка на вывод отклонена. Проверьте корректность реквизитов и попробуйте снова.",
            )
        except Exception as e:
            await message.answer(f"Ошибка: {e}")

    @user_router.callback_query(F.data == "show_about")
    @registration_required
    async def about_handler(callback: types.CallbackQuery):
        await callback.answer()

        about_text = get_setting("about_text")
        terms_url = get_setting("terms_url")
        privacy_url = get_setting("privacy_url")
        channel_url = get_setting("channel_url")

        final_text = about_text if about_text else "Информация о проекте не добавлена."

        keyboard = keyboards.create_about_keyboard(channel_url, terms_url, privacy_url)

        await callback.message.edit_text(
            final_text, reply_markup=keyboard, disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "show_help")
    @registration_required
    async def help_handler(callback: types.CallbackQuery):
        await callback.answer()

        support_user = get_setting("support_user")
        support_text = get_setting("support_text")

        if not support_user and not support_text:
            await callback.message.edit_text(
                "Информация о поддержке не установлена. Установите её в админ-панели.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
        elif not support_user:
            await callback.message.edit_text(
                support_text or "Обратитесь в поддержку.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
        elif not support_text:
            await callback.message.edit_text(
                "Для связи с поддержкой используйте кнопку ниже.",
                reply_markup=keyboards.create_support_keyboard(support_user),
            )
        else:
            await callback.message.edit_text(
                support_text,
                reply_markup=keyboards.create_support_keyboard(support_user),
            )

    @user_router.callback_query(F.data == "manage_keys")
    @registration_required
    async def manage_keys_handler(callback: types.CallbackQuery):
        try:
            await callback.answer()
        except TelegramBadRequest as e:
            message = str(e).lower()
            if (
                "query is too old" not in message
                and "query id is invalid" not in message
                and "response timeout expired" not in message
            ):
                raise
        user_id = callback.from_user.id

        # Get PAID keys (for global subscription check) and TRIAL keys
        paid_keys = get_user_paid_keys(user_id)
        trial_keys = get_user_trial_keys(user_id)
        all_keys = get_user_keys(user_id)
        now = time_utils.get_msk_now()

        xui_paid_keys = [k for k in paid_keys if k.get("service_type", "xui") == "xui"]
        mtg_keys = [k for k in all_keys if k.get("service_type") == "mtg"]
        xui_all_keys = [k for k in all_keys if k.get("service_type", "xui") == "xui"]

        active_paid_keys = []
        for k in xui_paid_keys:
            dt = time_utils.parse_iso_to_msk(k.get("expiry_date"))
            if dt and dt > now:
                active_paid_keys.append(k)
        active_paid_keys = _dedupe_paid_keys_by_host(active_paid_keys)
        active_trial_keys = get_active_trial_keys(user_id)
        has_global = has_active_global_access(active_paid_keys, active_trial_keys)

        try:
            if has_global:
                description = (
                    "У вас активна глобальная подписка.\n"
                    "Вы можете управлять ими как единой подпиской. Продление действует сразу на все сервера."
                    if has_active_global_subscription(active_paid_keys)
                    else "У вас активен пробный глобальный доступ.\n"
                    "После окончания можно сразу продлить общую подписку, не добавляя её заново."
                )
                await callback.message.edit_text(
                    "📂 <b>Управление подписками</b>\n\n" + description,
                    reply_markup=keyboards.create_unified_keys_keyboard(
                        len(active_paid_keys) or len(active_trial_keys),
                        0,
                        len(mtg_keys),
                    ),
                )
            else:
                # Standard View — show xui keys + mtg keys together
                display_keys = xui_all_keys + mtg_keys
                await callback.message.edit_text(
                    "Ваши подписки:" if display_keys else "У вас пока нет подписок.",
                    reply_markup=keyboards.create_keys_management_keyboard(
                        display_keys
                    ),
                )
        except Exception as e:
            if "message is not modified" in str(e):
                pass
            else:
                logger.error(f"Error in manage_keys_handler: {e}")

    @user_router.callback_query(F.data == "show_proxy_keys")
    @registration_required
    async def show_proxy_keys_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        all_keys = get_user_keys(user_id)
        mtg_keys = [k for k in all_keys if k.get("service_type") == "mtg"]
        if not mtg_keys:
            await callback.message.edit_text(
                "📡 У вас пока нет Telegram Proxy.",
                reply_markup=keyboards.create_proxy_keys_keyboard([]),
            )
            return
        # Build text with full info for each proxy — no extra tap needed
        now = time_utils.get_msk_now()
        parts = ["📡 <b>Ваши Telegram Proxy:</b>\n"]
        for i, key in enumerate(mtg_keys, 1):
            proxy_link = key.get("connection_string", "")
            expiry_date = time_utils.parse_iso_to_msk(key.get("expiry_date"))
            created_date = time_utils.parse_iso_to_msk(key.get("created_date"))
            if expiry_date:
                expiry_fmt = time_utils.format_msk(expiry_date, "%d.%m.%Y в %H:%M")
                is_active = expiry_date > now
                status = "✅ активен" if is_active else "❌ истёк"
            else:
                expiry_fmt = "?"
                status = "❓"
            created_fmt = (
                time_utils.format_msk(created_date, "%d.%m.%Y") if created_date else "?"
            )
            parts.append(
                f"<blockquote>📡 <b>Telegram Proxy #{i}</b>  {status}\n"
                f"📅 до {expiry_fmt} (МСК)\n"
                f"🗓 Куплен {created_fmt}\n\n"
                f"<code>{proxy_link}</code></blockquote>"
            )
        await callback.message.edit_text(
            "\n".join(parts),
            reply_markup=keyboards.create_proxy_keys_keyboard(mtg_keys),
        )

    @user_router.callback_query(F.data == "show_keys_detailed")
    @registration_required
    async def show_keys_detailed_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        active_paid_keys = get_active_paid_keys(user_id)
        active_trial_keys = get_active_trial_keys(user_id)
        user_keys = (
            active_paid_keys
            if has_active_global_subscription(active_paid_keys)
            else active_trial_keys
        )
        await callback.message.edit_text(
            "📋 <b>Детальный список ключей:</b>",
            reply_markup=keyboards.create_keys_management_keyboard(user_keys),
        )

    @user_router.callback_query(F.data == "show_trial_keys")
    @registration_required
    async def show_trial_keys_handler(callback: types.CallbackQuery):
        """Show trial keys in detailed list"""
        await callback.answer()
        user_id = callback.from_user.id

        trial_keys = get_user_trial_keys(user_id)

        if not trial_keys:
            await callback.message.edit_text(
                "У вас нет активного пробного периода VPN.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        await callback.message.edit_text(
            "🎁 <b>Пробный период VPN:</b>\n"
            "Ниже показаны серверы, входящие в ваш пробный глобальный доступ.",
            reply_markup=keyboards.create_keys_management_keyboard(trial_keys),
        )

    @user_router.callback_query(F.data == "show_global_info")
    @registration_required
    async def show_global_info_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        paid_keys = get_active_paid_keys(user_id)
        trial_keys = get_active_trial_keys(user_id)
        is_paid_global = has_active_global_subscription(paid_keys)
        user_keys = paid_keys if is_paid_global else trial_keys

        user_token = get_or_create_subscription_token(user_id)

        if not has_active_global_access(paid_keys, trial_keys):
            await callback.message.edit_text(
                "У вас нет активной глобальной подписки.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        # Calculate expiry (minimum of all keys to be safe)
        expiry_dates = []
        for k in user_keys:
            dt = time_utils.parse_iso_to_msk(k.get("expiry_date"))
            if dt:
                expiry_dates.append(dt)

        min_expiry = min(expiry_dates)
        days_left = (min_expiry - time_utils.get_msk_now()).days
        status_text = "Активна" if is_paid_global else "Пробный период"

        await callback.message.edit_text(
            f"🌍 <b>Глобальная подписка</b>\n\n"
            f"✅ <b>Статус:</b> {status_text}\n"
            f"📅 <b>Истекает:</b> {min_expiry.strftime('%d.%m.%Y')}\n"
            f"⏳ <b>Осталось дней:</b> {days_left}\n"
            f"🔗 <b>Доступно серверов:</b> {len(user_keys)}\n\n"
            "Продление действует сразу на все сервера в глобальной подписке.\n"
            "Используйте кнопки ниже для продления или подключения.",
            reply_markup=(
                keyboards.create_global_info_keyboard(user_token)
                if user_token
                else keyboards.create_back_to_menu_keyboard()
            ),
        )

    @user_router.callback_query(F.data == "get_trial")
    @registration_required
    async def trial_period_handler(callback: types.CallbackQuery, state: FSMContext):
        user_id = callback.from_user.id
        user_db_data = get_user(user_id)
        if user_db_data and user_db_data.get("trial_used"):
            await callback.answer(
                "Вы уже использовали бесплатный пробный период.", show_alert=True
            )
            return

        hosts = get_all_hosts(only_enabled=True)
        if not hosts:
            await callback.message.edit_text(
                "❌ В данный момент нет доступных серверов для создания пробного ключа."
            )
            return

        chosen_host = hosts[0]

        await callback.answer()
        await process_trial_key_creation(callback.message, chosen_host["host_name"])

    async def process_trial_key_creation(message: types.Message, host_name: str):
        user_id = message.chat.id
        trial_days_raw = get_setting("trial_duration_days")
        try:
            trial_days = int(float(trial_days_raw)) if trial_days_raw else 1
        except (TypeError, ValueError):
            trial_days = 1

        if trial_days <= 0:
            trial_days = 1

        await message.edit_text(
            f"Отлично! Создаю для вас бесплатный пробный доступ на {trial_days} дней для всей VPN-подписки..."
        )

        try:
            user_token = get_or_create_subscription_token(user_id)
            domain = get_setting("domain")
            hosts_to_process = get_all_hosts(only_enabled=True)
            results: list[dict] = []

            for host in hosts_to_process:
                current_host_name = host["host_name"]
                existing_trial_key = None
                for key in get_user_trial_keys(user_id):
                    if (
                        key.get("service_type", "xui") == "xui"
                        and key.get("host_name") == current_host_name
                    ):
                        existing_trial_key = key
                        break

                email = (
                    existing_trial_key["key_email"]
                    if existing_trial_key
                    else f"user{user_id}-global-{current_host_name.replace(' ', '').lower()}"
                )
                result = await xui_api.create_or_update_key_on_host(
                    host_name=current_host_name,
                    email=email,
                    days_to_add=int(trial_days),
                    telegram_id=str(user_id),
                )
                if not result:
                    continue

                results.append(result)
                expiry_datetime = time_utils.from_timestamp_ms(
                    result["expiry_timestamp_ms"]
                )
                if existing_trial_key:
                    update_key_info(
                        existing_trial_key["key_id"],
                        expiry_datetime,
                        result["connection_string"],
                    )
                else:
                    add_new_key(
                        user_id=user_id,
                        host_name=current_host_name,
                        xui_client_uuid=result["client_uuid"],
                        key_email=result["email"],
                        expiry_timestamp_ms=result["expiry_timestamp_ms"],
                        connection_string=result["connection_string"],
                        plan_id=0,
                    )

            if not results:
                await message.edit_text(
                    "❌ Не удалось создать пробный доступ. Ошибка на сервере."
                )
                return

            set_trial_used(user_id)

            await message.delete()
            new_expiry_date = min(
                time_utils.from_timestamp_ms(res["expiry_timestamp_ms"])
                for res in results
            )
            final_text = (
                f"🎁 <b>Пробный период активирован!</b>\n"
                f"<blockquote>📅 до {new_expiry_date.strftime('%d.%m.%Y')}\n"
                f"🔗 Доступно серверов: {len(results)}</blockquote>\n"
            )

            sub_link = _build_subscription_link(domain, user_token)
            if sub_link:
                final_text += (
                    f"\n🌍 <b>Ссылка-подписка:</b>\n<code>{sub_link}</code>\n\n"
                    "<blockquote>Добавьте ссылку в Happ как обычную подписку. После окончания пробного периода можно будет продлить эту же глобальную подписку.</blockquote>"
                )
            else:
                final_text += "\n⚠️ Не удалось сформировать ссылку подписки. Проверьте настройку домена в админке."

            failed_hosts = [
                host["host_name"]
                for host in hosts_to_process
                if host["host_name"] not in [res["host_name"] for res in results]
            ]
            if failed_hosts:
                final_text += (
                    "\n\n⚠️ Не удалось выдать пробный доступ на серверы:\n- "
                    + "\n- ".join(failed_hosts)
                )

            await message.answer(
                text=final_text,
                reply_markup=(
                    keyboards.create_global_info_keyboard(user_token)
                    if user_token
                    else keyboards.create_back_to_menu_keyboard()
                ),
            )

            await notify_admin_of_trial(message.bot, user_id, "ALL", trial_days)

        except Exception as e:
            logger.error(
                f"Error creating trial key for user {user_id} on host {host_name}: {e}",
                exc_info=True,
            )
            await message.edit_text("❌ Произошла ошибка при создании пробного ключа.")

    @user_router.callback_query(F.data.startswith("show_key_"))
    @registration_required
    async def show_key_handler(callback: types.CallbackQuery):
        key_id_to_show = int(callback.data.split("_")[2])
        await callback.message.edit_text("Загружаю информацию о ключе...")
        user_id = callback.from_user.id
        key_data = get_key_by_id(key_id_to_show)

        if not key_data or key_data["user_id"] != user_id:
            await callback.message.edit_text("❌ Ошибка: ключ не найден.")
            return

        # MTG proxy key: use stored connection_string directly
        if key_data.get("service_type") == "mtg":
            try:
                proxy_link = key_data.get("connection_string", "")
                expiry_date = time_utils.parse_iso_to_msk(key_data["expiry_date"])
                created_date = time_utils.parse_iso_to_msk(key_data["created_date"])
                all_user_mtg_keys = [
                    k for k in get_user_keys(user_id) if k.get("service_type") == "mtg"
                ]
                key_number = next(
                    (
                        i + 1
                        for i, k in enumerate(all_user_mtg_keys)
                        if k["key_id"] == key_id_to_show
                    ),
                    1,
                )
                final_text = get_proxy_info_text(
                    key_number, expiry_date, created_date, proxy_link
                )
                await callback.message.edit_text(
                    text=final_text,
                    reply_markup=keyboards.create_proxy_info_keyboard(
                        key_id_to_show, proxy_link
                    ),
                )
            except Exception as e:
                logger.error(f"Error showing MTG key {key_id_to_show}: {e}")
                await callback.message.edit_text(
                    "❌ Произошла ошибка при получении данных прокси."
                )
            return

        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details["connection_string"]:
                await callback.message.edit_text(
                    "❌ Ошибка на сервере. Не удалось получить данные ключа."
                )
                return

            connection_string = details["connection_string"]
            expiry_date = time_utils.parse_iso_to_msk(key_data["expiry_date"])
            created_date = time_utils.parse_iso_to_msk(key_data["created_date"])

            all_user_xui_keys = [
                k
                for k in get_user_keys(user_id)
                if k.get("service_type", "xui") != "mtg"
            ]
            key_number = next(
                (
                    i + 1
                    for i, key in enumerate(all_user_xui_keys)
                    if key["key_id"] == key_id_to_show
                ),
                1,
            )

            final_text = get_key_info_text(
                key_number, expiry_date, created_date, connection_string
            )
            final_text += "\n\n<blockquote>📋 Скопируйте ключ → откройте Happ → нажмите <b>+</b> → вставьте ссылку из буфера обмена.</blockquote>"

            await callback.message.edit_text(
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(
                    key_id_to_show, connection_string
                ),
            )
        except Exception as e:
            logger.error(f"Error showing key {key_id_to_show}: {e}")
            await callback.message.edit_text(
                "❌ Произошла ошибка при получении данных ключа."
            )

    @user_router.callback_query(F.data.startswith("show_qr_"))
    @registration_required
    async def show_qr_handler(callback: types.CallbackQuery):
        await callback.answer("Генерирую QR-код...")
        key_id = int(callback.data.split("_")[2])
        key_data = get_key_by_id(key_id)
        if not key_data or key_data["user_id"] != callback.from_user.id:
            return

        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details["connection_string"]:
                await callback.answer(
                    "Ошибка: Не удалось сгенерировать QR-код.", show_alert=True
                )
                return

            connection_string = details["connection_string"]
            qr_img = qrcode.make(connection_string)
            bio = BytesIO()
            qr_img.save(bio, "PNG")
            bio.seek(0)
            qr_code_file = BufferedInputFile(bio.read(), filename="vpn_qr.png")
            await callback.message.answer_photo(
                photo=qr_code_file, caption="📱 <b>QR-код для ключа</b>"
            )
        except Exception as e:
            logger.error(f"Error showing QR for key {key_id}: {e}")

    @user_router.callback_query(F.data.startswith("howto_vless_"))
    @registration_required
    async def show_instruction_handler(callback: types.CallbackQuery):
        await callback.answer()
        key_id = int(callback.data.split("_")[2])

        await callback.message.edit_text(
            "Выберите вашу платформу для инструкции по подключению VLESS:",
            reply_markup=keyboards.create_howto_vless_keyboard_key(key_id),
            disable_web_page_preview=True,
        )

    @user_router.callback_query(F.data == "howto_vless")
    @registration_required
    async def show_instruction_generic_handler(callback: types.CallbackQuery):
        await callback.answer()

        await callback.message.edit_text(
            "<b>📖 Инструкция по подключению</b>\n\n"
            "<b>Рекомендуемое приложение:</b> <a href='https://www.happ.su/main/ru'>Happ</a>\n\n"
            "<b>Что вы получаете в боте:</b>\n"
            "1. <code>vless://...</code> — один VPN-ключ.\n"
            "2. <code>https://.../sub/...</code> — ссылка подписки со всеми серверами.\n\n"
            "<b>Самый простой сценарий:</b>\n"
            "1. Установите Happ на ваше устройство.\n"
            "2. Скопируйте в боте ключ или ссылку подписки.\n"
            "3. Откройте Happ и нажмите <b>+</b>.\n"
            "4. Вставьте ссылку из буфера обмена.\n"
            "5. Если добавили <code>/sub/</code> — нажмите <b>Обновить подписку</b>.\n"
            "6. Выберите сервер и включите VPN.\n\n"
            "Выберите платформу ниже:",
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True,
        )

    @user_router.callback_query(F.data == "howto_android")
    @registration_required
    async def howto_android_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на Android</b>\n\n"
            "1. Установите <a href='https://play.google.com/store/apps/details?id=com.happproxy'>Happ из Google Play</a>.\n"
            "2. В боте скопируйте VPN-ключ <code>vless://...</code> или ссылку подписки <code>https://.../sub/...</code>.\n"
            "3. Откройте Happ и нажмите <b>+</b>.\n"
            "4. Выберите добавление из буфера обмена и вставьте ссылку.\n"
            "5. Если добавили <code>/sub/</code> — нажмите <b>Обновить подписку</b>.\n"
            "6. Выберите сервер и включите подключение.\n\n"
            "Для подписки <code>/sub/</code> в Happ появится список серверов.",
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True,
        )

    @user_router.callback_query(F.data == "howto_ios")
    @registration_required
    async def howto_ios_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на iOS (iPhone/iPad)</b>\n\n"
            "1. Установите <a href='https://apps.apple.com/us/app/happ-proxy-utility/id6504287215'>Happ из App Store</a>.\n"
            "2. В боте скопируйте VPN-ключ <code>vless://...</code> или ссылку подписки <code>https://.../sub/...</code>.\n"
            "3. Откройте Happ и нажмите <b>+</b>.\n"
            "4. Выберите добавление из буфера обмена и вставьте ссылку.\n"
            "5. Если добавили <code>/sub/</code> — нажмите <b>Обновить подписку</b>.\n"
            "6. Выберите сервер и включите VPN.\n\n"
            "Для подписки <code>/sub/</code> в Happ появится готовый список серверов.",
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True,
        )

    @user_router.callback_query(F.data == "howto_macos")
    @registration_required
    async def howto_macos_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на macOS</b>\n\n"
            "1. Установите Happ: <a href='https://apps.apple.com/us/app/happ-proxy-utility/id6504287215'>App Store</a> или <a href='https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg'>скачать dmg</a>.\n"
            "2. В боте скопируйте VPN-ключ <code>vless://...</code> или ссылку подписки <code>https://.../sub/...</code>.\n"
            "3. Откройте Happ и нажмите <b>+</b>.\n"
            "4. Добавьте ссылку из буфера обмена.\n"
            "5. Если добавили <code>/sub/</code> — нажмите <b>Обновить подписку</b>.\n"
            "6. Выберите сервер и включите подключение.\n"
            "7. При необходимости проверьте IP на <a href='https://2ip.ru'>2ip.ru</a>.",
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True,
        )

    @user_router.callback_query(F.data == "howto_windows")
    @registration_required
    async def howto_windows_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на Windows</b>\n\n"
            "1. Установите <a href='https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe'>Happ для Windows</a>.\n"
            "2. В боте скопируйте VPN-ключ <code>vless://...</code> или ссылку подписки <code>https://.../sub/...</code>.\n"
            "3. Откройте Happ и нажмите <b>+</b>.\n"
            "4. Добавьте ссылку из буфера обмена.\n"
            "5. Если добавили <code>/sub/</code> — нажмите <b>Обновить подписку</b>.\n"
            "6. Выберите сервер и включите подключение.\n"
            "7. При необходимости проверьте IP на <a href='https://2ip.ru'>2ip.ru</a>.",
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True,
        )

    @user_router.callback_query(F.data == "howto_linux")
    @registration_required
    async def howto_linux_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на Linux</b>\n\n"
            "1. <b>Скачайте и распакуйте Nekoray:</b> Перейдите в <a href='https://github.com/MatsuriDayo/Nekoray/releases'>релизы Nekoray на GitHub</a> и скачайте архив для Linux. Распакуйте его в удобную папку.\n"
            "2. <b>Запустите Nekoray:</b> Откройте терминал, перейдите в папку с Nekoray и выполните <code>./nekoray</code> (или используйте графический запуск, если доступен).\n"
            "3. <b>Скопируйте в боте</b> либо ключ <code>vless://...</code>, либо ссылку подписки <code>https://.../sub/...</code>.\n"
            "4. <b>Импортируйте:</b>\n"
            "   • В Nekoray нажмите «Сервер» (Server).\n"
            "   • Для <code>vless://</code> выберите «Импортировать из буфера обмена».\n"
            "   • Для <code>/sub/</code> добавьте Subscription URL.\n"
            "5. <b>Обновите подписку</b> (Server/Subscription Update).\n"
            "6. Включите <b>TUN Mode</b>.\n"
            "7. <b>Выберите сервер</b> и нажмите «Подключить».\n"
            "8. <b>Проверьте IP</b> на <a href='https://whatismyipaddress.com/'>WhatIsMyIPAddress</a>.",
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True,
        )

    @user_router.callback_query(F.data.in_({"buy_new_key", "buy_subscription"}))
    @registration_required
    async def buy_new_key_handler(callback: types.CallbackQuery, state: FSMContext):
        try:
            await callback.answer()
            await state.update_data(purchase_back_callback="back_to_main_menu")
            await _show_vpn_purchase_plans(callback, action="new", key_id=0)
        except Exception as e:
            logger.error(f"Error in buy_new_key_handler: {e}", exc_info=True)
            await callback.message.edit_text("❌ Произошла ошибка. Попробуйте позже.")

    @user_router.callback_query(F.data == "back_to_host_selection")
    @registration_required
    async def back_to_host_selection_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer()
        await _show_vpn_purchase_plans(callback, action="new", key_id=0)

    # ── MTG Proxy purchase flow ───────────────────────────────────────────────

    @user_router.callback_query(F.data == "buy_proxy")
    @registration_required
    async def buy_proxy_handler(callback: types.CallbackQuery, state: FSMContext):
        try:
            await callback.answer()
            mtg_hosts = get_all_mtg_hosts(only_enabled=True)
            if not mtg_hosts:
                await callback.answer(
                    "📡 Telegram Proxy временно недоступен.", show_alert=True
                )
                return
            await state.update_data(
                purchase_back_callback="back_to_main_menu", service_type="mtg"
            )
            await callback.message.edit_text(
                CHOOSE_PROXY_HOST_MESSAGE,
                reply_markup=keyboards.create_mtg_host_selection_keyboard(
                    mtg_hosts, back_callback="back_to_main_menu"
                ),
            )
        except Exception as e:
            logger.error(f"Error in buy_proxy_handler: {e}", exc_info=True)
            await callback.message.edit_text("❌ Произошла ошибка. Попробуйте позже.")

    @user_router.callback_query(F.data.startswith("select_mtg_host_"))
    @registration_required
    async def select_mtg_host_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        host_name = callback.data[len("select_mtg_host_") :]
        user_id = callback.from_user.id

        # Check if user already has a proxy on this host
        all_keys = get_user_keys(user_id)
        existing = next(
            (
                k
                for k in all_keys
                if k.get("service_type") == "mtg" and k.get("host_name") == host_name
            ),
            None,
        )
        if existing:
            exp_dt = time_utils.parse_iso_to_msk(existing.get("expiry_date"))
            now = time_utils.get_msk_now()
            key_id = existing["key_id"]
            if exp_dt and exp_dt > now:
                exp_str = time_utils.format_msk(exp_dt)
                builder = InlineKeyboardBuilder()
                builder.button(
                    text="➕ Продлить прокси", callback_data=f"extend_key_{key_id}"
                )
                builder.button(text="← Назад", callback_data="buy_proxy")
                builder.adjust(1)
                await callback.message.edit_text(
                    f"📡 У вас уже есть активный Telegram Proxy на сервере <b>{host_name}</b>.\n"
                    f"📅 Действует до: {exp_str}\n\n"
                    "Вы можете продлить текущий прокси.",
                    reply_markup=builder.as_markup(),
                )
                return
            # Expired — offer to extend or buy new (but there's only one slot per host)
            builder = InlineKeyboardBuilder()
            builder.button(
                text="🔄 Продлить (активировать снова)",
                callback_data=f"extend_key_{key_id}",
            )
            builder.button(text="← Назад", callback_data="buy_proxy")
            builder.adjust(1)
            await callback.message.edit_text(
                f"📡 Ваш Telegram Proxy на сервере <b>{host_name}</b> истёк.\n\n"
                "Вы можете продлить его, чтобы активировать снова.",
                reply_markup=builder.as_markup(),
            )
            return

        plans = get_plans_for_host(host_name, service_type="mtg")
        if not plans:
            await callback.message.edit_text(
                f'❌ Для прокси-сервера "{host_name}" не настроены тарифы.'
            )
            return
        await state.update_data(host_name=host_name, service_type="mtg")
        await callback.message.edit_text(
            "Выберите тариф для Telegram Proxy:",
            reply_markup=keyboards.create_plans_keyboard(
                plans, action="new", host_name=host_name, key_id=0
            ),
        )

    @user_router.callback_query(F.data.startswith("select_host_new_"))
    @registration_required
    async def select_host_for_purchase_handler(callback: types.CallbackQuery):
        await callback.answer()
        host_name = callback.data[len("select_host_new_") :]
        plans = get_plans_for_host(host_name, service_type="xui")
        if not plans:
            await callback.message.edit_text(
                f'❌ Для сервера "{host_name}" не настроены тарифы.'
            )
            return
        msg_text = f'Выберите тариф для сервера "{host_name}":'
        if host_name == "ALL":
            msg_text = "🌍 Выберите тариф единой подписки (на все серверы):"

        await callback.message.edit_text(
            msg_text,
            reply_markup=keyboards.create_plans_keyboard(
                plans, action="new", host_name=host_name
            ),
        )

    @user_router.callback_query(F.data.startswith("extend_key_"))
    @registration_required
    async def extend_key_handler(callback: types.CallbackQuery):
        await callback.answer()

        try:
            key_id = int(callback.data.split("_")[2])
        except (IndexError, ValueError):
            await callback.message.edit_text(
                "❌ Произошла ошибка. Неверный формат ключа."
            )
            return

        key_data = get_key_by_id(key_id)

        if not key_data or key_data["user_id"] != callback.from_user.id:
            await callback.message.edit_text(
                "❌ Ошибка: Ключ не найден или не принадлежит вам."
            )
            return

        host_name = key_data.get("host_name")
        if not host_name:
            await callback.message.edit_text(
                "❌ Ошибка: У этого ключа не указан сервер. Обратитесь в поддержку."
            )
            return

        plans = get_plans_for_host(
            host_name, service_type=key_data.get("service_type", "xui")
        )

        if not plans:
            await callback.message.edit_text(
                f'❌ Извините, для сервера "{host_name}" в данный момент не настроены тарифы для продления.'
            )
            return

        await callback.message.edit_text(
            f'Выберите тариф для продления ключа на сервере "{host_name}":',
            reply_markup=keyboards.create_plans_keyboard(
                plans=plans, action="extend", host_name=host_name, key_id=key_id
            ),
        )

    @user_router.callback_query(F.data.startswith("buy_"))
    @registration_required
    async def plan_selection_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()

        parts = callback.data.split("_")[1:]
        action = parts[-2]
        key_id = int(parts[-1])
        plan_id = int(parts[-3])
        host_name = "_".join(parts[:-3])

        # Determine service_type from plan to route payment fulfillment correctly
        _plan = get_plan_by_id(plan_id)
        service_type = _plan.get("service_type", "xui") if _plan else "xui"

        await state.update_data(
            action=action,
            key_id=key_id,
            plan_id=plan_id,
            host_name=host_name,
            service_type=service_type,
        )

        # Check if user already has a paid key for this host (or global if host is ALL)
        existing_paid_key = None
        user_keys = get_user_keys(callback.from_user.id)

        for k in user_keys:
            # Check for specific host match OR if purchasing global (All)
            target_match = (k["host_name"] == host_name) or (host_name == "ALL")
            # Check if key is paid (plan_id > 0)
            is_paid = k.get("plan_id", 0) > 0

            if target_match and is_paid:
                existing_paid_key = k
                break

        message_text = CHOOSE_PAYMENT_METHOD_MESSAGE
        if existing_paid_key:
            message_text = (
                f"⚠️ <b>Внимание:</b> У вас уже есть активная подписка на "
                f"{'все серверы' if host_name == 'ALL' else f'сервере {host_name}'}.\n"
                "Новая покупка продлит текущую подписку.\n\n"
            ) + CHOOSE_PAYMENT_METHOD_MESSAGE

        await state.update_data(customer_email=None)
        await callback.message.edit_text(
            message_text,
            reply_markup=keyboards.create_payment_method_keyboard(
                payment_methods=get_active_payment_methods(
                    context_key=_build_payment_context(service_type, host_name),
                    plan_id=plan_id,
                ),
                action=action,
                key_id=key_id,
            ),
        )
        await state.set_state(PaymentProcess.waiting_for_payment_method)
        logger.info(
            f"User {callback.from_user.id}: State set to waiting_for_payment_method"
        )

    @user_router.callback_query(
        PaymentProcess.waiting_for_email, F.data == "back_to_plans"
    )
    @user_router.callback_query(F.data == "back_to_plans")
    async def back_to_plans_handler(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        purchase_back_callback = data.get("purchase_back_callback", "manage_keys")
        service_type = data.get("service_type", "xui")
        host_name = data.get("host_name")
        await state.clear()

        action = data.get("action")

        if action == "new":
            await callback.answer()
            await state.update_data(
                purchase_back_callback=purchase_back_callback,
                service_type=service_type,
                host_name=host_name,
            )
            if service_type == "mtg":
                if host_name:
                    plans = get_plans_for_host(host_name, service_type="mtg")
                    if not plans:
                        await callback.message.edit_text(
                            f'❌ Для прокси-сервера "{host_name}" не настроены тарифы.'
                        )
                        return
                    await callback.message.edit_text(
                        "Выберите тариф для Telegram Proxy:",
                        reply_markup=keyboards.create_plans_keyboard(
                            plans, action="new", host_name=host_name, key_id=0
                        ),
                    )
                else:
                    mtg_hosts = get_all_mtg_hosts(only_enabled=True)
                    await callback.message.edit_text(
                        CHOOSE_PROXY_HOST_MESSAGE,
                        reply_markup=keyboards.create_mtg_host_selection_keyboard(
                            mtg_hosts, back_callback="back_to_main_menu"
                        ),
                    )
            else:
                await _show_vpn_purchase_plans(callback, action="new", key_id=0)
        elif action == "extend":
            await extend_key_handler(callback)
        else:
            await back_to_main_menu_handler(callback, state)

    @user_router.message(PaymentProcess.waiting_for_email)
    async def process_email_handler(message: types.Message, state: FSMContext):
        if is_valid_email(message.text):
            await state.update_data(customer_email=message.text)
            await message.answer(f"✅ Email принят: {message.text}")
            await _create_yookassa_payment(message, state)
        else:
            await message.answer("❌ Неверный формат email. Попробуйте еще раз.")

    @user_router.callback_query(
        PaymentProcess.waiting_for_email, F.data == "skip_email"
    )
    async def skip_email_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.update_data(customer_email=None)
        await _create_yookassa_payment(callback, state)

    async def show_payment_options(message: types.Message, state: FSMContext):
        data = await state.get_data()
        user_data = get_user(message.chat.id)
        plan = get_plan_by_id(data.get("plan_id"))

        if not plan:
            await message.edit_text("❌ Ошибка: Тариф не найден.")
            await state.clear()
            return

        price = Decimal(str(plan["price"]))
        final_price = price
        discount_applied = False
        message_text = CHOOSE_PAYMENT_METHOD_MESSAGE

        if user_data.get("referred_by") and user_data.get("total_spent", 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)

            if discount_percentage > 0:
                discount_amount = (price * discount_percentage / 100).quantize(
                    Decimal("0.01")
                )
                final_price = price - discount_amount

                message_text = (
                    f"🎉 Как приглашенному пользователю, на вашу первую покупку предоставляется скидка {discount_percentage_str}%!\n"
                    f"Старая цена: <s>{price:.2f} RUB</s>\n"
                    f"<b>Новая цена: {final_price:.2f} RUB</b>\n\n"
                ) + CHOOSE_PAYMENT_METHOD_MESSAGE

        await state.update_data(final_price=float(final_price))

        methods = get_active_payment_methods(
            context_key=_build_payment_context(
                data.get("service_type"), data.get("host_name")
            ),
            plan_id=data.get("plan_id"),
        )
        if not methods:
            await message.edit_text(
                "❌ Нет доступных способов оплаты для данного тарифа. Обратитесь в поддержку."
            )
            await state.clear()
            return

        await message.edit_text(
            message_text,
            reply_markup=keyboards.create_payment_method_keyboard(
                payment_methods=methods,
                action=data.get("action"),
                key_id=data.get("key_id"),
            ),
        )
        await state.set_state(PaymentProcess.waiting_for_payment_method)

    async def _render_payment_methods(
        target_message: types.Message,
        state: FSMContext,
        warning_text: str | None = None,
    ):
        data = await state.get_data()
        message_text = warning_text or CHOOSE_PAYMENT_METHOD_MESSAGE
        await target_message.edit_text(
            message_text,
            reply_markup=keyboards.create_payment_method_keyboard(
                payment_methods=get_active_payment_methods(
                    context_key=_build_payment_context(
                        data.get("service_type"), data.get("host_name")
                    ),
                    plan_id=data.get("plan_id"),
                ),
                action=data.get("action"),
                key_id=data.get("key_id"),
            ),
        )
        await state.set_state(PaymentProcess.waiting_for_payment_method)

    async def _create_yookassa_payment(
        source: types.Message | types.CallbackQuery, state: FSMContext
    ):
        if isinstance(source, types.CallbackQuery):
            callback = source
            message_obj = callback.message
            user_id = callback.from_user.id
        else:
            callback = None
            message_obj = source
            user_id = source.chat.id

        data = await state.get_data()
        user_data = get_user(user_id)

        plan_id = data.get("plan_id")
        plan = get_plan_by_id(plan_id)
        if not plan:
            if callback:
                await callback.message.answer("Произошла ошибка при выборе тарифа.")
            else:
                await message_obj.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan["price"]))
        price_rub = base_price

        if user_data.get("referred_by") and user_data.get("total_spent", 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(
                    Decimal("0.01")
                )
                price_rub = base_price - discount_amount

        customer_email = data.get("customer_email")
        if not customer_email:
            customer_email = get_setting("receipt_email")

        host_name = data.get("host_name")
        action = data.get("action")
        key_id = data.get("key_id")
        months = plan["months"]

        try:
            price_str_for_api = f"{price_rub:.2f}"
            price_float_for_metadata = float(price_rub)

            receipt = None
            if customer_email and is_valid_email(customer_email):
                receipt = {
                    "customer": {"email": customer_email},
                    "items": [
                        {
                            "description": f"Подписка на {months} мес.",
                            "quantity": "1.00",
                            "amount": {"value": price_str_for_api, "currency": "RUB"},
                            "vat_code": "1",
                        }
                    ],
                }

            payment_payload = {
                "amount": {"value": price_str_for_api, "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": f"https://t.me/{TELEGRAM_BOT_USERNAME}",
                },
                "capture": True,
                "description": f"Подписка на {months} мес.",
                "metadata": {
                    "user_id": user_id,
                    "months": months,
                    "price": price_float_for_metadata,
                    "action": action,
                    "key_id": key_id,
                    "host_name": host_name,
                    "plan_id": plan_id,
                    "customer_email": customer_email,
                    "payment_method": "YooKassa",
                },
            }
            if receipt:
                payment_payload["receipt"] = receipt

            payment = Payment.create(payment_payload, uuid.uuid4())
            await state.clear()

            if callback:
                await callback.message.edit_text(
                    "Нажмите на кнопку ниже для оплаты:",
                    reply_markup=keyboards.create_payment_keyboard(
                        payment.confirmation.confirmation_url
                    ),
                )
            else:
                await message_obj.answer(
                    "Нажмите на кнопку ниже для оплаты:",
                    reply_markup=keyboards.create_payment_keyboard(
                        payment.confirmation.confirmation_url
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to create YooKassa payment: {e}", exc_info=True)
            if callback:
                await callback.message.answer("Не удалось создать ссылку на оплату.")
            else:
                await message_obj.answer("Не удалось создать ссылку на оплату.")
            await state.clear()

    @user_router.callback_query(F.data == "back_to_email_prompt")
    @user_router.callback_query(F.data == "back_to_payment_methods")
    async def back_to_payment_methods_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        try:
            await callback.answer()
            await _render_payment_methods(callback.message, state)
        except Exception as e:
            if "message is not modified" in str(e):
                await callback.answer()
            else:
                logger.warning(f"Error in back_to_payment_methods (edit failed): {e}")
                try:
                    await callback.message.delete()
                except TelegramBadRequest as delete_error:
                    logger.debug(
                        f"Could not delete message before payment methods resend: {delete_error}"
                    )
                data = await state.get_data()
                await callback.message.answer(
                    CHOOSE_PAYMENT_METHOD_MESSAGE,
                    reply_markup=keyboards.create_payment_method_keyboard(
                        payment_methods=get_active_payment_methods(
                            context_key=_build_payment_context(
                                data.get("service_type"), data.get("host_name")
                            ),
                            plan_id=data.get("plan_id"),
                        ),
                        action=data.get("action"),
                        key_id=data.get("key_id"),
                    ),
                )
                await state.set_state(PaymentProcess.waiting_for_payment_method)

    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method, F.data == "pay_yookassa"
    )
    async def create_yookassa_payment_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer()
        await state.update_data(selected_payment_method="yookassa")
        if str(get_setting("email_prompt_enabled")).lower() == "true":
            await callback.message.edit_text(
                "📧 Введите email для отправки чека.\n\n"
                "Если не хотите указывать почту, нажмите кнопку ниже.",
                reply_markup=keyboards.create_skip_email_keyboard(),
            )
            await state.set_state(PaymentProcess.waiting_for_email)
            return

        await state.update_data(customer_email=None)
        await _create_yookassa_payment(callback, state)

    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method, F.data == "pay_stars"
    )
    async def create_stars_invoice_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer("Создаю счет Telegram Stars...")

        data = await state.get_data()
        user_id = callback.from_user.id

        plan_id = data.get("plan_id")
        plan = get_plan_by_id(plan_id)
        if not plan:
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        stars_rate_setting = get_setting("stars_rub_per_star")
        try:
            rub_per_star = (
                Decimal(str(stars_rate_setting)) if stars_rate_setting else Decimal("0")
            )
        except Exception:
            rub_per_star = Decimal("0")

        if rub_per_star <= 0:
            # Auto-calculate if not set: 1 Star ~= 0.013 USD. Use USDT rate + small margin.
            usdt_rub = await get_usdt_rub_rate()
            if usdt_rub:
                # Multiplier 0.016 (approx ~1.5-1.6 RUB/Star with padding)
                rub_per_star = usdt_rub * Decimal("0.016")
            else:
                await callback.message.edit_text(
                    "❌ Оплата Telegram Stars временно недоступна. (Администратор не указал курс)"
                )
                await state.clear()
                return

        price_rub = Decimal(str(data.get("final_price", plan["price"])))
        stars_amount = int(
            (price_rub / rub_per_star).to_integral_value(rounding=ROUND_CEILING)
        )
        if stars_amount <= 0:
            await callback.message.edit_text(
                "❌ Некорректная сумма для оплаты Telegram Stars."
            )
            await state.clear()
            return

        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "months": plan["months"],
            "price": float(price_rub),
            "action": data.get("action"),
            "key_id": data.get("key_id"),
            "host_name": data.get("host_name"),
            "plan_id": data.get("plan_id"),
            "customer_email": data.get("customer_email"),
            "payment_method": "Telegram Stars",
        }

        title = f"Подписка на {plan['months']} мес."
        description = f"Оплата подписки на {plan['months']} мес. через Telegram Stars"

        try:
            invoice_message = await callback.bot.send_invoice(
                chat_id=user_id,
                title=title,
                description=description,
                payload=payment_id,
                provider_token="",
                currency="XTR",
                prices=[types.LabeledPrice(label=title, amount=stars_amount)],
                reply_markup=InlineKeyboardBuilder()
                .button(text=f"Оплатить {stars_amount} ⭐️", pay=True)
                .button(text="🏠 В меню", callback_data="back_to_main_menu")
                .adjust(1)
                .as_markup(),
            )

            metadata["chat_id"] = user_id
            metadata["message_id"] = invoice_message.message_id
            create_pending_transaction(payment_id, user_id, float(price_rub), metadata)

            await state.clear()
        except Exception as e:
            logger.error(
                f"Failed to create Stars invoice for user {user_id}: {e}", exc_info=True
            )
            await callback.message.edit_text(
                "❌ Не удалось создать счет Telegram Stars. Попробуйте позже."
            )
            await state.clear()

    @user_router.pre_checkout_query()
    async def stars_pre_checkout_handler(
        pre_checkout_query: types.PreCheckoutQuery, bot: Bot
    ):
        payment_id = pre_checkout_query.invoice_payload
        if not payment_id or not _stars_is_pending_transaction(payment_id):
            await bot.answer_pre_checkout_query(
                pre_checkout_query_id=pre_checkout_query.id,
                ok=False,
                error_message="Счет недействителен или уже оплачен. Создайте новый счет.",
            )
            return

        await bot.answer_pre_checkout_query(
            pre_checkout_query_id=pre_checkout_query.id, ok=True
        )

    @user_router.message(F.successful_payment)
    async def stars_successful_payment_handler(message: types.Message, bot: Bot):
        sp = message.successful_payment
        payment_id = sp.invoice_payload
        paid_stars = int(sp.total_amount)
        telegram_payment_charge_id = sp.telegram_payment_charge_id

        metadata = _stars_complete_transaction(
            payment_id, paid_stars, telegram_payment_charge_id
        )
        if not metadata:
            logger.info(
                f"Stars: Ignoring duplicate or unknown payment for payload={payment_id}"
            )
            return

        try:
            processed_ok = await process_successful_payment(bot, metadata)
        except Exception:
            finalize_reserved_transaction(
                payment_id,
                success=False,
                metadata=metadata,
                payment_method="Telegram Stars",
                amount_currency=int(paid_stars),
                currency_name="XTR",
            )
            raise
        finalized = finalize_reserved_transaction(
            payment_id,
            success=processed_ok,
            metadata=metadata,
            payment_method="Telegram Stars",
            amount_currency=int(paid_stars),
            currency_name="XTR",
        )
        if not finalized:
            logger.error(
                "Stars: Failed to finalize reserved transaction %s after processing=%s",
                payment_id,
                processed_ok,
            )

    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method, F.data == "pay_cryptobot"
    )
    async def create_cryptobot_invoice_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer("Создаю счет в Crypto Pay...")

        data = await state.get_data()
        user_data = get_user(callback.from_user.id)

        plan_id = data.get("plan_id")
        user_id = data.get("user_id", callback.from_user.id)
        customer_email = data.get("customer_email")
        host_name = data.get("host_name")
        action = data.get("action")
        key_id = data.get("key_id")

        cryptobot_token = get_setting("cryptobot_token")
        if not cryptobot_token:
            logger.error(
                f"Attempt to create Crypto Pay invoice failed for user {user_id}: cryptobot_token is not set."
            )
            await callback.message.edit_text(
                "❌ Оплата криптовалютой временно недоступна. (Администратор не указал токен)."
            )
            await state.clear()
            return

        plan = get_plan_by_id(plan_id)
        if not plan:
            logger.error(
                f"Attempt to create Crypto Pay invoice failed for user {user_id}: Plan with id {plan_id} not found."
            )
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan["price"]))
        price_rub = base_price

        if user_data.get("referred_by") and user_data.get("total_spent", 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(
                    Decimal("0.01")
                )
                price_rub = base_price - discount_amount
        months = plan["months"]

        try:
            exchange_rate = await get_usdt_rub_rate()

            if not exchange_rate:
                logger.warning(
                    "Failed to get live exchange rate. Falling back to the manual setting or default."
                )

                # Fallback to setting if available, otherwise default to a safe high rate (e.g. 110)
                # Ideally, you should have a setting for this. For now, we'll try to get it or hardcode.
                manual_rate = get_setting("usdt_rub_rate")
                if manual_rate:
                    try:
                        exchange_rate = Decimal(manual_rate)
                    except (InvalidOperation, ValueError, TypeError) as rate_error:
                        logger.warning(
                            f"Invalid manual usdt_rub_rate setting '{manual_rate}': {rate_error}"
                        )

                if not exchange_rate:
                    # Final fallback
                    exchange_rate = Decimal("100.00")
                    logger.warning("Using hardcoded fallback rate: 100.00 RUB/USDT")

            if not exchange_rate:
                await callback.message.edit_text(
                    "❌ Не удалось получить курс валют. Попробуйте позже."
                )
                await state.clear()
                return

            margin = Decimal("1.03")
            price_usdt = (price_rub / exchange_rate * margin).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

            logger.info(
                f"Creating Crypto Pay invoice for user {user_id}. Plan price: {price_rub} RUB. Converted to: {price_usdt} USDT."
            )

            crypto = CryptoPay(cryptobot_token)
            payment_id = str(uuid.uuid4())
            metadata = {
                "user_id": user_id,
                "months": months,
                "price": float(price_rub),
                "action": action,
                "key_id": key_id,
                "host_name": host_name,
                "plan_id": plan_id,
                "customer_email": customer_email,
                "payment_method": "CryptoBot",
            }
            create_pending_transaction(payment_id, user_id, float(price_rub), metadata)

            invoice = await crypto.create_invoice(
                currency_type="fiat",
                fiat="RUB",
                amount=float(price_rub),
                description=f"Подписка на {months} мес.",
                payload=_cryptobot_build_payload(payment_id),
                expires_in=3600,
            )

            if not invoice or not invoice.pay_url:
                raise Exception("Failed to create invoice or pay_url is missing.")

            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_payment_keyboard(invoice.pay_url),
            )
            await state.clear()

        except Exception as e:
            logger.error(
                f"Failed to create Crypto Pay invoice for user {user_id}: {e}",
                exc_info=True,
            )
            await callback.message.edit_text(
                f"❌ Не удалось создать счет для оплаты криптовалютой.\n\n<pre>Ошибка: {e}</pre>"
            )
            await state.clear()

    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method, F.data == "pay_p2p"
    )
    async def start_p2p_payment_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer()

        user_id = callback.from_user.id
        if get_active_p2p_request_for_user(user_id):
            await callback.message.edit_text(
                "⚠️ <b>У вас уже есть активная заявка на проверку.</b>\n\n"
                "Пожалуйста, дождитесь ответа администратора по предыдущему платежу, прежде чем создавать новый.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        card = (
            get_setting("p2p_card_number")
            or "Не указаны реквизиты. Обратитесь в поддержку."
        )
        data = await state.get_data()

        request_id = str(uuid.uuid4())
        plan_id = data.get("plan_id")
        plan = get_plan_by_id(plan_id) if plan_id else None

        base_price = Decimal(str(plan["price"])) if plan else Decimal("0")
        final_price = Decimal(str(data.get("final_price", base_price)))
        price_rub = float(final_price)

        create_p2p_request(
            request_id,
            {
                "user_id": user_id,
                "months": plan["months"] if plan else 1,
                "price": price_rub,
                "action": data.get("action") or "new",
                "key_id": data.get("key_id") or 0,
                "host_name": data.get("host_name") or "",
                "plan_id": plan_id or 0,
                "customer_email": data.get("customer_email"),
                "submitted": False,
            },
        )

        await callback.message.edit_text(
            (
                "<b>Оплата по карте (P2P)</b>\n\n"
                f"Сумма к оплате: <b>{final_price:.2f} RUB</b>\n"
                f"Реквизиты для перевода: <code>{card}</code>\n\n"
                'После оплаты нажмите кнопку "✅ Подтвердить".'
            ),
            reply_markup=keyboards.create_p2p_payment_keyboard(request_id),
        )
        await state.update_data(payment_method="P2P", request_id=request_id)

    @user_router.callback_query(F.data.startswith("p2p_paid_"))
    async def notify_admin_paid(callback: types.CallbackQuery, state: FSMContext):
        request_id = callback.data.replace("p2p_paid_", "")
        admin_id = int(get_setting("admin_telegram_id"))
        user = get_user(callback.from_user.id)

        pending = get_p2p_request(request_id)
        if not pending:
            await callback.answer("Заявка устарела или не найдена.", show_alert=True)
            await show_main_menu(callback.message, edit_message=True)
            return

        if pending.get("submitted"):
            await callback.answer("Заявка уже отправлена на проверку.", show_alert=True)
            return

        # Block if user already has another active submitted request
        existing = get_active_p2p_request_for_user(callback.from_user.id)
        if existing and existing["request_id"] != request_id:
            await callback.answer(
                "У вас уже есть другая активная заявка.", show_alert=True
            )
            return

        mark_p2p_request_submitted(request_id)
        await callback.answer("Ваша заявка отправлена на проверку админу.")

        plan_id = pending.get("plan_id")
        plan = get_plan_by_id(plan_id) if plan_id else None
        plan_name = plan["plan_name"] if plan else "-"
        months = pending.get("months", 1)
        price = float(pending.get("price", 0))
        support_user = get_setting("support_user")

        await callback.message.edit_text(
            "✅ <b>Заявка отправлена!</b>\n\n"
            "Администратор проверит поступление средств и подтвердит выдачу ключа.\n"
            "Обычно это занимает не более 15 минут.",
            reply_markup=keyboards.create_p2p_submitted_keyboard(support_user),
        )

        from aiogram.utils.keyboard import InlineKeyboardBuilder

        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Подтвердить", callback_data=f"p2p_approve_{request_id}")
        builder.button(text="❌ Отклонить", callback_data=f"p2p_decline_{request_id}")
        builder.adjust(2)

        await callback.bot.send_message(
            admin_id,
            (
                "💳 <b>Новая P2P-заявка на оплату</b>\n\n"
                f"👤 Пользователь: @{user.get('username','-')} (<code>{callback.from_user.id}</code>)\n"
                f"📦 Тариф: {plan_name} (ID: {plan_id}, {months} мес.)\n"
                f"💰 Сумма: <b>{price:.2f} RUB</b>\n\n"
                "Выберите действие с помощью кнопок ниже."
            ),
            reply_markup=builder.as_markup(),
        )

    @user_router.message(Command(commands=["approve_p2p"]))
    async def admin_approve_p2p_handler(message: types.Message, bot: Bot):
        admin_id = int(get_setting("admin_telegram_id"))
        if message.from_user.id != admin_id:
            return
        parts = message.text.split("_")
        if len(parts) < 3:
            return
        request_id = "_".join(parts[2:])

        pending = get_p2p_request(request_id)
        if not pending:
            await message.answer("Заявка не найдена или уже подтверждена/отклонена.")
            return

        await message.answer("Платеж подтвержден. Выполняю выдачу ключа.")
        pending["payment_method"] = "P2P"
        success = await process_successful_payment(bot, pending)
        if success:
            delete_p2p_request(request_id)
        else:
            await message.answer(
                "Выдача не завершилась. Заявка сохранена, можно повторить подтверждение позже."
            )

    @user_router.message(Command(commands=["decline_p2p"]))
    async def admin_decline_p2p_handler(message: types.Message):
        admin_id = int(get_setting("admin_telegram_id"))
        if message.from_user.id != admin_id:
            return
        parts = message.text.split("_")
        if len(parts) < 3:
            return
        request_id = "_".join(parts[2:])

        pending = get_p2p_request(request_id)
        if not pending:
            await message.answer("Заявка не найдена или уже подтверждена/отклонена.")
            return

        delete_p2p_request(request_id)
        await message.bot.send_message(
            pending["user_id"],
            "❌ Оплата не подтверждена. Свяжитесь с поддержкой для уточнения причин.",
        )
        await message.answer("Пользователь получил отказ в ручном подтверждении.")

    @user_router.callback_query(F.data.startswith("p2p_approve_"))
    async def admin_approve_p2p_callback(callback: types.CallbackQuery, bot: Bot):
        admin_id = int(get_setting("admin_telegram_id"))
        if callback.from_user.id != admin_id:
            await callback.answer("У вас нет прав для этого действия.", show_alert=True)
            return

        request_id = callback.data.replace("p2p_approve_", "")
        pending = get_p2p_request(request_id)
        if not pending:
            await callback.answer(
                "Заявка не найдена или уже обработана.", show_alert=True
            )
            await callback.message.edit_text("⚠️ Заявка уже была обработана ранее.")
            return

        # Delete BEFORE processing to prevent double-approve race condition
        delete_p2p_request(request_id)

        await callback.answer("Платеж подтвержден.")
        await callback.message.edit_text(
            "✅ Платеж подтвержден. Выполняю выдачу ключа."
        )
        pending["payment_method"] = "P2P"
        success = await process_successful_payment(bot, pending)
        if success:
            await callback.message.edit_text(
                "✅ Платеж подтвержден. Ключ успешно выдан."
            )
        else:
            # Restore request so admin can retry
            create_p2p_request(request_id, pending)
            await callback.message.edit_text(
                "⚠️ Выдача завершилась ошибкой. Заявка восстановлена, подтверждение можно повторить."
            )

    @user_router.callback_query(F.data.startswith("p2p_decline_"))
    async def admin_decline_p2p_callback(callback: types.CallbackQuery):
        admin_id = int(get_setting("admin_telegram_id"))
        if callback.from_user.id != admin_id:
            await callback.answer("У вас нет прав для этого действия.", show_alert=True)
            return

        request_id = callback.data.replace("p2p_decline_", "")
        pending = get_p2p_request(request_id)
        if not pending:
            await callback.answer(
                "Заявка не найдена или уже обработана.", show_alert=True
            )
            await callback.message.edit_text("⚠️ Заявка уже была обработана ранее.")
            return

        delete_p2p_request(request_id)
        await callback.answer("Заявка отклонена.")
        await callback.message.edit_text("❌ Заявка отклонена. Пользователь уведомлен.")
        await callback.bot.send_message(
            pending["user_id"],
            "❌ Оплата не подтверждена. Свяжитесь с поддержкой для уточнения причин.",
        )

    @user_router.message(F.text)
    @registration_required
    async def unknown_message_handler(message: types.Message):
        if message.text.startswith("/"):
            await message.answer("Такой команды не существует. Попробуйте /start.")
        else:
            await message.answer(
                "Я не понимаю эту команду. Пожалуйста, используйте кнопки меню."
            )

    return user_router


async def process_successful_onboarding(
    callback: types.CallbackQuery, state: FSMContext
):
    await callback.answer("✅ Спасибо! Доступ предоставлен.")
    set_terms_agreed(callback.from_user.id)
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "Приятного использования!", reply_markup=keyboards.main_reply_keyboard
    )
    await show_main_menu(callback.message)


async def is_url_reachable(url: str) -> bool:
    pattern = re.compile(r"^(https?://)" r"(([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})" r"(/.*)?$")
    if not re.match(pattern, url):
        return False

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            async with session.head(url, allow_redirects=False) as response:
                if response.status < 300:
                    return True
                if 300 <= response.status < 400 and response.headers.get("Location"):
                    return True
                if response.status != 405:
                    return False

            async with session.get(url, allow_redirects=False) as response:
                if response.status < 300:
                    return True
                if 300 <= response.status < 400 and response.headers.get("Location"):
                    return True
                return False
    except Exception as e:
        logger.warning(f"URL validation failed for {url}. Error: {e}")
        return False


async def notify_admin_of_purchase(bot: Bot, metadata: dict):
    if get_setting("enable_admin_payment_notifications") == "false":
        return

    admin_id_str = get_setting("admin_telegram_id")
    if not admin_id_str:
        logger.warning(
            "Admin notification skipped: admin_telegram_id is not set in settings."
        )
        return

    admin_id = int(admin_id_str)

    try:
        user_id = metadata.get("user_id")
        months = metadata.get("months")
        price = float(metadata.get("price"))
        host_name = metadata.get("host_name")
        plan_id = metadata.get("plan_id")
        payment_method = metadata.get("payment_method", "Unknown")

        user_info = get_user(user_id)
        plan_info = get_plan_by_id(plan_id)

        username = user_info.get("username", "N/A") if user_info else "N/A"
        plan_name = (
            plan_info.get("plan_name", f"{months} мес.")
            if plan_info
            else f"{months} мес."
        )

        # Escape user provided values for HTML
        safe_username = html.quote(username)
        safe_host_name = html.quote(host_name)
        safe_plan_name = html.quote(plan_name)
        safe_payment_method = html.quote(payment_method)

        message_text = (
            "🎉 <b>Новая покупка!</b> 🎉\n\n"
            f"👤 <b>Пользователь:</b> @{safe_username} (ID: <code>{user_id}</code>)\n"
            f"🌍 <b>Сервер:</b> {safe_host_name}\n"
            f"📄 <b>Тариф:</b> {safe_plan_name}\n"
            f"💰 <b>Сумма:</b> {price:.2f} RUB\n"
            f"💳 <b>Способ оплаты:</b> {safe_payment_method}"
        )

        await bot.send_message(chat_id=admin_id, text=message_text, parse_mode="HTML")
        logger.info(f"Admin notification sent for a new purchase by user {user_id}.")

    except Exception as e:
        logger.error(
            f"Failed to send admin notification for purchase: {e}", exc_info=True
        )


async def notify_admin_of_trial(
    bot: Bot, user_id: int, host_name: str, duration_days: int
):
    if get_setting("enable_admin_trial_notifications") == "false":
        return

    admin_id_str = get_setting("admin_telegram_id")
    if not admin_id_str:
        return

    try:
        admin_id = int(admin_id_str)
        user_info = get_user(user_id)
        username = user_info.get("username", "N/A") if user_info else "N/A"

        safe_username = html.quote(username)
        safe_host_name = html.quote(host_name)

        message_text = (
            "🎁 <b>Взят пробный ключ!</b>\n\n"
            f"👤 <b>Пользователь:</b> @{safe_username} (ID: <code>{user_id}</code>)\n"
            f"🌍 <b>Сервер:</b> {safe_host_name}\n"
            f"⏳ <b>Срок:</b> {duration_days} дн."
        )

        await bot.send_message(chat_id=admin_id, text=message_text, parse_mode="HTML")
        logger.info(f"Admin notification sent for TRIAL by user {user_id}.")
    except Exception as e:
        logger.error(f"Failed to send admin notification for trial: {e}", exc_info=True)


async def get_usdt_rub_rate() -> Decimal | None:
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": "USDTRUB"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                price_str = data.get("price")
                if price_str:
                    logger.info(f"Got USDT RUB: {price_str}")
                    return Decimal(price_str)
                logger.error("Can't find 'price' in Binance response.")
                return None
    except Exception as e:
        logger.error(f"Error getting USDT RUB Binance rate: {e}", exc_info=True)
        return None


async def _create_mtg_proxy_after_payment(
    bot: "Bot",
    processing_message,
    user_id: int,
    host_name: str,
    plan_id: int,
    key_id: int,
    action: str,
    months: int,
    price: float,
    metadata: dict,
    plan: dict,
) -> bool:
    """Handle the MTG proxy creation/renewal after successful payment."""
    from shop_bot.data_manager.database import (
        get_user_keys,
        get_key_by_id,
        update_key_info,
        update_key_plan_id,
        get_or_create_subscription_token,
    )

    days = months * 30
    try:
        if action == "extend" and key_id:
            # Renewal: call MTG panel renew endpoint
            existing_key = get_key_by_id(key_id)
            if not existing_key or existing_key.get("user_id") != user_id:
                await processing_message.edit_text("❌ Ключ для продления не найден.")
                return False
            proxy_name = existing_key["key_email"]
            node_id = int(existing_key["xui_client_uuid"])
            current_exp = time_utils.parse_iso_to_msk(existing_key.get("expiry_date"))
            current_expiry_ms = (
                int(current_exp.timestamp() * 1000) if current_exp else 0
            )
            new_expiry_ms = await mtg_api.renew_proxy_for_user(
                host_name, proxy_name, node_id, days, current_expiry_ms
            )
            if not new_expiry_ms:
                await processing_message.edit_text(
                    "❌ Не удалось продлить прокси. Обратитесь в поддержку."
                )
                return False
            new_expiry_dt = time_utils.from_timestamp_ms(new_expiry_ms)
            update_key_info(key_id, new_expiry_dt)
            update_key_plan_id(key_id, int(plan_id))
            proxy_link = existing_key.get(
                "connection_string"
            ) or await mtg_api.get_proxy_link(host_name, proxy_name)
            used_key_id = key_id
        else:
            # New proxy
            key_num = get_next_key_number(user_id)
            proxy_name = f"user{user_id}key{key_num}mtg"
            result = await mtg_api.create_proxy_for_user(host_name, proxy_name, days)
            if not result:
                await processing_message.edit_text(
                    "❌ Не удалось создать прокси. Обратитесь в поддержку."
                )
                return False
            proxy_link = result["connection_string"]
            new_expiry_ms = result["expiry_timestamp_ms"]
            new_expiry_dt = time_utils.from_timestamp_ms(new_expiry_ms)
            used_key_id = add_new_key(
                user_id=user_id,
                host_name=host_name,
                xui_client_uuid=str(result["node_id"]),
                key_email=proxy_name,
                expiry_timestamp_ms=new_expiry_ms,
                connection_string=proxy_link,
                plan_id=plan_id,
                service_type="mtg",
            )
    except Exception as e:
        logger.error(
            f"MTG proxy creation/renewal failed for user {user_id}: {e}", exc_info=True
        )
        await processing_message.edit_text(
            "❌ Ошибка при создании прокси. Обратитесь в поддержку."
        )
        return False

    # Shared: referrals, stats, transaction log
    try:
        user_data = get_user(user_id)
        referrer_id = user_data.get("referred_by")
        if referrer_id:
            percentage = Decimal(get_setting("referral_percentage") or "0")
            reward = (Decimal(str(price)) * percentage / 100).quantize(Decimal("0.01"))
            if float(reward) > 0:
                add_to_referral_balance(referrer_id, float(reward))
                try:
                    referrer_username = user_data.get("username", "пользователь")
                    await bot.send_message(
                        referrer_id,
                        f"🎉 Ваш реферал @{referrer_username} совершил покупку на сумму {price:.2f} RUB!\n"
                        f"💰 На ваш баланс начислено вознаграждение: {reward:.2f} RUB.",
                    )
                except Exception:
                    pass
        update_user_stats(user_id, price, months)
        user_info = get_user(user_id)
        log_transaction(
            username=user_info.get("username", "N/A") if user_info else "N/A",
            transaction_id=None,
            payment_id=str(uuid.uuid4()),
            user_id=user_id,
            status="paid",
            amount_rub=price,
            amount_currency=None,
            currency_name=None,
            payment_method=metadata.get("payment_method", "Unknown"),
            metadata=json.dumps(
                {
                    "plan_id": plan_id,
                    "plan_name": (
                        plan.get("plan_name", "Unknown") if plan else "Unknown"
                    ),
                    "host_name": host_name,
                    "service_type": "mtg",
                    "customer_email": metadata.get("customer_email"),
                }
            ),
        )
    except Exception as e:
        logger.error(
            f"MTG post-payment stats/log error for user {user_id}: {e}", exc_info=True
        )

    await processing_message.delete()

    # Determine display key number (count only MTG proxy keys)
    all_mtg_keys = [k for k in get_user_keys(user_id) if k.get("service_type") == "mtg"]
    displayed_key_number = len(all_mtg_keys)
    for idx, k in enumerate(all_mtg_keys):
        if int(k.get("key_id", 0)) == int(used_key_id or 0):
            displayed_key_number = idx + 1
            break

    final_text = get_proxy_purchase_success_text(
        action=action,
        key_number=displayed_key_number,
        expiry_date=new_expiry_dt,
        proxy_link=proxy_link,
    )
    await bot.send_message(
        chat_id=user_id,
        text=final_text,
        reply_markup=keyboards.create_proxy_info_keyboard(
            int(used_key_id or 0), proxy_link
        ),
        parse_mode="HTML",
    )
    await notify_admin_of_purchase(bot, metadata)
    return True


def _build_hosts_for_payment(
    user_id: int, action: str | None, host_name: str, key_id: int
) -> tuple[str, int | None, list[tuple[str, str]], str | None]:
    """Prepare target hosts and emails for payment fulfillment."""
    normalized_action = action if action and str(action) != "None" else "new"

    key_number: int | None = None
    if normalized_action == "new" or host_name == "ALL":
        key_number = get_next_key_number(user_id)

    hosts_to_process: list[tuple[str, str]] = []
    if host_name == "ALL":
        hosts_data = get_all_hosts(only_enabled=True)
        for host in hosts_data:
            email = f"user{user_id}-global-{host['host_name'].replace(' ', '').lower()}"
            hosts_to_process.append((host["host_name"], email))
        return normalized_action, key_number, hosts_to_process, None

    if normalized_action == "new":
        email = f"user{user_id}-key{key_number}-{host_name.replace(' ', '').lower()}"
        hosts_to_process.append((host_name, email))
        return normalized_action, key_number, hosts_to_process, None

    if normalized_action == "extend":
        key_data = get_key_by_id(key_id)
        if not key_data or key_data["user_id"] != user_id:
            return (
                normalized_action,
                key_number,
                [],
                "❌ Ошибка: ключ для продления не найден.",
            )
        hosts_to_process.append((host_name, key_data["key_email"]))
        return normalized_action, key_number, hosts_to_process, None

    return normalized_action, key_number, [], "❌ Неверное действие оплаты."


async def _execute_payment_for_hosts(
    user_id: int,
    purchase_host_name: str,
    action: str,
    plan_id: int,
    days_to_add: int,
    hosts_to_process: list[tuple[str, str]],
    key_id: int,
) -> tuple[list[dict], int | None]:
    """Create/extend keys on all target hosts and update DB."""
    results: list[dict] = []
    primary_key_id: int | None = None

    for h_name, h_email in hosts_to_process:
        try:
            existing_key_db = None
            if purchase_host_name == "ALL" or action == "new":
                # Reuse existing key on the same host to preserve trial/global clients.
                existing_key_db = _find_existing_xui_key_for_host(user_id, h_name)
                if existing_key_db:
                    h_email = existing_key_db["key_email"]

            res = await xui_api.create_or_update_key_on_host(
                host_name=h_name,
                email=h_email,
                days_to_add=days_to_add,
                telegram_id=str(user_id),
            )
            if not res:
                continue

            results.append(res)
            if existing_key_db:
                expiry_datetime = time_utils.from_timestamp_ms(
                    res["expiry_timestamp_ms"]
                )
                update_key_info(
                    existing_key_db["key_id"],
                    expiry_datetime,
                    res["connection_string"],
                    xui_client_uuid=res.get("client_uuid"),
                )
                if action == "new":
                    update_key_plan_id(existing_key_db["key_id"], int(plan_id))
                if purchase_host_name != "ALL" and primary_key_id is None:
                    primary_key_id = int(existing_key_db["key_id"])
            elif action == "new":
                new_key_id = add_new_key(
                    user_id,
                    h_name,
                    res["client_uuid"],
                    res["email"],
                    res["expiry_timestamp_ms"],
                    res["connection_string"],
                    int(plan_id),
                )
                if (
                    purchase_host_name != "ALL"
                    and primary_key_id is None
                    and new_key_id is not None
                ):
                    primary_key_id = int(new_key_id)
            elif action == "extend" and purchase_host_name != "ALL":
                expiry_datetime = time_utils.from_timestamp_ms(
                    res["expiry_timestamp_ms"]
                )
                update_key_info(key_id, expiry_datetime, res["connection_string"])
                update_key_plan_id(key_id, int(plan_id))
                if primary_key_id is None:
                    primary_key_id = int(key_id)
        except Exception as e:
            logger.error(f"Failed to process key on host {h_name}: {e}")

    return results, primary_key_id


async def process_successful_payment(bot: Bot, metadata: dict) -> bool:
    pending_flag_set = False
    try:
        logger.info(
            f"Processing successful payment for user {metadata.get('user_id')}: {metadata}"
        )

        user_id = int(metadata["user_id"])

        # ========== RACE CONDITION PROTECTION ==========
        # Atomically acquire the per-user processing flag.
        if not set_pending_payment(user_id, True):
            logger.warning(
                f"Payment already being processed for user {user_id}. Ignoring duplicate webhook."
            )
            return False
        pending_flag_set = True
        # ===============================================

        months = int(metadata["months"])
        price = float(metadata["price"])
        action = metadata["action"]
        key_id = int(metadata["key_id"])
        host_name = metadata["host_name"]
        plan_id = int(metadata["plan_id"])
        customer_email = metadata.get("customer_email")
        payment_method = metadata.get("payment_method")

        chat_id_to_delete = metadata.get("chat_id")
        message_id_to_delete = metadata.get("message_id")

        # Additional safety check for keys
        if action in ["extend", "new"] and not host_name:
            logger.error(
                f"Missing host_name in metadata for action {action}: {metadata}"
            )
            await bot.send_message(
                user_id,
                "❌ Произошла ошибка при обработке платежа: не указан сервер. Обратитесь в поддержку.",
            )
            return False

        plan = get_plan_by_id(plan_id)
        if not plan:
            logger.error(f"Plan {plan_id} not found during payment processing")
            await bot.send_message(
                user_id, "❌ Ошибка: Тариф не найден. Обратитесь в поддержку."
            )
            return False

        months = plan[
            "months"
        ]  # Re-assign months from plan, as it might be different from metadata['months'] for some payment methods
        service_type = plan.get("service_type", "xui")

    except (ValueError, TypeError) as e:
        logger.error(
            f"FATAL: Could not parse metadata. Error: {e}. Metadata: {metadata}"
        )
        if "user_id" in metadata:
            set_pending_payment(int(metadata["user_id"]), False)
        return False
    except Exception as e:
        logger.error(
            f"An unexpected error occurred during initial payment processing for user {metadata.get('user_id')}: {e}",
            exc_info=True,
        )
        _err_user_id = metadata.get("user_id")
        if _err_user_id:
            set_pending_payment(int(_err_user_id), False)
            try:
                await bot.send_message(
                    int(_err_user_id),
                    "❌ Произошла непредвиденная ошибка при обработке платежа. Пожалуйста, обратитесь в поддержку.",
                )
            except Exception:
                pass
        return False

    if chat_id_to_delete and message_id_to_delete:
        try:
            await bot.delete_message(
                chat_id=chat_id_to_delete, message_id=message_id_to_delete
            )
        except TelegramBadRequest as e:
            logger.warning(f"Could not delete payment message: {e}")

    processing_message = await bot.send_message(
        chat_id=user_id,
        text=f'✅ Оплата получена! Обрабатываю ваш запрос на сервере "{host_name}"...',
    )
    try:
        # ── MTG Proxy branch ──────────────────────────────────────────────
        if service_type == "mtg":
            return await _create_mtg_proxy_after_payment(
                bot=bot,
                processing_message=processing_message,
                user_id=user_id,
                host_name=host_name,
                plan_id=int(plan_id),
                key_id=int(key_id),
                action=action,
                months=months,
                price=float(metadata.get("price", 0)),
                metadata=metadata,
                plan=plan,
            )
        # ─────────────────────────────────────────────────────────────────

        action, key_number, hosts_to_process, prep_error = _build_hosts_for_payment(
            user_id=user_id, action=action, host_name=host_name, key_id=key_id
        )
        if prep_error:
            await processing_message.edit_text(prep_error)
            return False

        days_to_add = months * 30
        results, primary_key_id = await _execute_payment_for_hosts(
            user_id=user_id,
            purchase_host_name=host_name,
            action=action,
            plan_id=int(plan_id),
            days_to_add=days_to_add,
            hosts_to_process=hosts_to_process,
            key_id=key_id,
        )

        if not results:
            await processing_message.edit_text(
                "❌ Не удалось создать/обновить ни одного ключа."
            )
            return False

        price = float(metadata.get("price"))

        user_data = get_user(user_id)
        referrer_id = user_data.get("referred_by")

        if referrer_id:
            percentage = Decimal(get_setting("referral_percentage") or "0")

            reward = (Decimal(str(price)) * percentage / 100).quantize(Decimal("0.01"))

            if float(reward) > 0:
                add_to_referral_balance(referrer_id, float(reward))

                try:
                    referrer_username = user_data.get("username", "пользователь")
                    await bot.send_message(
                        referrer_id,
                        f"🎉 Ваш реферал @{referrer_username} совершил покупку на сумму {price:.2f} RUB!\n"
                        f"💰 На ваш баланс начислено вознаграждение: {reward:.2f} RUB.",
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not send referral reward notification to {referrer_id}: {e}"
                    )

        update_user_stats(user_id, price, months)

        user_info = get_user(user_id)

        provider_payment_id = metadata.get("provider_payment_id")
        payment_id_for_log = (
            str(provider_payment_id).strip()
            if provider_payment_id
            else str(uuid.uuid4())
        )

        log_username = user_info.get("username", "N/A") if user_info else "N/A"
        log_status = "paid"
        log_amount_rub = float(price)
        log_method = metadata.get("payment_method", "Unknown")

        log_metadata = json.dumps(
            {
                "plan_id": metadata.get("plan_id"),
                "plan_name": plan.get("plan_name", "Unknown") if plan else "Unknown",
                "host_name": metadata.get("host_name"),
                "customer_email": metadata.get("customer_email"),
            }
        )

        existing_status = (
            _get_transaction_status(payment_id_for_log) if provider_payment_id else None
        )
        if provider_payment_id and existing_status in {"pending", "processing", "paid"}:
            logger.info(
                "Skipping duplicate transaction insert for provider payment_id=%s status=%s user_id=%s",
                payment_id_for_log,
                existing_status,
                user_id,
            )
        else:
            log_transaction(
                username=log_username,
                transaction_id=None,
                payment_id=payment_id_for_log,
                user_id=user_id,
                status=log_status,
                amount_rub=log_amount_rub,
                amount_currency=None,
                currency_name=None,
                payment_method=log_method,
                metadata=log_metadata,
            )

        await processing_message.delete()

        # Prepare success message
        # If multiple results (ALL hosts), show generic success or first key.
        # Prefer showing subscription link if ALL.

        # Taking the first result for expiry/key_info display purposes
        first_res = results[0]
        connection_string = first_res["connection_string"]
        if not connection_string:
            logger.error(
                f"connection_string is None for user {user_id} on host {host_name}. "
                "Protocol may be VMess/Trojan which is not yet fully implemented."
            )
            connection_string = (
                "⚠️ Ключ создан, но ссылка недоступна. Обратитесь к администратору."
            )
        new_expiry_date = time_utils.from_timestamp_ms(first_res["expiry_timestamp_ms"])

        # Count only VPN (xui) keys for display numbering
        all_user_xui_keys = [
            k for k in get_user_keys(user_id) if k.get("service_type", "xui") != "mtg"
        ]
        displayed_key_number = None
        if action == "new":
            displayed_key_number = key_number
        else:
            try:
                effective_key_id = (
                    primary_key_id if primary_key_id is not None else key_id
                )
                for idx, k in enumerate(all_user_xui_keys):
                    if int(k.get("key_id", 0)) == int(effective_key_id):
                        displayed_key_number = idx + 1
                        break
            except Exception:
                displayed_key_number = None
        if displayed_key_number is None:
            displayed_key_number = len(all_user_xui_keys)

        final_text = get_purchase_success_text(
            action=action,
            key_number=int(displayed_key_number),
            expiry_date=new_expiry_date,
            connection_string=connection_string,
        )

        if host_name == "ALL":
            domain = get_setting("domain")
            user_token = get_or_create_subscription_token(user_id)
            plan = get_plan_by_id(metadata.get("plan_id")) if metadata else None
            plan_name = plan.get("plan_name") if isinstance(plan, dict) else None
            if not plan_name:
                plan_name = "—"

            final_text = (
                f"🎉 <b>Подписка активирована!</b>\n"
                f"<blockquote>📋 {plan_name}\n"
                f"📅 до {new_expiry_date.strftime('%d.%m.%Y')}</blockquote>\n"
            )

            if not user_token:
                # If token missing (legacy user?), try to generate one or warn
                # Since we can't easily generate here without importing database write logic, better notify admin or ask user to re-register/re-login.
                # Actually we can't re-login easily in bot.
                final_text += "\n\n⚠️ Ошибка: У вас отсутствует токен подписки. Пожалуйста, обратитесь к администратору."
            elif not domain:
                final_text += "\n\n⚠️ Не удалось сгенерировать ссылку. Администратор не настроил домен (Admin Panel -> Settings -> Ваш домен)."
            else:
                if not domain.startswith("http"):
                    sub_link = f"https://{domain}/sub/{user_token}"
                else:
                    sub_link = f"{domain}/sub/{user_token}"

                final_text += (
                    f"\n🌍 <b>Ссылка-подписка:</b>\n<code>{sub_link}</code>\n\n"
                    "<blockquote>Вставьте ссылку в Happ и при необходимости нажмите <b>Обновить подписку</b>.</blockquote>"
                )

            # Report failures if any
            failed_hosts = [
                h[0]
                for h in hosts_to_process
                if h[0] not in [r["host_name"] for r in results]
            ]
            if failed_hosts:
                final_text += (
                    f"\n\n❌ <b>Внимание:</b> Не удалось создать ключи на следующих серверах (свяжитесь с админом):\n- "
                    + "\n- ".join(failed_hosts)
                )

            await bot.send_message(
                chat_id=user_id,
                text=final_text,
                reply_markup=(
                    keyboards.create_global_sub_keyboard(user_token)
                    if user_token
                    else keyboards.create_back_to_menu_keyboard()
                ),
            )
        else:
            final_text += "\n\n<blockquote>📋 Скопируйте ключ → откройте Happ → нажмите <b>+</b> → вставьте ссылку из буфера обмена.</blockquote>"
            await bot.send_message(
                chat_id=user_id,
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(
                    primary_key_id if primary_key_id is not None else key_id,
                    connection_string,
                ),
            )

        await notify_admin_of_purchase(bot, metadata)
        return True

    except Exception as e:
        logger.error(
            f"Error processing payment for user {user_id} on host {host_name}: {e}",
            exc_info=True,
        )
        await processing_message.edit_text("❌ Ошибка при выдаче ключа.")
        return False
    finally:
        if pending_flag_set:
            set_pending_payment(user_id, False)

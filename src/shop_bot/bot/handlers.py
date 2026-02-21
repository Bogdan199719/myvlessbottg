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

from urllib.parse import urlencode
from hmac import compare_digest
from functools import wraps
from yookassa import Payment
from io import BytesIO
from datetime import datetime, timedelta
from aiosend import CryptoPay, TESTNET
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING
from typing import Dict
from shop_bot.utils import time_utils

from pytonconnect import TonConnect
from pytonconnect.exceptions import UserRejectsError

from aiogram import Bot, Router, F, types, html
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.bot import keyboards
from shop_bot.utils import time_utils
from shop_bot.modules import xui_api
from shop_bot.data_manager.database import (
    get_user, add_new_key, get_user_keys, update_user_stats,
    register_user_if_not_exists, get_next_key_number, get_key_by_id,
    update_key_info, update_key_plan_id, set_trial_used, set_terms_agreed, get_setting, get_all_hosts,
    get_plans_for_host, get_plan_by_id, log_transaction, get_referral_count,
    add_to_referral_balance, create_pending_transaction, get_all_users,
    set_referral_balance, set_referral_balance_all, DB_FILE, get_user_paid_keys, get_user_trial_keys,
    set_pending_payment, get_pending_payment_status, clear_all_pending_payments,
    get_or_create_subscription_token
)

from shop_bot.config import (
    get_profile_text, get_vpn_active_text, VPN_INACTIVE_TEXT, VPN_NO_DATA_TEXT,
    get_key_info_text, CHOOSE_PAYMENT_METHOD_MESSAGE, get_purchase_success_text
)

TELEGRAM_BOT_USERNAME = None
ADMIN_ID = None
CRYPTO_BOT_TOKEN = get_setting("cryptobot_token")
p2p_pending_requests = {}

logger = logging.getLogger(__name__)
admin_router = Router()
user_router = Router()

class KeyPurchase(StatesGroup):
    waiting_for_host_selection = State()
    waiting_for_plan_selection = State()

class Onboarding(StatesGroup):
    waiting_for_subscription_and_agreement = State()

class PaymentProcess(StatesGroup):
    waiting_for_email = State()
    waiting_for_payment_method = State()

def get_active_payment_methods() -> Dict[str, bool]:
    """Dynamically fetch active payment methods from DB settings."""
    methods = {}
    
    # YooKassa
    if get_setting("yookassa_enabled") == "true":
        shop_id = get_setting("yookassa_shop_id")
        secret = get_setting("yookassa_secret_key")
        if shop_id and secret:
            methods["yookassa"] = True

    # Telegram Stars
    if get_setting("stars_enabled") == "true":
         methods["stars"] = True
         
    # Heleket
    if get_setting("heleket_enabled") == "true":
        mid = get_setting("heleket_merchant_id")
        key = get_setting("heleket_api_key")
        if mid and key:
             methods["heleket"] = True

    # CryptoBot
    if get_setting("cryptobot_enabled") == "true":
        token = get_setting("cryptobot_token")
        if token:
             methods["cryptobot"] = True
             
    # TON Connect
    if get_setting("tonconnect_enabled") == "true":
         addr = get_setting("ton_wallet_address")
         key = get_setting("tonapi_key")
         if addr and key:
              methods["tonconnect"] = True
              
    return methods

def has_active_global_subscription(active_paid_keys: list[dict]) -> bool:
    """Detect active global subscription based on global plan ids and non-expired keys."""
    try:
        global_plan_ids = {
            int(p['plan_id'])
            for p in get_plans_for_host('ALL')
            if p.get('plan_id') is not None
        }
    except Exception as e:
        logger.warning(f"Error getting global plan IDs: {e}")
        global_plan_ids = set()

    if not global_plan_ids:
        # Fallback to legacy heuristic
        return len(active_paid_keys) >= 2

    for key in active_paid_keys:
        try:
            if int(key.get('plan_id', 0)) in global_plan_ids:
                return True
        except (ValueError, TypeError) as e:
            logger.debug(f"Error checking plan_id: {e}")
            continue
    return False

def get_active_paid_keys(user_id: int) -> list[dict]:
    now = time_utils.get_msk_now()
    active_keys: list[dict] = []
    for key in get_user_paid_keys(user_id):
        expiry_dt = time_utils.parse_iso_to_msk(key.get('expiry_date'))
        if expiry_dt and expiry_dt > now:
            active_keys.append(key)
    return active_keys

def _stars_is_pending_transaction(payment_id: str) -> bool:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM transactions WHERE payment_id = ?", (payment_id,))
            row = cursor.fetchone()
            return bool(row and row[0] == 'pending')
    except sqlite3.Error as e:
        logger.error(f"Stars: Failed to check pending transaction {payment_id}: {e}")
        return False

def _stars_complete_transaction(payment_id: str, paid_stars: int, telegram_payment_charge_id: str | None) -> dict | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT metadata FROM transactions WHERE payment_id = ? AND status = 'pending'",
                (payment_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            metadata_str = row['metadata']
            try:
                metadata = json.loads(metadata_str) if metadata_str else {}
            except json.JSONDecodeError:
                metadata = {}

            if telegram_payment_charge_id:
                metadata['telegram_payment_charge_id'] = telegram_payment_charge_id
            metadata['paid_stars'] = int(paid_stars)
            metadata['payment_method'] = 'Telegram Stars'

            cursor.execute(
                "UPDATE transactions SET status = 'paid', amount_currency = ?, currency_name = 'XTR', payment_method = 'Telegram Stars', metadata = ? WHERE payment_id = ? AND status = 'pending'",
                (int(paid_stars), json.dumps(metadata), payment_id)
            )
            if cursor.rowcount != 1:
                return None
            conn.commit()
            return metadata
    except sqlite3.Error as e:
        logger.error(f"Stars: Failed to complete transaction {payment_id}: {e}")
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
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None

async def show_main_menu(message: types.Message, edit_message: bool = False):
    user_id = message.chat.id
    user_db_data = get_user(user_id)
    user_keys = get_user_keys(user_id)
    
    trial_available = not (user_db_data and user_db_data.get('trial_used'))
    is_admin = str(user_id) == str(get_setting("admin_telegram_id") or "")

    text = "🏠 <b>Главное меню</b>\n\nВыберите действие:"
    keyboard = keyboards.create_main_menu_keyboard(user_keys, trial_available, is_admin)
    
    if edit_message:
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            pass
    else:
        await message.answer(text, reply_markup=keyboard)

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
            message_text = "Пожалуйста, для начала работы со мной, отправьте команду /start"
            if isinstance(event, types.CallbackQuery):
                await event.answer(message_text, show_alert=True)
            else:
                await event.answer(message_text)
    return decorated_function

def get_user_router() -> Router:
    user_router = Router()

    @user_router.message(CommandStart())
    async def start_handler(message: types.Message, state: FSMContext, bot: Bot, command: CommandObject):
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        referrer_id = None

        if command.args and command.args.startswith('ref_'):
            try:
                potential_referrer_id = int(command.args.split('_')[1])
                if potential_referrer_id != user_id:
                    referrer_id = potential_referrer_id
                    logger.info(f"New user {user_id} was referred by {referrer_id}")
            except (IndexError, ValueError):
                logger.warning(f"Invalid referral code received: {command.args}")
                
        register_user_if_not_exists(user_id, username, referrer_id)
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        user_data = get_user(user_id)

        if user_data and user_data.get('agreed_to_terms'):
            await message.answer(
                f"👋 Снова здравствуйте, {html.bold(message.from_user.full_name)}!",
                reply_markup=keyboards.main_reply_keyboard
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
        
        show_welcome_screen = (is_subscription_forced and channel_url) or (terms_url and privacy_url)

        if not show_welcome_screen:
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        welcome_parts = ["<b>Добро пожаловать!</b>\n"]
        
        if is_subscription_forced and channel_url:
            welcome_parts.append("Для доступа ко всем функциям, пожалуйста, подпишитесь на наш канал.\n")
        
        if terms_url and privacy_url:
            welcome_parts.append("Также необходимо ознакомиться с нашими Условиями использования и Политикой конфиденциальности.")
        elif terms_url:
            welcome_parts.append("Также необходимо ознакомиться и принять наши Условия использования.")
        elif privacy_url:
            welcome_parts.append("Также необходимо ознакомиться с нашей Политикой конфиденциальности.")

        welcome_parts.append("\nПосле этого нажмите кнопку ниже.")
        final_text = "\n".join(welcome_parts)
        
        await message.answer(
            final_text,
            reply_markup=keyboards.create_welcome_keyboard(
                channel_url=channel_url,
                is_subscription_forced=is_subscription_forced,
                terms_url=terms_url,
                privacy_url=privacy_url
            ),
            disable_web_page_preview=True
        )
        await state.set_state(Onboarding.waiting_for_subscription_and_agreement)

    @user_router.callback_query(Onboarding.waiting_for_subscription_and_agreement, F.data == "check_subscription_and_agree")
    async def check_subscription_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        user_id = callback.from_user.id
        channel_url = get_setting("channel_url")
        is_subscription_forced = get_setting("force_subscription") == "true"

        if not is_subscription_forced or not channel_url:
            await process_successful_onboarding(callback, state)
            return
            
        try:
            if '@' not in channel_url and 't.me/' not in channel_url:
                logger.error(f"Неверный формат URL канала: {channel_url}. Пропускаем проверку подписки.")
                await process_successful_onboarding(callback, state)
                return

            channel_id = '@' + channel_url.split('/')[-1] if 't.me/' in channel_url else channel_url
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            
            if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await process_successful_onboarding(callback, state)
            else:
                await callback.answer("Вы еще не подписались на канал. Пожалуйста, подпишитесь и попробуйте снова.", show_alert=True)

        except Exception as e:
            logger.error(f"Ошибка при проверке подписки для user_id {user_id} на канал {channel_url}: {e}")
            await callback.answer("Не удалось проверить подписку. Убедитесь, что бот является администратором канала. Попробуйте позже.", show_alert=True)

    @user_router.message(Onboarding.waiting_for_subscription_and_agreement)
    async def onboarding_fallback_handler(message: types.Message):
        await message.answer("Пожалуйста, выполните требуемые действия и нажмите на кнопку в сообщении выше.")

    @user_router.message(F.text == "🏠 Главное меню")
    @registration_required
    async def main_menu_handler(message: types.Message, state: FSMContext):
        await state.clear()
        await show_main_menu(message)

    @user_router.callback_query(F.data.startswith("global_qr_"))
    async def show_qr_token_handler(callback: types.CallbackQuery, bot: Bot):
        token = callback.data[len("global_qr_"):]
        domain = get_setting("domain")
        user_id = callback.from_user.id
        expected_token = get_or_create_subscription_token(user_id)
        active_paid_keys = get_active_paid_keys(user_id)

        
        if not domain:
             await callback.answer("Домен не настроен", show_alert=True)
             return

        if not has_active_global_subscription(active_paid_keys):
             await callback.answer("У вас нет активной платной мультиподписки.", show_alert=True)
             return
        
        if not expected_token or token != expected_token:
             await callback.answer("Ссылка подписки недействительна. Откройте профиль и получите новую.", show_alert=True)
             return

        if not domain.startswith('http'):
             sub_link = f"https://{domain}/sub/{token}"
        else:
             sub_link = f"{domain}/sub/{token}"
        
        img = qrcode.make(sub_link)
        bio = BytesIO()
        img.save(bio, 'PNG')
        bio.seek(0)
        
        await bot.send_photo(
             chat_id=callback.from_user.id, 
             photo=types.BufferedInputFile(bio.getvalue(), filename="qrcode.png"),
             caption="📱 <b>QR-код для подписки</b>"
        )
        await callback.answer()

    @user_router.callback_query(F.data.startswith("global_link_"))
    async def show_link_token_handler(callback: types.CallbackQuery):
        token = callback.data[len("global_link_"):]
        domain = get_setting("domain")
        user_id = callback.from_user.id
        expected_token = get_or_create_subscription_token(user_id)
        active_paid_keys = get_active_paid_keys(user_id)


        if not domain:
            await callback.answer("Домен не настроен", show_alert=True)
            return

        if not has_active_global_subscription(active_paid_keys):
            await callback.answer("У вас нет активной платной мультиподписки.", show_alert=True)
            return
        
        if not expected_token or token != expected_token:
            await callback.answer("Ссылка подписки недействительна. Откройте профиль и получите новую.", show_alert=True)
            return

        if not str(domain).startswith('http'):
            sub_link = f"https://{domain}/sub/{token}"
        else:
            sub_link = f"{domain}/sub/{token}"

        await callback.message.answer(
            f"🔗 <b>Ссылка-подписка (оплаченные сервера):</b>\n<code>{sub_link}</code>",
            disable_web_page_preview=True
        )
        await callback.answer()

    @user_router.callback_query(F.data == "global_howto")
    async def howto_vless_global_handler(callback: types.CallbackQuery):
        await callback.message.edit_text(
            "<b>📖 Инструкция по подключению (Global Subscription):</b>\n\n"
            "1. Скопируйте ссылку-подписку (или отсканируйте QR-код).\n"
            "2. Скачайте приложение для вашего устройства (v2rayNG, V2Box, Streisand).\n"
            "3. Найдите раздел 'Подписки' (Subscription Group).\n"
            "4. Добавьте новую подписку, вставьте ссылку.\n"
            "5. Нажмите 'Обновить подписку' (Update Subscription).\n"
            "6. У вас появятся все доступные серверы.\n"
            "7. Выберите любой и подключитесь!",
            reply_markup=keyboards.create_howto_vless_keyboard()
        )

    @user_router.callback_query(F.data == "back_to_main_menu")
    @registration_required
    async def back_to_main_menu_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.clear()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_profile")
    @registration_required
    async def profile_handler_callback(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_db_data = get_user(user_id)
        user_keys = get_user_keys(user_id)
        paid_keys = get_user_paid_keys(user_id)
        trial_keys = get_user_trial_keys(user_id)
        if not user_db_data:
            await callback.answer("Не удалось получить данные профиля.", show_alert=True)
            return
        username = html.bold(user_db_data.get('username', 'Пользователь'))
        total_spent = user_db_data.get('total_spent', 0)
        now = time_utils.get_msk_now()
        active_paid_keys = []
        for key in paid_keys:
             dt = time_utils.parse_iso_to_msk(key.get('expiry_date'))
             if dt and dt > now:
                 active_paid_keys.append(key)

        active_trial_keys = []
        for key in trial_keys:
             dt = time_utils.parse_iso_to_msk(key.get('expiry_date'))
             if dt and dt > now:
                 active_trial_keys.append(key)

        active_any_keys = []
        for key in user_keys:
             dt = time_utils.parse_iso_to_msk(key.get('expiry_date'))
             if dt and dt > now:
                 active_any_keys.append(key)
        
        is_global_active = has_active_global_subscription(active_paid_keys)

        if is_global_active and active_paid_keys:
            # Для глобальной подписки берем минимальную дату истечения (самый «короткий» хост)
            min_expiry_date = min(time_utils.parse_iso_to_msk(k['expiry_date']) for k in active_paid_keys if k.get('expiry_date'))
            time_left = min_expiry_date - now
            vpn_status_text = get_vpn_active_text(time_left.days, time_left.seconds // 3600)
        else:
            if active_any_keys:
                latest_key = max(active_any_keys, key=lambda k: time_utils.parse_iso_to_msk(k['expiry_date']))
                latest_expiry_date = time_utils.parse_iso_to_msk(latest_key['expiry_date'])
                time_left = latest_expiry_date - now
                vpn_status_text = get_vpn_active_text(time_left.days, time_left.seconds // 3600)
            elif user_keys:
                vpn_status_text = VPN_INACTIVE_TEXT
            else:
                vpn_status_text = "ℹ️ <b>Статус VPN:</b> Активных подписок нет"
        
        domain = get_setting("domain")
        subscription_token = user_db_data.get('subscription_token')
        if not subscription_token:
            subscription_token = get_or_create_subscription_token(user_id)
        subscription_text = ""
        profile_kb = InlineKeyboardBuilder()

        if active_trial_keys:
            trial_lines = []
            for key in active_trial_keys:
                host_dt = time_utils.parse_iso_to_msk(key.get('expiry_date'))
                expiry_str = time_utils.format_msk(host_dt) if host_dt else "-"
                host_name = key.get('host_name', '-')
                trial_lines.append(f"- {host_name} (до {expiry_str})")
            subscription_text += "\n\n🎁 <b>Пробный доступ:</b>\n" + "\n".join(trial_lines)

        if user_keys:
            profile_kb.button(text="🔑 Мои ключи", callback_data="manage_keys")

        if active_paid_keys:
            valid_dates = [time_utils.parse_iso_to_msk(k.get('expiry_date')) for k in active_paid_keys]
            expiry_dates_msk = [d for d in valid_dates if d]

            min_expiry = min(expiry_dates_msk)
            min_expiry_str = time_utils.format_msk(min_expiry)
            
            if is_global_active:
                subscription_text += (
                    "\n\n💳 <b>Глобальная подписка:</b> Активна"
                    f"\n🌍 <b>Серверов:</b> {len(active_paid_keys)}"
                    f"\n📅 <b>Истекает:</b> {min_expiry_str}"
                    "\n\nПродление глобальной подписки продлевает доступ сразу на всех серверах."
                    "\nИспользуйте кнопки ниже для получения ссылки и QR-кода подписки."
                )
                if subscription_token:
                    profile_kb.button(text="🔗 Показать ссылку", callback_data=f"global_link_{subscription_token}")
                    profile_kb.button(text="📱 Показать QR", callback_data=f"global_qr_{subscription_token}")
                    profile_kb.button(text="📖 Инструкция", callback_data="global_howto")
            else:
                host_name = active_paid_keys[0].get('host_name', '-')
                subscription_text += (
                    "\n\n💳 <b>Платный доступ:</b> Активен"
                    f"\n🌍 <b>Сервер:</b> {host_name}"
                    f"\n📅 <b>Истекает:</b> {min_expiry_str}"
                )

        final_text = get_profile_text(username, total_spent, vpn_status_text) + subscription_text
        profile_kb.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
        profile_kb.adjust(1)
        await callback.message.edit_text(final_text, reply_markup=profile_kb.as_markup())

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
            reply_markup=keyboards.create_broadcast_cancel_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_message)

    @user_router.message(Broadcast.waiting_for_message)
    async def broadcast_message_received_handler(message: types.Message, state: FSMContext):
        message_dict = message.model_dump(mode='json', exclude_unset=True)
        await state.update_data(message_to_send=json.dumps(message_dict))

        await message.answer(
            "Сообщение получено. Хотите добавить к нему кнопку со ссылкой?",
            reply_markup=keyboards.create_broadcast_options_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_button_option)

    @user_router.callback_query(Broadcast.waiting_for_button_option, F.data == "broadcast_add_button")
    async def add_button_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await callback.message.edit_text(
            "Хорошо. Теперь отправьте мне текст для кнопки.",
            reply_markup=keyboards.create_broadcast_cancel_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_button_text)

    @user_router.message(Broadcast.waiting_for_button_text)
    async def button_text_received_handler(message: types.Message, state: FSMContext):
        await state.update_data(button_text=message.text)
        await message.answer(
            "Текст кнопки получен. Теперь отправьте ссылку (URL), куда она будет вести.",
            reply_markup=keyboards.create_broadcast_cancel_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_button_url)

    @user_router.message(Broadcast.waiting_for_button_url)
    async def button_url_received_handler(message: types.Message, state: FSMContext, bot: Bot):
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

    @user_router.callback_query(Broadcast.waiting_for_button_option, F.data == "broadcast_skip_button")
    async def skip_button_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.answer()
        await state.update_data(button_text=None, button_url=None)
        await show_broadcast_preview(callback.message, state, bot)

    async def show_broadcast_preview(message: types.Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        message_json = data.get('message_to_send')
        original_message = types.Message.model_validate_json(message_json)
        
        button_text = data.get('button_text')
        button_url = data.get('button_url')
        
        preview_keyboard = None
        if button_text and button_url:
            builder = InlineKeyboardBuilder()
            builder.button(text=button_text, url=button_url)
            preview_keyboard = builder.as_markup()

        await message.answer(
            "Вот так будет выглядеть ваше сообщение. Отправляем?",
            reply_markup=keyboards.create_broadcast_confirmation_keyboard()
        )
        
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=original_message.chat.id,
            message_id=original_message.message_id,
            reply_markup=preview_keyboard
        )

        await state.set_state(Broadcast.waiting_for_confirmation)

    @user_router.callback_query(Broadcast.waiting_for_confirmation, F.data == "confirm_broadcast")
    async def confirm_broadcast_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.message.edit_text("⏳ Начинаю рассылку... Это может занять некоторое время.")
        
        data = await state.get_data()
        message_json = data.get('message_to_send')
        original_message = types.Message.model_validate_json(message_json)
        
        button_text = data.get('button_text')
        button_url = data.get('button_url')
        
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
            user_id = user['telegram_id']
            if user.get('is_banned'):
                banned_count += 1
                continue
            
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=original_message.chat.id,
                    message_id=original_message.message_id,
                    reply_markup=final_keyboard
                )

                sent_count += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                failed_count += 1
                logger.warning(f"Failed to send broadcast message to user {user_id}: {e}")
        
        await callback.message.answer(
            f"✅ Рассылка завершена!\n\n"
            f"👍 Отправлено: {sent_count}\n"
            f"👎 Не удалось отправить: {failed_count}\n"
            f"🚫 Пропущено (забанены): {banned_count}"
        )
        await show_main_menu(callback.message)

    @user_router.callback_query(StateFilter(Broadcast), F.data == "cancel_broadcast")
    async def cancel_broadcast_handler(callback: types.CallbackQuery, state: FSMContext):
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
                reply_markup=keyboards.create_back_to_menu_keyboard()
            )
            return
        
        user_id = callback.from_user.id
        user_data = get_user(user_id)
        bot_username = (await callback.bot.get_me()).username
        
        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        referral_count = get_referral_count(user_id)
        balance = user_data.get('referral_balance', 0)

        text = (
            "🤝 <b>Реферальная программа</b>\n\n"
            "Приглашайте друзей и получайте вознаграждение с <b>каждой</b> их покупки!\n\n"
            f"<b>Ваша реферальная ссылка:</b>\n<code>{referral_link}</code>\n\n"
            f"<b>Приглашено пользователей:</b> {referral_count}\n"
            f"<b>Ваш баланс:</b> {balance:.2f} RUB"
        )

        builder = InlineKeyboardBuilder()
        if balance >= 100:
            builder.button(text="💸 Оставить заявку на вывод", callback_data="withdraw_request")
        builder.button(text="⬅️ Назад", callback_data="back_to_main_menu")
        await callback.message.edit_text(
            text, reply_markup=builder.as_markup()
        )

    @user_router.callback_query(F.data == "withdraw_request")
    @registration_required
    async def withdraw_request_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await callback.message.edit_text(
            "Пожалуйста, отправьте ваши реквизиты для вывода (номер карты или номер телефона и банк):",
            reply_markup=keyboards.create_back_to_menu_keyboard()
        )
        await state.set_state(WithdrawStates.waiting_for_details)

    @user_router.message(WithdrawStates.waiting_for_details)
    @registration_required
    async def process_withdraw_details(message: types.Message, state: FSMContext):
        user_id = message.from_user.id
        user = get_user(user_id)
        balance = user.get('referral_balance', 0)
        details = message.text.strip()
        if balance < 100:
            await message.answer("❌ Ваш баланс менее 100 руб. Вывод недоступен.")
            await state.clear()
            return

        admin_id_str = get_setting("admin_telegram_id")
        if not admin_id_str:
            await message.answer("❌ Ошибка: Администратор не настроен. Обратитесь в поддержку.")
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

    @user_router.message(Command(commands=["approve_withdraw"]))
    async def approve_withdraw_handler(message: types.Message):
        admin_id_str = get_setting("admin_telegram_id")
        if not admin_id_str:
            return
        admin_id = int(admin_id_str)
        if message.from_user.id != admin_id:
            return
        try:
            user_id = int(message.text.split("_")[-1])
            user = get_user(user_id)
            balance = user.get('referral_balance', 0)
            if balance < 100:
                await message.answer("Баланс пользователя менее 100 руб.")
                return
            set_referral_balance(user_id, 0)
            set_referral_balance_all(user_id, 0)
            await message.answer(f"✅ Выплата {balance:.2f} RUB пользователю {user_id} подтверждена.")
            await message.bot.send_message(
                user_id,
                f"✅ Ваша заявка на вывод {balance:.2f} RUB одобрена. Деньги будут переведены в ближайшее время."
            )
        except Exception as e:
            await message.answer(f"Ошибка: {e}")

    @user_router.message(Command(commands=["decline_withdraw"]))
    async def decline_withdraw_handler(message: types.Message):
        admin_id_str = get_setting("admin_telegram_id")
        if not admin_id_str:
            return
        admin_id = int(admin_id_str)
        if message.from_user.id != admin_id:
            return
        try:
            user_id = int(message.text.split("_")[-1])
            await message.answer(f"❌ Заявка пользователя {user_id} отклонена.")
            await message.bot.send_message(
                user_id,
                "❌ Ваша заявка на вывод отклонена. Проверьте корректность реквизитов и попробуйте снова."
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
            final_text,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "show_help")
    @registration_required
    async def help_handler(callback: types.CallbackQuery):
        await callback.answer()

        support_user = get_setting("support_user")
        support_text = get_setting("support_text")

        if support_user is None and support_text is None:
            await callback.message.edit_text(
                "Информация о поддержке не установлена. Установите её в админ-панели.",
                reply_markup=keyboards.create_back_to_menu_keyboard()
            )
        elif support_text is None:
            await callback.message.edit_text(
                "Для связи с поддержкой используйте кнопку ниже.",
                reply_markup=keyboards.create_support_keyboard(support_user)
            )
        else:
            await callback.message.edit_text(
                support_text + "\n\n",
                reply_markup=keyboards.create_support_keyboard(support_user)
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
        active_paid_keys = []
        for k in paid_keys:
            dt = time_utils.parse_iso_to_msk(k.get('expiry_date'))
            if dt and dt > now:
                active_paid_keys.append(k)
        has_global = has_active_global_subscription(active_paid_keys)
        
        try:
            if has_global:
                # Unified View for multiple PAID keys (Global Subscription)
                # Show trial keys separately if they exist
                await callback.message.edit_text(
                    "📂 <b>Управление ключами</b>\n\n"
                    "У вас активна глобальная подписка.\n"
                    "Вы можете управлять ими как единой подпиской. Продление действует сразу на все сервера.",
                    reply_markup=keyboards.create_unified_keys_keyboard(len(paid_keys), len(trial_keys))
                )
            elif len(trial_keys) > 0 and len(paid_keys) == 0:
                # Only trial keys - show them with special button
                await callback.message.edit_text(
                    "📂 <b>Управление ключами</b>\n\n"
                    "У вас есть пробные ключи.",
                    reply_markup=keyboards.create_trial_only_keyboard(len(trial_keys))
                )
            else:
                # Standard View - show ALL keys separately (when paid_keys <= 1)
                await callback.message.edit_text(
                    "Ваши ключи:" if all_keys else "У вас пока нет ключей.",
                    reply_markup=keyboards.create_keys_management_keyboard(all_keys)
                )
        except Exception as e:
            if "message is not modified" in str(e):
                pass
            else:
                logger.error(f"Error in manage_keys_handler: {e}")

    @user_router.callback_query(F.data == "show_keys_detailed")
    @registration_required
    async def show_keys_detailed_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        # Show only PAID keys in global subscription detailed list
        user_keys = get_user_paid_keys(user_id)
        await callback.message.edit_text(
            "📋 <b>Детальный список ключей:</b>",
            reply_markup=keyboards.create_keys_management_keyboard(user_keys)
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
                "У вас нет пробных ключей.",
                reply_markup=keyboards.create_back_to_menu_keyboard()
            )
            return
        
        await callback.message.edit_text(
            "🎁 <b>Пробные ключи:</b>",
            reply_markup=keyboards.create_keys_management_keyboard(trial_keys)
        )

    @user_router.callback_query(F.data == "show_global_info")
    @registration_required
    async def show_global_info_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        # Get ONLY paid keys for global subscription info
        now = time_utils.get_msk_now()
        raw_keys = get_user_paid_keys(user_id)
        user_keys = []
        for k in raw_keys:
             dt = time_utils.parse_iso_to_msk(k.get('expiry_date'))
             if dt and dt > now:
                 user_keys.append(k)

        user_token = get_or_create_subscription_token(user_id)
        
        if not has_active_global_subscription(user_keys):
             await callback.message.edit_text("У вас нет активной глобальной подписки.", reply_markup=keyboards.create_back_to_menu_keyboard())
             return

        # Calculate expiry (minimum of all keys to be safe)
        expiry_dates = []
        for k in user_keys:
             dt = time_utils.parse_iso_to_msk(k.get('expiry_date'))
             if dt:
                 expiry_dates.append(dt)

        min_expiry = min(expiry_dates)
        days_left = (min_expiry - time_utils.get_msk_now()).days

        await callback.message.edit_text(
            f"🌍 <b>Глобальная подписка</b>\n\n"
            f"✅ <b>Статус:</b> Активна\n"
            f"📅 <b>Истекает:</b> {min_expiry.strftime('%d.%m.%Y')}\n"
            f"⏳ <b>Осталось дней:</b> {days_left}\n"
            f"🔗 <b>Доступно серверов:</b> {len(user_keys)}\n\n"
            "Продление действует сразу на все сервера в глобальной подписке.\n"
            "Используйте кнопки ниже для продления или подключения.",
            reply_markup=keyboards.create_global_info_keyboard(user_token) if user_token else keyboards.create_back_to_menu_keyboard()
        )

    @user_router.callback_query(F.data == "get_trial")
    @registration_required
    async def trial_period_handler(callback: types.CallbackQuery, state: FSMContext):
        user_id = callback.from_user.id
        user_db_data = get_user(user_id)
        if user_db_data and user_db_data.get('trial_used'):
            await callback.answer("Вы уже использовали бесплатный пробный период.", show_alert=True)
            return

        hosts = get_all_hosts(only_enabled=True)
        if not hosts:
            await callback.message.edit_text("❌ В данный момент нет доступных серверов для создания пробного ключа.")
            return

        chosen_host = hosts[0]

        await callback.answer()
        await process_trial_key_creation(callback.message, chosen_host['host_name'])

    async def process_trial_key_creation(message: types.Message, host_name: str):
        user_id = message.chat.id
        trial_days_raw = get_setting("trial_duration_days")
        try:
            trial_days = int(float(trial_days_raw)) if trial_days_raw else 1
        except (TypeError, ValueError):
            trial_days = 1

        if trial_days <= 0:
            trial_days = 1

        await message.edit_text(f"Отлично! Создаю для вас бесплатный пробный доступ на {trial_days} дней на сервере \"{host_name}\"...")

        try:
            email = f"user{user_id}-key{get_next_key_number(user_id)}-trial"
            result = await xui_api.create_or_update_key_on_host(
                host_name=host_name,
                email=email,
                days_to_add=int(trial_days),
                telegram_id=str(user_id)
            )
            if not result:
                await message.edit_text("❌ Не удалось создать пробный ключ. Ошибка на сервере.")
                return

            set_trial_used(user_id)
            
            # Trial key: plan_id = 0
            new_key_id = add_new_key(
                user_id=user_id,
                host_name=host_name,
                xui_client_uuid=result['client_uuid'],
                key_email=result['email'],
                expiry_timestamp_ms=result['expiry_timestamp_ms'],
                connection_string=result['connection_string']
            )
            
            await message.delete()
            # Correctly convert timestamp to MSK
            new_expiry_date = time_utils.from_timestamp_ms(result['expiry_timestamp_ms'])
            final_text = get_purchase_success_text("готов", get_next_key_number(user_id) -1, new_expiry_date, result['connection_string'])
            await message.answer(text=final_text, reply_markup=keyboards.create_key_info_keyboard(new_key_id))

            await notify_admin_of_trial(message.bot, user_id, host_name, trial_days)

        except Exception as e:
            logger.error(f"Error creating trial key for user {user_id} on host {host_name}: {e}", exc_info=True)
            await message.edit_text("❌ Произошла ошибка при создании пробного ключа.")

    @user_router.callback_query(F.data.startswith("show_key_"))
    @registration_required
    async def show_key_handler(callback: types.CallbackQuery):
        key_id_to_show = int(callback.data.split("_")[2])
        await callback.message.edit_text("Загружаю информацию о ключе...")
        user_id = callback.from_user.id
        key_data = get_key_by_id(key_id_to_show)

        if not key_data or key_data['user_id'] != user_id:
            await callback.message.edit_text("❌ Ошибка: ключ не найден.")
            return
            
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details['connection_string']:
                await callback.message.edit_text("❌ Ошибка на сервере. Не удалось получить данные ключа.")
                return

            connection_string = details['connection_string']
            expiry_date = time_utils.parse_iso_to_msk(key_data['expiry_date'])
            created_date = time_utils.parse_iso_to_msk(key_data['created_date'])
            
            all_user_keys = get_user_keys(user_id)
            key_number = next((i + 1 for i, key in enumerate(all_user_keys) if key['key_id'] == key_id_to_show), 0)
            
            final_text = get_key_info_text(key_number, expiry_date, created_date, connection_string)
            
            await callback.message.edit_text(
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(key_id_to_show)
            )
        except Exception as e:
            logger.error(f"Error showing key {key_id_to_show}: {e}")
            await callback.message.edit_text("❌ Произошла ошибка при получении данных ключа.")


    @user_router.callback_query(F.data.startswith("show_qr_"))
    @registration_required
    async def show_qr_handler(callback: types.CallbackQuery):
        await callback.answer("Генерирую QR-код...")
        key_id = int(callback.data.split("_")[2])
        key_data = get_key_by_id(key_id)
        if not key_data or key_data['user_id'] != callback.from_user.id: return
        
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details['connection_string']:
                await callback.answer("Ошибка: Не удалось сгенерировать QR-код.", show_alert=True)
                return

            connection_string = details['connection_string']
            qr_img = qrcode.make(connection_string)
            bio = BytesIO(); qr_img.save(bio, "PNG"); bio.seek(0)
            qr_code_file = BufferedInputFile(bio.read(), filename="vpn_qr.png")
            await callback.message.answer_photo(photo=qr_code_file, caption="📱 <b>QR-код для ключа</b>")
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
            disable_web_page_preview=True
        )
    
    @user_router.callback_query(F.data == "howto_vless")
    @registration_required
    async def show_instruction_generic_handler(callback: types.CallbackQuery):
        await callback.answer()

        await callback.message.edit_text(
            "Выберите вашу платформу для инструкции по подключению VLESS:",
            reply_markup=keyboards.create_howto_vless_keyboard(),
            disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "howto_android")
    @registration_required
    async def howto_android_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на Android</b>\n\n"
            "1. <b>Установите приложение V2RayTun:</b> Загрузите и установите приложение V2RayTun из Google Play Store.\n"
            "2. <b>Скопируйте свой ключ (vless://)</b> Перейдите в раздел «Моя подписка» в нашем боте и скопируйте свой ключ.\n"
            "3. <b>Импортируйте конфигурацию:</b>\n"
            "   • Откройте V2RayTun.\n"
            "   • Нажмите на значок + в правом нижнем углу.\n"
            "   • Выберите «Импортировать конфигурацию из буфера обмена» (или аналогичный пункт).\n"
            "4. <b>Выберите сервер:</b> Выберите появившийся сервер в списке.\n"
            "5. <b>Подключитесь к VPN:</b> Нажмите на кнопку подключения (значок «V» или воспроизведения). Возможно, потребуется разрешение на создание VPN-подключения.\n"
            "6. <b>Проверьте подключение:</b> После подключения проверьте свой IP-адрес, например, на https://whatismyipaddress.com/. Он должен отличаться от вашего реального IP.",
        reply_markup=keyboards.create_howto_vless_keyboard(),
        disable_web_page_preview=True
    )

    @user_router.callback_query(F.data == "howto_ios")
    @registration_required
    async def howto_ios_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на iOS (iPhone/iPad)</b>\n\n"
            "1. <b>Установите приложение V2RayTun:</b> Загрузите и установите приложение V2RayTun из App Store.\n"
            "2. <b>Скопируйте свой ключ (vless://):</b> Перейдите в раздел «Моя подписка» в нашем боте и скопируйте свой ключ.\n"
            "3. <b>Импортируйте конфигурацию:</b>\n"
            "   • Откройте V2RayTun.\n"
            "   • Нажмите на значок +.\n"
            "   • Выберите «Импортировать конфигурацию из буфера обмена» (или аналогичный пункт).\n"
            "4. <b>Выберите сервер:</b> Выберите появившийся сервер в списке.\n"
            "5. <b>Подключитесь к VPN:</b> Включите главный переключатель в V2RayTun. Возможно, потребуется разрешить создание VPN-подключения.\n"
            "6. <b>Проверьте подключение:</b> После подключения проверьте свой IP-адрес, например, на https://whatismyipaddress.com/. Он должен отличаться от вашего реального IP.",
        reply_markup=keyboards.create_howto_vless_keyboard(),
        disable_web_page_preview=True
    )

    @user_router.callback_query(F.data == "howto_macos")
    @registration_required
    async def howto_macos_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на macOS</b>\n\n"
            "1. <b>Установите приложение V2Box:</b> Загрузите приложение <a href='https://apps.apple.com/us/app/v2box-v2ray-client/id6446814690'>V2Box - V2Ray Client</a> из Mac App Store.\n"
            "2. <b>Скопируйте свой ключ (vless://):</b> Перейдите в раздел «Моя подписка» в нашем боте и скопируйте свой ключ.\n"
            "3. <b>Импортируйте конфигурацию:</b>\n"
            "   • Откройте V2Box.\n"
            "   • Программа часто сама предлагает добавить ключ из буфера обмена. Если нет — найдите «Import».\n"
            "4. <b>Выберите сервер:</b> Выберите добавленный сервер в списке.\n"
            "5. <b>Подключитесь к VPN:</b> Нажмите переключатель для соединения. Возможно, потребуется разрешить создание VPN-конфигурации (ввести пароль от Mac).\n"
            "6. <b>Проверьте подключение:</b> Проверьте свой IP-адрес на сайте https://whatismyipaddress.com/.",
        reply_markup=keyboards.create_howto_vless_keyboard(),
        disable_web_page_preview=True
    )

    @user_router.callback_query(F.data == "howto_windows")
    @registration_required
    async def howto_windows_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на Windows</b>\n\n"
            "1. <b>Установите приложение Hiddify:</b> Скачайте и установите приложение по прямой ссылке: <a href='https://github.com/hiddify/hiddify-app/releases/latest/download/Hiddify-Windows-Setup-x64.Msix'>Скачать Hiddify для Windows</a>.\n"
            "2. <b>Запустите Hiddify:</b> Откройте установленное приложение.\n"
            "3. <b>Настройте язык и регион:</b> При первом запуске выберите Русский язык и регион Россия.\n"
            "4. <b>Скопируйте свой ключ (vless://):</b> Перейдите в раздел «🔑 Мои ключи» в этом боте, выберите ключ и скопируйте его (начинается с <code>vless://</code>).\n"
            "5. <b>Добавьте ключ в приложение:</b>\n"
            "   • В Hiddify нажмите кнопку <b>«Новый профиль»</b> или «+».\n"
            "   • Выберите <b>«Добавить из буфера обмена»</b>.\n"
            "6. <b>Подключитесь:</b> Нажмите большую кнопку подключения по центру экрана.\n"
            "7. <b>Готово!</b> Теперь ваш интернет защищен. Проверить IP можно на сайте 2ip.ru.",
        reply_markup=keyboards.create_howto_vless_keyboard(),
        disable_web_page_preview=True
    )

    @user_router.callback_query(F.data == "howto_linux")
    @registration_required
    async def howto_linux_handler(callback: types.CallbackQuery):
        await callback.answer()
        await callback.message.edit_text(
            "<b>Подключение на Linux</b>\n\n"
            "1. <b>Скачайте и распакуйте Nekoray:</b> Перейдите на https://github.com/MatsuriDayo/Nekoray/releases и скачайте архив для Linux. Распакуйте его в удобную папку.\n"
            "2. <b>Запустите Nekoray:</b> Откройте терминал, перейдите в папку с Nekoray и выполните <code>./nekoray</code> (или используйте графический запуск, если доступен).\n"
            "3. <b>Скопируйте свой ключ (vless://)</b> Перейдите в раздел «Моя подписка» в нашем боте и скопируйте свой ключ.\n"
            "4. <b>Импортируйте конфигурацию:</b>\n"
            "   • В Nekoray нажмите «Сервер» (Server).\n"
            "   • Выберите «Импортировать из буфера обмена».\n"
            "   • Nekoray автоматически импортирует конфигурацию.\n"
            "5. <b>Обновите серверы (если нужно):</b> Если серверы не появились, нажмите «Серверы» → «Обновить все серверы».\n"
            "6. Сверху включите пункт 'Режим TUN' ('Tun Mode')\n"
            "7. <b>Выберите сервер:</b> В главном окне выберите появившийся сервер.\n"
            "8. <b>Подключитесь к VPN:</b> Нажмите «Подключить» (Connect).\n"
            "9. <b>Проверьте подключение:</b> Откройте браузер и проверьте IP на https://whatismyipaddress.com/. Он должен отличаться от вашего реального IP.",
        reply_markup=keyboards.create_howto_vless_keyboard(),
        disable_web_page_preview=True
    )

    @user_router.callback_query(F.data == "buy_new_key")
    @registration_required
    async def buy_new_key_handler(callback: types.CallbackQuery):
        try:
            await callback.answer()
            hosts = get_all_hosts(only_enabled=True)
            global_plans = get_plans_for_host('ALL')
        
            hosts_for_display = []
            # Check global plans setting
            enable_global = get_setting("enable_global_plans")
            # Default to enabled if not set
            is_global_enabled = True if not enable_global or enable_global == "true" else False

            if global_plans and is_global_enabled:
                hosts_for_display.append({'host_name': 'ALL', 'host_url': 'global'})
                
            for host in hosts:
                 host_plans = get_plans_for_host(host['host_name'])
                 if host_plans:
                     hosts_for_display.append(host) 

            if not hosts_for_display:
                await callback.message.edit_text("❌ В данный момент нет доступных серверов для покупки.")
                return
            
            await callback.message.edit_text(
                "Выберите сервер, на котором хотите приобрести ключ:",
                reply_markup=keyboards.create_host_selection_keyboard(hosts_for_display, action="new")
            )
        except Exception as e:
            logger.error(f"Error in buy_new_key_handler: {e}", exc_info=True)
            await callback.message.edit_text("❌ Произошла ошибка при загрузке списка серверов. Попробуйте позже.")

    @user_router.callback_query(F.data.startswith("select_host_new_"))
    @registration_required
    async def select_host_for_purchase_handler(callback: types.CallbackQuery):
        await callback.answer()
        host_name = callback.data[len("select_host_new_"):]
        plans = get_plans_for_host(host_name)
        if not plans:
            await callback.message.edit_text(f"❌ Для сервера \"{host_name}\" не настроены тарифы.")
            return
        msg_text = f"Выберите тариф для сервера \"{host_name}\":"
        if host_name == 'ALL':
             msg_text = "🌍 Выберите тариф единой подписки (на все серверы):"
             
        await callback.message.edit_text(
            msg_text, 
            reply_markup=keyboards.create_plans_keyboard(plans, action="new", host_name=host_name)
        )

    @user_router.callback_query(F.data.startswith("extend_key_"))
    @registration_required
    async def extend_key_handler(callback: types.CallbackQuery):
        await callback.answer()

        try:
            key_id = int(callback.data.split("_")[2])
        except (IndexError, ValueError):
            await callback.message.edit_text("❌ Произошла ошибка. Неверный формат ключа.")
            return

        key_data = get_key_by_id(key_id)

        if not key_data or key_data['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ Ошибка: Ключ не найден или не принадлежит вам.")
            return
        
        host_name = key_data.get('host_name')
        if not host_name:
            await callback.message.edit_text("❌ Ошибка: У этого ключа не указан сервер. Обратитесь в поддержку.")
            return

        plans = get_plans_for_host(host_name)

        if not plans:
            await callback.message.edit_text(
                f"❌ Извините, для сервера \"{host_name}\" в данный момент не настроены тарифы для продления."
            )
            return

        await callback.message.edit_text(
            f"Выберите тариф для продления ключа на сервере \"{host_name}\":",
            reply_markup=keyboards.create_plans_keyboard(
                plans=plans,
                action="extend",
                host_name=host_name,
                key_id=key_id
            )
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

        await state.update_data(
            action=action, key_id=key_id, plan_id=plan_id, host_name=host_name
        )
        
        
        # Check if user already has a paid key for this host (or global if host is ALL)
        existing_paid_key = None
        user_keys = get_user_keys(callback.from_user.id)
        
        for k in user_keys:
            # Check for specific host match OR if purchasing global (All)
            target_match = (k['host_name'] == host_name) or (host_name == 'ALL')
            # Check if key is paid (plan_id > 0)
            is_paid = k.get('plan_id', 0) > 0
            
            if target_match and is_paid:
                existing_paid_key = k
                break

        email_prompt_enabled = get_setting("email_prompt_enabled")
        # Default to True if not set, or treat 'false' as False. 
        # But settings are strings 'true'/'false'. If not set, it might be None.
        # Let's assume enabled by default for backward compatibility unless explicitly 'false'.
        
        if email_prompt_enabled == 'false':
             # Skip email prompt
            await state.update_data(customer_email=None)
            await callback.message.edit_text(
                CHOOSE_PAYMENT_METHOD_MESSAGE,
                reply_markup=keyboards.create_payment_method_keyboard(
                    payment_methods=get_active_payment_methods(),
                    action=action,
                    key_id=key_id
                )
            )
            await state.set_state(PaymentProcess.waiting_for_payment_method)
            logger.info(f"User {callback.from_user.id}: State set to waiting_for_payment_method (Email prompt disabled)")
            return

        message_text = (
            "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
            "Если вы не хотите указывать почту, нажмите кнопку ниже."
        )

        if existing_paid_key:
             message_text = (
                 f"⚠️ <b>Внимание:</b> У вас уже есть активная подписка на сервере {host_name if host_name != 'ALL' else 'ALL'}.\n"
                 "Эта покупка <b>ПРОДЛИТ</b> срок действия вашего текущего ключа.\n\n"
             ) + message_text

        await callback.message.edit_text(
            message_text,
            reply_markup=keyboards.create_skip_email_keyboard()
        )
        await state.set_state(PaymentProcess.waiting_for_email)

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "back_to_plans")
    async def back_to_plans_handler(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await state.clear()
        
        action = data.get('action')

        if action == 'new':
            await buy_new_key_handler(callback)
        elif action == 'extend':
            await extend_key_handler(callback)
        else:
            await back_to_main_menu_handler(callback)

    @user_router.message(PaymentProcess.waiting_for_email)
    async def process_email_handler(message: types.Message, state: FSMContext):
        if is_valid_email(message.text):
            await state.update_data(customer_email=message.text)
            await message.answer(f"✅ Email принят: {message.text}")

            data = await state.get_data()
            await message.answer(
                CHOOSE_PAYMENT_METHOD_MESSAGE,
                reply_markup=keyboards.create_payment_method_keyboard(
                    payment_methods=PAYMENT_METHODS,
                    action=data.get('action'),
                    key_id=data.get('key_id')
                )
            )
            await state.set_state(PaymentProcess.waiting_for_payment_method)
            logger.info(f"User {message.chat.id}: State set to waiting_for_payment_method")
        else:
            await message.answer("❌ Неверный формат email. Попробуйте еще раз.")

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "skip_email")
    async def skip_email_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.update_data(customer_email=None)

        data = await state.get_data()
        await callback.message.edit_text(
            CHOOSE_PAYMENT_METHOD_MESSAGE,
            reply_markup=keyboards.create_payment_method_keyboard(
                payment_methods=get_active_payment_methods(),
                action=data.get('action'),
                key_id=data.get('key_id')
            )
        )
        await state.set_state(PaymentProcess.waiting_for_payment_method)
        logger.info(f"User {callback.from_user.id}: State set to waiting_for_payment_method")

    async def show_payment_options(message: types.Message, state: FSMContext):
        data = await state.get_data()
        user_data = get_user(message.chat.id)
        plan = get_plan_by_id(data.get('plan_id'))
        
        if not plan:
            await message.edit_text("❌ Ошибка: Тариф не найден.")
            await state.clear()
            return

        price = Decimal(str(plan['price']))
        final_price = price
        discount_applied = False
        message_text = CHOOSE_PAYMENT_METHOD_MESSAGE

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            
            if discount_percentage > 0:
                discount_amount = (price * discount_percentage / 100).quantize(Decimal("0.01"))
                final_price = price - discount_amount

                message_text = (
                    f"🎉 Как приглашенному пользователю, на вашу первую покупку предоставляется скидка {discount_percentage_str}%!\n"
                    f"Старая цена: <s>{price:.2f} RUB</s>\n"
                    f"<b>Новая цена: {final_price:.2f} RUB</b>\n\n"
                ) + CHOOSE_PAYMENT_METHOD_MESSAGE

        await state.update_data(final_price=float(final_price))

        await message.edit_text(
            message_text,
            reply_markup=keyboards.create_payment_method_keyboard(
                payment_methods=get_active_payment_methods(),
                action=data.get('action'),
                key_id=data.get('key_id')
            )
        )
        await state.set_state(PaymentProcess.waiting_for_payment_method)
        
    @user_router.callback_query(F.data == "back_to_email_prompt")
    async def back_to_email_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        # Check if email prompt is enabled. If not, go back to plans/host selection logic 
        # effectively stepping back one more step.
        email_prompt_enabled = get_setting("email_prompt_enabled")
        if email_prompt_enabled == 'false':
             # Iterate back to plans/action selection
             # We need to know the action to go back properly
             data = await state.get_data()
             action = data.get('action')
             host_name = data.get('host_name', 'ALL') 
             key_id = data.get('key_id', 0)
             
             # Re-show plans? Or go back to where we came from?
             # Logic from plan_selection_handler's back button:
             # It goes back to plans.
             # Wait, correct logic is to simulate "back" from payment methods -> which means back to "Plan Selection".
             # So we show plans again.
             
             plans = []
             if host_name == 'ALL':
                 from shop_bot.data_manager.database import get_plans_for_host
                 plans = get_plans_for_host('ALL')
             else:
                 from shop_bot.data_manager.database import get_plans_for_host
                 plans = get_plans_for_host(host_name)
                 
             try:
                 await callback.message.edit_text(
                    f"Выберите тариф для продления ключа на сервере \"{host_name}\":" if action == "extend" else f"Выберите тариф:",
                    reply_markup=keyboards.create_plans_keyboard(
                        plans=plans,
                        action=action,
                        host_name=host_name,
                        key_id=key_id
                    )
                 )
             except Exception as e:
                 logger.warning(f"Error checking back navigation: {e}")
                 # If edit fails (e.g. from invoice), try to delete and send new
                 if "message can't be edited" in str(e) or "message is not modified" not in str(e):
                     try:
                         await callback.message.delete()
                     except:
                         pass
                     await callback.message.answer(
                        f"Выберите тариф для продления ключа на сервере \"{host_name}\":" if action == "extend" else f"Выберите тариф:",
                        reply_markup=keyboards.create_plans_keyboard(
                            plans=plans,
                            action=action,
                            host_name=host_name,
                            key_id=key_id
                        )
                     )
             return

        try:
            await callback.message.edit_text(
                "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
                "Если вы не хотите указывать почту, нажмите кнопку ниже.",
                reply_markup=keyboards.create_skip_email_keyboard()
            )
            await state.set_state(PaymentProcess.waiting_for_email)
        except Exception as e:
             if "message is not modified" in str(e):
                 await callback.answer()
             else:
                 logger.warning(f"Error in back_to_email_prompt (edit failed): {e}")
                 # Try delete and send new
                 try:
                     await callback.message.delete()
                 except:
                     pass
                 await callback.message.answer(
                    "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
                    "Если вы не хотите указывать почту, нажмите кнопку ниже.",
                    reply_markup=keyboards.create_skip_email_keyboard()
                 )
                 await state.set_state(PaymentProcess.waiting_for_email)


    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_yookassa")
    async def create_yookassa_payment_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю ссылку на оплату...")
        
        data = await state.get_data()
        user_data = get_user(callback.from_user.id)
        
        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id)

        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan['price']))
        price_rub = base_price

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub = base_price - discount_amount

        plan_id = data.get('plan_id')
        customer_email = data.get('customer_email')
        host_name = data.get('host_name')
        action = data.get('action')
        key_id = data.get('key_id')
        
        if not customer_email:
            customer_email = get_setting("receipt_email")

        plan = get_plan_by_id(plan_id)
        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        months = plan['months']
        user_id = callback.from_user.id

        try:
            price_str_for_api = f"{price_rub:.2f}"
            price_float_for_metadata = float(price_rub)

            receipt = None
            if customer_email and is_valid_email(customer_email):
                receipt = {
                    "customer": {"email": customer_email},
                    "items": [{
                        "description": f"Подписка на {months} мес.",
                        "quantity": "1.00",
                        "amount": {"value": price_str_for_api, "currency": "RUB"},
                        "vat_code": "1"
                    }]
                }
            payment_payload = {
                "amount": {"value": price_str_for_api, "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": f"https://t.me/{TELEGRAM_BOT_USERNAME}"},
                "capture": True,
                "description": f"Подписка на {months} мес.",
                "metadata": {
                    "user_id": user_id, "months": months, "price": price_float_for_metadata, 
                    "action": action, "key_id": key_id, "host_name": host_name,
                    "plan_id": plan_id, "customer_email": customer_email,
                    "payment_method": "YooKassa"
                }
            }
            if receipt:
                payment_payload['receipt'] = receipt

            payment = Payment.create(payment_payload, uuid.uuid4())
            
            await state.clear()
            
            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_payment_keyboard(payment.confirmation.confirmation_url)
            )
        except Exception as e:
            logger.error(f"Failed to create YooKassa payment: {e}", exc_info=True)
            await callback.message.answer("Не удалось создать ссылку на оплату.")
            await state.clear()

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_stars")
    async def create_stars_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю счет Telegram Stars...")

        data = await state.get_data()
        user_id = callback.from_user.id

        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id)
        if not plan:
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        stars_rate_setting = get_setting("stars_rub_per_star")
        try:
            rub_per_star = Decimal(str(stars_rate_setting)) if stars_rate_setting else Decimal('0')
        except Exception:
            rub_per_star = Decimal('0')

        if rub_per_star <= 0:
            # Auto-calculate if not set: 1 Star ~= 0.013 USD. Use USDT rate + small margin.
            usdt_rub = await get_usdt_rub_rate()
            if usdt_rub:
                 # Multiplier 0.016 (approx ~1.5-1.6 RUB/Star with padding)
                 rub_per_star = usdt_rub * Decimal('0.016')
            else:
                await callback.message.edit_text("❌ Оплата Telegram Stars временно недоступна. (Администратор не указал курс)")
                await state.clear()
                return

        price_rub = Decimal(str(data.get('final_price', plan['price'])))
        stars_amount = int((price_rub / rub_per_star).to_integral_value(rounding=ROUND_CEILING))
        if stars_amount <= 0:
            await callback.message.edit_text("❌ Некорректная сумма для оплаты Telegram Stars.")
            await state.clear()
            return

        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "months": plan['months'],
            "price": float(price_rub),
            "action": data.get('action'),
            "key_id": data.get('key_id'),
            "host_name": data.get('host_name'),
            "plan_id": data.get('plan_id'),
            "customer_email": data.get('customer_email'),
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
                    .button(text="⬅️ Назад", callback_data="back_to_email_prompt")
                    .adjust(1)
                    .as_markup()
            )

            metadata['chat_id'] = user_id
            metadata['message_id'] = invoice_message.message_id
            create_pending_transaction(payment_id, user_id, float(price_rub), metadata)

            await state.clear()
        except Exception as e:
            logger.error(f"Failed to create Stars invoice for user {user_id}: {e}", exc_info=True)
            await callback.message.edit_text("❌ Не удалось создать счет Telegram Stars. Попробуйте позже.")
            await state.clear()

    @user_router.pre_checkout_query()
    async def stars_pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery, bot: Bot):
        payment_id = pre_checkout_query.invoice_payload
        if not payment_id or not _stars_is_pending_transaction(payment_id):
            await bot.answer_pre_checkout_query(
                pre_checkout_query_id=pre_checkout_query.id,
                ok=False,
                error_message="Счет недействителен или уже оплачен. Создайте новый счет."
            )
            return

        await bot.answer_pre_checkout_query(pre_checkout_query_id=pre_checkout_query.id, ok=True)

    @user_router.message(F.successful_payment)
    async def stars_successful_payment_handler(message: types.Message, bot: Bot):
        sp = message.successful_payment
        payment_id = sp.invoice_payload
        paid_stars = int(sp.total_amount)
        telegram_payment_charge_id = sp.telegram_payment_charge_id

        metadata = _stars_complete_transaction(payment_id, paid_stars, telegram_payment_charge_id)
        if not metadata:
            logger.info(f"Stars: Ignoring duplicate or unknown payment for payload={payment_id}")
            return

        await process_successful_payment(bot, metadata)

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_cryptobot")
    async def create_cryptobot_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю счет в Crypto Pay...")
        
        data = await state.get_data()
        user_data = get_user(callback.from_user.id)
        
        plan_id = data.get('plan_id')
        user_id = data.get('user_id', callback.from_user.id)
        customer_email = data.get('customer_email')
        host_name = data.get('host_name')
        action = data.get('action')
        key_id = data.get('key_id')

        cryptobot_token = get_setting('cryptobot_token')
        if not cryptobot_token:
            logger.error(f"Attempt to create Crypto Pay invoice failed for user {user_id}: cryptobot_token is not set.")
            await callback.message.edit_text("❌ Оплата криптовалютой временно недоступна. (Администратор не указал токен).")
            await state.clear()
            return

        plan = get_plan_by_id(plan_id)
        if not plan:
            logger.error(f"Attempt to create Crypto Pay invoice failed for user {user_id}: Plan with id {plan_id} not found.")
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return
        

        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan['price']))
        price_rub = base_price

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub = base_price - discount_amount
        months = plan['months']
        
        try:
            exchange_rate = await get_usdt_rub_rate()

            if not exchange_rate:
                logger.warning("Failed to get live exchange rate. Falling back to the manual setting or default.")
                
                # Fallback to setting if available, otherwise default to a safe high rate (e.g. 110)
                # Ideally, you should have a setting for this. For now, we'll try to get it or hardcode.
                manual_rate = get_setting("usdt_rub_rate")
                if manual_rate:
                    try:
                        exchange_rate = Decimal(manual_rate)
                    except:
                        pass
                
                if not exchange_rate:
                    # Final fallback
                    exchange_rate = Decimal("100.00") 
                    logger.warning("Using hardcoded fallback rate: 100.00 RUB/USDT")
            
            if not exchange_rate:
                 await callback.message.edit_text("❌ Не удалось получить курс валют. Попробуйте позже.")
                 await state.clear()
                 return

            margin = Decimal("1.03")
            price_usdt = (price_rub / exchange_rate * margin).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            
            logger.info(f"Creating Crypto Pay invoice for user {user_id}. Plan price: {price_rub} RUB. Converted to: {price_usdt} USDT.")

            crypto = CryptoPay(cryptobot_token)
            
            payload_data = f"{user_id}:{months}:{float(price_rub)}:{action}:{key_id}:{host_name}:{plan_id}:{customer_email}:CryptoBot"

            invoice = await crypto.create_invoice(
                currency_type="fiat",
                fiat="RUB",
                amount=float(price_rub),
                description=f"Подписка на {months} мес.",
                payload=payload_data,
                expires_in=3600
            )
            
            if not invoice or not invoice.pay_url:
                raise Exception("Failed to create invoice or pay_url is missing.")

            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_payment_keyboard(invoice.pay_url)
            )
            await state.clear()

        except Exception as e:
            logger.error(f"Failed to create Crypto Pay invoice for user {user_id}: {e}", exc_info=True)
            await callback.message.edit_text(f"❌ Не удалось создать счет для оплаты криптовалютой.\n\n<pre>Ошибка: {e}</pre>")
            await state.clear()
        
    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_heleket")
    async def create_heleket_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("Создаю счет Heleket...")
        
        data = await state.get_data()
        plan = get_plan_by_id(data.get('plan_id'))
        user_data = get_user(callback.from_user.id)
        
        if not plan:
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id)

        if not plan:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        base_price = Decimal(str(plan['price']))
        price_rub_decimal = base_price

        if user_data.get('referred_by') and user_data.get('total_spent', 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub_decimal = base_price - discount_amount
        months = plan['months']
        
        final_price_float = float(price_rub_decimal)

        pay_url = await _create_heleket_payment_request(
            user_id=callback.from_user.id,
            price=final_price_float,
            months=plan['months'],
            host_name=data.get('host_name'),
            state_data=data
        )
        
        if pay_url:
            await callback.message.edit_text(
                "Нажмите на кнопку ниже для оплаты:",
                reply_markup=keyboards.create_payment_keyboard(pay_url)
            )
            await state.clear()
        else:
            await callback.message.edit_text("❌ Не удалось создать счет Heleket. Попробуйте другой способ оплаты.")

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_p2p")
    async def start_p2p_payment_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        
        # Check for existing submitted requests
        user_id = callback.from_user.id
        for rid, req in p2p_pending_requests.items():
            if req.get('user_id') == user_id and req.get('submitted'):
                await callback.message.edit_text(
                    "⚠️ <b>У вас уже есть активная заявка на проверку.</b>\n\n"
                    "Пожалуйста, дождитесь ответа администратора по предыдущему платежу, прежде чем создавать новый.",
                    reply_markup=keyboards.create_back_to_menu_keyboard()
                )
                return

        card = get_setting("p2p_card_number") or "Не указаны реквизиты. Обратитесь в поддержку."
        data = await state.get_data()
        from_user = callback.from_user
        
        request_id = str(uuid.uuid4())

        plan_id = data.get('plan_id')
        plan = get_plan_by_id(plan_id) if plan_id else None

        # Базовая цена тарифа
        base_price = Decimal(str(plan['price'])) if plan else Decimal("0")
        # Итоговая цена с учетом скидок, как в других способах оплаты
        final_price = Decimal(str(data.get('final_price', base_price)))
        price_rub = float(final_price)

        pending = {
            "user_id": from_user.id,
            "months": plan['months'] if plan else 1,
            "price": price_rub,
            "action": data.get('action') or 'new',
            "key_id": data.get('key_id') or 0,
            "host_name": data.get('host_name') or '',
            "plan_id": plan_id or 0,
            "customer_email": data.get("customer_email"),
            "payment_method": "P2P",
            "submitted": False  # New flag
        }
        p2p_pending_requests[request_id] = pending

        from aiogram.utils.keyboard import InlineKeyboardBuilder
        await callback.message.edit_text(
            (
                "<b>Оплата по карте (P2P)</b>\n\n"
                f"Сумма к оплате: <b>{final_price:.2f} RUB</b>\n"
                f"Реквизиты для перевода: <code>{card}</code>\n\n"
                "После оплаты обязательно нажмите кнопку \"✅ Я оплатил\"."
            ),
            reply_markup=InlineKeyboardBuilder()
                .button(text="✅ Я оплатил", callback_data=f"p2p_paid_{request_id}")
                .button(text="⬅️ Назад", callback_data="back_to_email_prompt")
                .adjust(1)
                .as_markup()
        )
        await state.update_data(payment_method="P2P", request_id=request_id)

    @user_router.callback_query(F.data.startswith("p2p_paid_"))
    async def notify_admin_paid(callback: types.CallbackQuery, state: FSMContext):
        request_id = callback.data.replace("p2p_paid_", "")
        admin_id = int(get_setting("admin_telegram_id"))
        user = get_user(callback.from_user.id)

        if request_id not in p2p_pending_requests:
            await callback.answer("Заявка устарела или не найдена.", show_alert=True)
            await show_main_menu(callback.message, edit_message=True)
            return

        pending = p2p_pending_requests[request_id]
        
        # Double check if user is trying to trick by using an old button while having another active request
        user_id = callback.from_user.id
        for rid, req in p2p_pending_requests.items():
            if req.get('user_id') == user_id and req.get('submitted') and rid != request_id:
                await callback.answer("У вас уже есть другая активная заявка.", show_alert=True)
                return

        await callback.answer("Ваша заявка отправлена на проверку админу.")
        
        # Mark as submitted so new requests are blocked
        p2p_pending_requests[request_id]['submitted'] = True

        plan_id = pending.get('plan_id')
        plan = get_plan_by_id(plan_id) if plan_id else None
        plan_name = plan['plan_name'] if plan else '-'
        months = pending.get('months', 1)
        price = float(pending.get('price', 0))

        await callback.message.edit_text(
             "✅ <b>Заявка отправлена!</b>\n\n"
             "Администратор проверит поступление средств и подтвердит выдачу ключа.\n"
             "Обычно это занимает не более 15 минут.",
             reply_markup=keyboards.create_back_to_menu_keyboard()
        )

        from aiogram.utils.keyboard import InlineKeyboardBuilder
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Подтвердить оплату", callback_data=f"p2p_approve_{request_id}")
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
            reply_markup=builder.as_markup()
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

        pending = p2p_pending_requests.pop(request_id, None)
        if not pending:
            await message.answer("Заявка не найдена или уже подтверждена/отклонена.")
            return

        await message.answer("Платеж подтвержден. Выполняю выдачу ключа.")
        await process_successful_payment(bot, pending)
        await bot.send_message(pending['user_id'], "✅ Оплата по карте подтверждена! Ключ выдан автоматически.")

    @user_router.message(Command(commands=["decline_p2p"]))
    async def admin_decline_p2p_handler(message: types.Message):
        admin_id = int(get_setting("admin_telegram_id"))
        if message.from_user.id != admin_id:
            return
        parts = message.text.split("_")
        if len(parts) < 3:
            return
        request_id = "_".join(parts[2:])

        pending = p2p_pending_requests.pop(request_id, None)
        if not pending:
            await message.answer("Заявка не найдена или уже подтверждена/отклонена.")
            return

        await message.bot.send_message(pending['user_id'], "❌ Оплата не подтверждена. Свяжитесь с поддержкой для уточнения причин.")
        await message.answer("Пользователь получил отказ в ручном подтверждении.")

    @user_router.callback_query(F.data.startswith("p2p_approve_"))
    async def admin_approve_p2p_callback(callback: types.CallbackQuery, bot: Bot):
        admin_id = int(get_setting("admin_telegram_id"))
        if callback.from_user.id != admin_id:
            await callback.answer("У вас нет прав для этого действия.", show_alert=True)
            return

        request_id = callback.data.replace("p2p_approve_", "")

        pending = p2p_pending_requests.pop(request_id, None)
        if not pending:
            await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
            return

        await callback.answer("Платеж подтвержден.")
        await callback.message.edit_text("✅ Платеж подтвержден. Ключ будет выдан автоматически.")
        await process_successful_payment(bot, pending)
        await bot.send_message(pending['user_id'], "✅ Оплата по карте подтверждена! Ключ выдан автоматически.")

    @user_router.callback_query(F.data.startswith("p2p_decline_"))
    async def admin_decline_p2p_callback(callback: types.CallbackQuery):
        admin_id = int(get_setting("admin_telegram_id"))
        if callback.from_user.id != admin_id:
            await callback.answer("У вас нет прав для этого действия.", show_alert=True)
            return

        request_id = callback.data.replace("p2p_decline_", "")

        pending = p2p_pending_requests.pop(request_id, None)
        if not pending:
            await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
            return

        await callback.answer("Заявка отклонена.")
        await callback.message.edit_text("❌ Заявка отклонена. Пользователь уведомлен.")
        await callback.bot.send_message(pending['user_id'], "❌ Оплата не подтверждена. Свяжитесь с поддержкой для уточнения причин.")

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_tonconnect")
    async def create_ton_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        logger.info(f"User {callback.from_user.id}: Entered create_ton_invoice_handler.")
        data = await state.get_data()
        user_id = callback.from_user.id
        wallet_address = get_setting("ton_wallet_address")
        plan = get_plan_by_id(data.get('plan_id'))
        
        if not wallet_address or not plan:
            await callback.message.edit_text("❌ Оплата через TON временно недоступна.")
            await state.clear()
            return

        await callback.answer("Создаю ссылку и QR-код для TON Connect...")
            
        price_rub = Decimal(str(data.get('final_price', plan['price'])))

        usdt_rub_rate = await get_usdt_rub_rate()
        ton_usdt_rate = await get_ton_usdt_rate()

        if not usdt_rub_rate or not ton_usdt_rate:
            await callback.message.edit_text("❌ Не удалось получить курс TON. Попробуйте позже.")
            await state.clear()
            return

        price_ton = (price_rub / usdt_rub_rate / ton_usdt_rate).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        amount_nanoton = int(price_ton * 1_000_000_000)
        
        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id, "months": plan['months'], "price": float(price_rub),
            "action": data.get('action'), "key_id": data.get('key_id'),
            "host_name": data.get('host_name'), "plan_id": data.get('plan_id'),
            "customer_email": data.get('customer_email'), "payment_method": "TON Connect"
        }
        create_pending_transaction(payment_id, user_id, float(price_rub), metadata)

        transaction_payload = {
            'messages': [{'address': wallet_address, 'amount': str(amount_nanoton), 'payload': payment_id}],
            'valid_until': int(datetime.now().timestamp()) + 600
        }

        try:
            connect_url = await _start_ton_connect_process(user_id, transaction_payload)
            
            qr_img = qrcode.make(connect_url)
            bio = BytesIO()
            qr_img.save(bio, "PNG")
            qr_file = BufferedInputFile(bio.getvalue(), "ton_qr.png")

            await callback.message.delete()
            await callback.message.answer_photo(
                photo=qr_file,
                caption=(
                    f"💎 **Оплата через TON Connect**\n\n"
                    f"Сумма к оплате: `{price_ton}` **TON**\n\n"
                    f"✅ **Способ 1 (на телефоне):** Нажмите кнопку **'Открыть кошелек'** ниже.\n"
                    f"✅ **Способ 2 (на компьютере):** Отсканируйте QR-код кошельком.\n\n"
                    f"После подключения кошелька подтвердите транзакцию."
                ),
                parse_mode="Markdown",
                reply_markup=keyboards.create_ton_connect_keyboard(connect_url)
            )
            await state.clear()

        except Exception as e:
            logger.error(f"Failed to generate TON Connect link for user {user_id}: {e}", exc_info=True)
            await callback.message.answer("❌ Не удалось создать ссылку для TON Connect. Попробуйте позже.")
            await state.clear()

    @user_router.message(F.text)
    @registration_required
    async def unknown_message_handler(message: types.Message):
        if message.text.startswith('/'):
            await message.answer("Такой команды не существует. Попробуйте /start.")
        else:
            await message.answer("Я не понимаю эту команду. Пожалуйста, используйте кнопки меню.")

    return user_router

_user_connectors: Dict[int, TonConnect] = {}
_listener_tasks: Dict[int, asyncio.Task] = {}

async def _get_ton_connect_instance(user_id: int) -> TonConnect:
    if user_id not in _user_connectors:
        manifest_url = 'https://raw.githubusercontent.com/ton-blockchain/ton-connect/main/requests-responses.json'
        _user_connectors[user_id] = TonConnect(manifest_url=manifest_url)
    return _user_connectors[user_id]

async def _listener_task(connector: TonConnect, user_id: int, transaction_payload: dict):
    try:
        wallet_connected = False
        for _ in range(120):
            if connector.connected:
                wallet_connected = True
                break
            await asyncio.sleep(1)

        if not wallet_connected:
            logger.warning(f"TON Connect: Timeout waiting for wallet connection from user {user_id}.")
            return

        logger.info(f"TON Connect: Wallet connected for user {user_id}. Address: {connector.account.address}")
        
        logger.info(f"TON Connect: Sending transaction request to user {user_id} with payload: {transaction_payload}")
        await connector.send_transaction(transaction_payload)
        
        logger.info(f"TON Connect: Transaction request sent successfully for user {user_id}.")

    except UserRejectsError:
        logger.warning(f"TON Connect: User {user_id} rejected the transaction.")
    except Exception as e:
        logger.error(f"TON Connect: An error occurred in the listener task for user {user_id}: {e}", exc_info=True)
    finally:
        if user_id in _user_connectors:
            del _user_connectors[user_id]
        if user_id in _listener_tasks:
            del _listener_tasks[user_id]

async def _start_ton_connect_process(user_id: int, transaction_payload: dict) -> str:
    if user_id in _listener_tasks and not _listener_tasks[user_id].done():
        _listener_tasks[user_id].cancel()

    connector = await _get_ton_connect_instance(user_id)
    
    task = asyncio.create_task(
        _listener_task(connector, user_id, transaction_payload)
    )
    _listener_tasks[user_id] = task

    wallets = connector.get_wallets()
    return await connector.connect(wallets[0])

async def process_successful_onboarding(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("✅ Спасибо! Доступ предоставлен.")
    set_terms_agreed(callback.from_user.id)
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Приятного использования!", reply_markup=keyboards.main_reply_keyboard)
    await show_main_menu(callback.message)

async def is_url_reachable(url: str) -> bool:
    pattern = re.compile(
        r'^(https?://)'
        r'(([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})'
        r'(/.*)?$'
    )
    if not re.match(pattern, url):
        return False

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.head(url, allow_redirects=True) as response:
                return response.status < 400
    except Exception as e:
        logger.warning(f"URL validation failed for {url}. Error: {e}")
        return False

async def notify_admin_of_purchase(bot: Bot, metadata: dict):
    if get_setting("enable_admin_payment_notifications") == 'false':
        return

    admin_id_str = get_setting("admin_telegram_id")
    if not admin_id_str:
        logger.warning("Admin notification skipped: admin_telegram_id is not set in settings.")
        return
    
    admin_id = int(admin_id_str)

    try:
        user_id = metadata.get('user_id')
        months = metadata.get('months')
        price = float(metadata.get('price'))
        host_name = metadata.get('host_name')
        plan_id = metadata.get('plan_id')
        payment_method = metadata.get('payment_method', 'Unknown')
        
        user_info = get_user(user_id)
        plan_info = get_plan_by_id(plan_id)

        username = user_info.get('username', 'N/A') if user_info else 'N/A'
        plan_name = plan_info.get('plan_name', f'{months} мес.') if plan_info else f'{months} мес.'
        
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

        await bot.send_message(
            chat_id=admin_id,
            text=message_text,
            parse_mode='HTML'
        )
        logger.info(f"Admin notification sent for a new purchase by user {user_id}.")

    except Exception as e:
        logger.error(f"Failed to send admin notification for purchase: {e}", exc_info=True)

async def notify_admin_of_trial(bot: Bot, user_id: int, host_name: str, duration_days: int):
    if get_setting("enable_admin_trial_notifications") == 'false':
        return

    admin_id_str = get_setting("admin_telegram_id")
    if not admin_id_str:
        return

    try:
        admin_id = int(admin_id_str)
        user_info = get_user(user_id)
        username = user_info.get('username', 'N/A') if user_info else 'N/A'
        
        safe_username = html.quote(username)
        safe_host_name = html.quote(host_name)

        message_text = (
            "🎁 <b>Взят пробный ключ!</b>\n\n"
            f"👤 <b>Пользователь:</b> @{safe_username} (ID: <code>{user_id}</code>)\n"
            f"🌍 <b>Сервер:</b> {safe_host_name}\n"
            f"⏳ <b>Срок:</b> {duration_days} дн."
        )

        await bot.send_message(
            chat_id=admin_id,
            text=message_text,
            parse_mode='HTML'
        )
        logger.info(f"Admin notification sent for TRIAL by user {user_id}.")
    except Exception as e:
        logger.error(f"Failed to send admin notification for trial: {e}", exc_info=True)

async def _create_heleket_payment_request(user_id: int, price: float, months: int, host_name: str, state_data: dict) -> str | None:
    merchant_id = get_setting("heleket_merchant_id")
    api_key = get_setting("heleket_api_key")
    bot_username = get_setting("telegram_bot_username")
    domain = get_setting("domain")

    if not all([merchant_id, api_key, bot_username, domain]):
        logger.error("Heleket Error: Not all required settings are configured.")
        return None

    redirect_url = f"https://t.me/{bot_username}"
    order_id = str(uuid.uuid4())
    
    metadata = {
        "user_id": user_id, "months": months, "price": float(price),
        "action": state_data.get('action'), "key_id": state_data.get('key_id'),
        "host_name": host_name, "plan_id": state_data.get('plan_id'),
        "customer_email": state_data.get('customer_email'), "payment_method": "Heleket"
    }

    payload = {
        "amount": f"{price:.2f}",
        "currency": "RUB",
        "order_id": order_id,
        "description": json.dumps(metadata),
        "url_return": redirect_url,
        "url_success": redirect_url,
        "url_callback": f"https://{domain}/heleket-webhook",
        "lifetime": 1800,
        "is_payment_multiple": False
    }
    
    headers = {
        "merchant": merchant_id,
        "sign": _generate_heleket_signature(json.dumps(payload), api_key),
        "Content-Type": "application/json",
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.heleket.com/v1/payment"
            async with session.post(url, json=payload, headers=headers) as response:
                result = await response.json()
                if response.status == 200 and result.get("result", {}).get("url"):
                    return result["result"]["url"]
                else:
                    logger.error(f"Heleket API Error: Status {response.status}, Result: {result}")
                    return None
    except Exception as e:
        logger.error(f"Heleket request failed: {e}", exc_info=True)
        return None

def _generate_heleket_signature(data, api_key: str) -> str:
    if isinstance(data, dict):
        data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    else:
        data_str = str(data)
    base64_encoded = base64.b64encode(data_str.encode()).decode()
    raw_string = f"{base64_encoded}{api_key}"
    return hashlib.md5(raw_string.encode()).hexdigest()

async def get_usdt_rub_rate() -> Decimal | None:
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": "USDTRUB"}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                price_str = data.get('price')
                if price_str:
                    logger.info(f"Got USDT RUB: {price_str}")
                    return Decimal(price_str)
                logger.error("Can't find 'price' in Binance response.")
                return None
    except Exception as e:
        logger.error(f"Error getting USDT RUB Binance rate: {e}", exc_info=True)
        return None
    
async def get_ton_usdt_rate() -> Decimal | None:
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": "TONUSDT"}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                price_str = data.get('price')
                if price_str:
                    logger.info(f"Got TON USDT: {price_str}")
                    return Decimal(price_str)
                logger.error("Can't find 'price' in Binance response.")
                return None
    except Exception as e:
        logger.error(f"Error getting TON USDT Binance rate: {e}", exc_info=True)
        return None

async def process_successful_payment(bot: Bot, metadata: dict):
    try:
        logger.info(f"Processing successful payment for user {metadata.get('user_id')}: {metadata}")

        user_id = int(metadata['user_id'])
        
        # ========== RACE CONDITION PROTECTION ==========
        # Check if payment is already being processed
        if get_pending_payment_status(user_id):
            logger.warning(f"Payment already being processed for user {user_id}. Ignoring duplicate webhook.")
            return
        
        # Mark payment as pending to prevent duplicate processing
        if not set_pending_payment(user_id, True):
            logger.error(f"Failed to set pending payment flag for user {user_id}")
            return
        # ===============================================
        
        months = int(metadata['months'])
        price = float(metadata['price'])
        action = metadata['action']
        key_id = int(metadata['key_id'])
        host_name = metadata['host_name']
        plan_id = int(metadata['plan_id'])
        customer_email = metadata.get('customer_email')
        payment_method = metadata.get('payment_method')

        chat_id_to_delete = metadata.get('chat_id')
        message_id_to_delete = metadata.get('message_id')
        
        # Additional safety check for keys
        if action in ['extend', 'new'] and not host_name:
             logger.error(f"Missing host_name in metadata for action {action}: {metadata}")
             await bot.send_message(user_id, "❌ Произошла ошибка при обработке платежа: не указан сервер. Обратитесь в поддержку.")
             set_pending_payment(user_id, False)
             return

        plan = get_plan_by_id(plan_id)
        if not plan:
            logger.error(f"Plan {plan_id} not found during payment processing")
            await bot.send_message(user_id, "❌ Ошибка: Тариф не найден. Обратитесь в поддержку.")
            set_pending_payment(user_id, False)
            return

        months = plan['months'] # Re-assign months from plan, as it might be different from metadata['months'] for some payment methods
        
    except (ValueError, TypeError) as e:
        logger.error(f"FATAL: Could not parse metadata. Error: {e}. Metadata: {metadata}")
        if 'user_id' in metadata:
            set_pending_payment(int(metadata['user_id']), False)
        return
    except Exception as e:
        logger.error(f"An unexpected error occurred during initial payment processing for user {metadata.get('user_id')}: {e}", exc_info=True)
        if metadata.get('user_id'):
            set_pending_payment(int(metadata.get('user_id')), False)
        await bot.send_message(metadata.get('user_id'), "❌ Произошла непредвиденная ошибка при обработке платежа. Пожалуйста, обратитесь в поддержку.")
        return

    if chat_id_to_delete and message_id_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id_to_delete, message_id=message_id_to_delete)
        except TelegramBadRequest as e:
            logger.warning(f"Could not delete payment message: {e}")

    processing_message = await bot.send_message(
        chat_id=user_id,
        text=f"✅ Оплата получена! Обрабатываю ваш запрос на сервере \"{host_name}\"..."
    )
    try:
        email = ""
        if not action or str(action) == 'None':
            action = 'new'

        key_number = None
        if action == "new" or host_name == 'ALL':
             key_number = get_next_key_number(user_id)
        
        hosts_to_process = []
        if host_name == 'ALL':
             hosts_data = get_all_hosts(only_enabled=True)
             for h in hosts_data:
                 h_email = f"user{user_id}-key{key_number}-{h['host_name'].replace(' ', '').lower()}"
                 hosts_to_process.append((h['host_name'], h_email))
        else:
             if action == "new":
                 email = f"user{user_id}-key{key_number}-{host_name.replace(' ', '').lower()}"
             elif action == "extend":
                 key_data = get_key_by_id(key_id)
                 if not key_data or key_data['user_id'] != user_id:
                     await processing_message.edit_text("❌ Ошибка: ключ для продления не найден.")
                     return
                 email = key_data['key_email']
             hosts_to_process.append((host_name, email))

        days_to_add = months * 30
        results = []
        primary_key_id: int | None = None
        
        for h_name, h_email in hosts_to_process:
            try:
                # Key Reuse Logic for Global Plans / New Keys
                # Check if we already have a key for this user on this host
                existing_key_db = None
                if host_name == 'ALL' or action == "new":
                     # We need to find if user has a PAID key on this host
                     # CRITICAL: Ignore trial keys (plan_id=0) - they should never be extended
                     user_keys = get_user_keys(user_id)
                     for k in user_keys:
                         if k['host_name'] == h_name and k.get('plan_id', 0) > 0:
                             # Only reuse PAID keys
                             existing_key_db = k
                             # Use the existing email to extend instead of creating new
                             h_email = k['key_email']
                             break
                
                res = await xui_api.create_or_update_key_on_host(
                    host_name=h_name,
                    email=h_email,
                    days_to_add=days_to_add,
                    telegram_id=str(user_id)
                )
                if res:
                    results.append(res)
                    if existing_key_db:
                        # Update existing key info in DB

                        expiry_datetime = datetime.fromtimestamp(res['expiry_timestamp_ms'] / 1000)
                        update_key_info(existing_key_db['key_id'], expiry_datetime, res['connection_string'])
                        # If user purchased a GLOBAL plan, mark reused keys with global plan_id
                        if host_name == 'ALL' and action == 'new':
                            update_key_plan_id(existing_key_db['key_id'], int(plan_id))
                        if host_name != 'ALL' and primary_key_id is None:
                            primary_key_id = int(existing_key_db['key_id'])
                    elif action == "new":
                        # Only add new row if it didn't exist
                        # Paid key: use plan_id from metadata
                        new_key_id = add_new_key(
                            user_id,
                            h_name,
                            res['client_uuid'],
                            res['email'],
                            res['expiry_timestamp_ms'],
                            res['connection_string'],
                            int(plan_id),
                        )
                        if host_name != 'ALL' and primary_key_id is None and new_key_id is not None:
                            primary_key_id = int(new_key_id)
                    elif action == "extend" and host_name != 'ALL':
                        # Key ID driven
                        expiry_datetime = datetime.fromtimestamp(res['expiry_timestamp_ms'] / 1000)
                        update_key_info(key_id, expiry_datetime, res['connection_string'])
                        if primary_key_id is None:
                            primary_key_id = int(key_id)
            except Exception as e:
                logger.error(f"Failed to process key on host {h_name}: {e}")

        if not results:
            await processing_message.edit_text("❌ Не удалось создать/обновить ни одного ключа.")
            return
        
        price = float(metadata.get('price')) 

        user_data = get_user(user_id)
        referrer_id = user_data.get('referred_by')

        if referrer_id:
            percentage = Decimal(get_setting("referral_percentage") or "0")
            
            reward = (Decimal(str(price)) * percentage / 100).quantize(Decimal("0.01"))
            
            if float(reward) > 0:
                add_to_referral_balance(referrer_id, float(reward))
                
                try:
                    referrer_username = user_data.get('username', 'пользователь')
                    await bot.send_message(
                        referrer_id,
                        f"🎉 Ваш реферал @{referrer_username} совершил покупку на сумму {price:.2f} RUB!\n"
                        f"💰 На ваш баланс начислено вознаграждение: {reward:.2f} RUB."
                    )
                except Exception as e:
                    logger.warning(f"Could not send referral reward notification to {referrer_id}: {e}")

        update_user_stats(user_id, price, months)
        
        user_info = get_user(user_id)

        internal_payment_id = str(uuid.uuid4())
        
        log_username = user_info.get('username', 'N/A') if user_info else 'N/A'
        log_status = 'paid'
        log_amount_rub = float(price)
        log_method = metadata.get('payment_method', 'Unknown')
        
        log_metadata = json.dumps({
            "plan_id": metadata.get('plan_id'),
            "plan_name": get_plan_by_id(metadata.get('plan_id')).get('plan_name', 'Unknown') if get_plan_by_id(metadata.get('plan_id')) else 'Unknown',
            "host_name": metadata.get('host_name'),
            "customer_email": metadata.get('customer_email')
        })

        log_transaction(
            username=log_username,
            transaction_id=None,
            payment_id=internal_payment_id,
            user_id=user_id,
            status=log_status,
            amount_rub=log_amount_rub,
            amount_currency=None,
            currency_name=None,
            payment_method=log_method,
            metadata=log_metadata
        )
        
        await processing_message.delete()
        
        # Prepare success message
        # If multiple results (ALL hosts), show generic success or first key.
        # Prefer showing subscription link if ALL.
        
        # Taking the first result for expiry/key_info display purposes
        first_res = results[0]
        connection_string = first_res['connection_string']
        new_expiry_date = datetime.fromtimestamp(first_res['expiry_timestamp_ms'] / 1000)
        
        all_user_keys = get_user_keys(user_id)
        # Determine key number more reliably
        displayed_key_number = None
        if action == "new":
            displayed_key_number = key_number
        else:
            try:
                effective_key_id = primary_key_id if primary_key_id is not None else key_id
                for idx, k in enumerate(all_user_keys):
                    if int(k.get('key_id', 0)) == int(effective_key_id):
                        displayed_key_number = idx + 1
                        break
            except Exception:
                displayed_key_number = None
        if displayed_key_number is None:
            displayed_key_number = len(all_user_keys)

        final_text = get_purchase_success_text(
            action=action,
            key_number=int(displayed_key_number),
            expiry_date=new_expiry_date,
            connection_string=connection_string
        )
        
        if host_name == 'ALL':
             domain = get_setting("domain")
             user_token = get_or_create_subscription_token(user_id)
             plan = get_plan_by_id(metadata.get('plan_id')) if metadata else None
             plan_name = plan.get('plan_name') if isinstance(plan, dict) else None
             if not plan_name:
                 plan_name = "—"
             
             final_text = (
                 f"🎉 <b>Мульти-подписка активирована!</b>\n"
                 f"Ваш тариф: {plan_name}\n"
                 f"Срок действия до: {new_expiry_date.strftime('%d.%m.%Y')}\n\n"
             )
             
             if not user_token:
                  # If token missing (legacy user?), try to generate one or warn
                  # Since we can't easily generate here without importing database write logic, better notify admin or ask user to re-register/re-login.
                  # Actually we can't re-login easily in bot.
                  final_text += "\n\n⚠️ Ошибка: У вас отсутствует токен подписки. Пожалуйста, обратитесь к администратору."
             elif not domain:
                  final_text += "\n\n⚠️ Не удалось сгенерировать ссылку. Администратор не настроил домен (Admin Panel -> Settings -> Ваш домен)."
             else:
                 if not domain.startswith('http'):
                     sub_link = f"https://{domain}/sub/{user_token}"
                 else:
                     sub_link = f"{domain}/sub/{user_token}"

                 final_text += f"\n\n🌍 <b>Ваша ссылка-подписка (для всех серверов):</b>\n<code>{sub_link}</code>\n\n⚠️ Вставьте эту ссылку в ваше приложение (например, v2rayNG, Streisand, V2Box) как Подписку (Subscription Group)."

             # Report failures if any
             failed_hosts = [h[0] for h in hosts_to_process if h[0] not in [r['host_name'] for r in results]]
             if failed_hosts:
                 final_text += f"\n\n❌ <b>Внимание:</b> Не удалось создать ключи на следующих серверах (свяжитесь с админом):\n- " + "\n- ".join(failed_hosts)

             await bot.send_message(
                chat_id=user_id,
                text=final_text,
                reply_markup=keyboards.create_global_sub_keyboard(user_token) if user_token else keyboards.create_back_to_menu_keyboard()
             )
        else:
            await bot.send_message(
                chat_id=user_id,
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(primary_key_id if primary_key_id is not None else key_id)
            )

        await notify_admin_of_purchase(bot, metadata)
        
        # ========== CLEAR RACE CONDITION PROTECTION ==========
        set_pending_payment(user_id, False)
        # ======================================================
        
    except Exception as e:
        logger.error(f"Error processing payment for user {user_id} on host {host_name}: {e}", exc_info=True)
        # Clear pending payment flag on error so user can retry
        set_pending_payment(user_id, False)
        await processing_message.edit_text("❌ Ошибка при выдаче ключа.")

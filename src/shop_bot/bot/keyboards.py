import logging

from datetime import datetime

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.data_manager.database import get_setting
from shop_bot.utils import time_utils

logger = logging.getLogger(__name__)

main_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏠 Главное меню")]],
    resize_keyboard=True
)

def create_main_menu_keyboard(user_keys: list, trial_available: bool, is_admin: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    builder.button(text="💳 Купить подписку", callback_data="buy_subscription")

    if trial_available and get_setting("trial_enabled") == "true":
        builder.button(text="🎁 Попробовать бесплатно", callback_data="get_trial")

    builder.button(text="👤 Мой профиль", callback_data="show_profile")
    builder.button(text=f"📦 Мои подписки ({len(user_keys)})", callback_data="manage_keys")
    
    # Показываем реферальную программу только если она включена
    referral_enabled = str(get_setting("enable_referrals")).lower() == "true"
    if referral_enabled:
        builder.button(text="🤝 Реферальная программа", callback_data="show_referral_program")
    
    builder.button(text="🆘 Поддержка", callback_data="show_help")
    builder.button(text="ℹ️ О проекте", callback_data="show_about")
    builder.button(text="📖 Инструкция", callback_data="howto_vless")
    if is_admin:
        builder.button(text="📢 Рассылка", callback_data="start_broadcast")

    # Динамический layout в зависимости от включенных кнопок
    layout = [1]  # Buy subscription CTA
    if trial_available and get_setting("trial_enabled") == "true":
        layout.append(1)  # Trial button
    layout.append(2)  # Profile + Keys
    if referral_enabled:
        layout.append(1)  # Referral
    layout.append(2)  # Support + About
    layout.append(1)  # How to use
    if is_admin:
        layout.append(1)  # Broadcast
    builder.adjust(*layout)
    
    return builder.as_markup()

def create_broadcast_options_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить кнопку", callback_data="broadcast_add_button")
    builder.button(text="➡️ Пропустить", callback_data="broadcast_skip_button")
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast")
    builder.adjust(2, 1)
    return builder.as_markup()

def create_broadcast_confirmation_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data="confirm_broadcast")
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast")
    builder.adjust(2)
    return builder.as_markup()

def create_broadcast_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast")
    return builder.as_markup()

def create_about_keyboard(channel_url: str | None, terms_url: str | None, privacy_url: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if channel_url:
        builder.button(text="📰 Наш канал", url=channel_url)
    if terms_url:
        builder.button(text="📄 Условия использования", url=terms_url)
    if privacy_url:
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()
    
def create_support_keyboard(support_user: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🆘 Написать в поддержку", url=support_user)
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_host_selection_keyboard(hosts: list, action: str, back_callback: str = "manage_keys") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for host in hosts:
        callback_data = f"select_host_{action}_{host['host_name']}"
        text = host['host_name']
        if text == 'ALL':
            text = "🌍 Глобальная подписка (Все серверы)"
        builder.button(text=text, callback_data=callback_data)
    builder.button(
        text="← Назад" if action == 'new' else "🏠 В меню",
        callback_data=back_callback if action == 'new' else "back_to_main_menu"
    )
    builder.adjust(1)
    return builder.as_markup()

def create_plans_keyboard(plans: list[dict], action: str, host_name: str, key_id: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        callback_data = f"buy_{host_name}_{plan['plan_id']}_{action}_{key_id}"
        builder.button(text=f"{plan['plan_name']} - {plan['price']:.0f} RUB", callback_data=callback_data)
    back_callback = "manage_keys" if action == "extend" else "back_to_host_selection"
    builder.button(text="← Назад", callback_data=back_callback)
    builder.adjust(1) 
    return builder.as_markup()

def create_skip_email_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➡️ Продолжить без почты", callback_data="skip_email")
    builder.button(text="← Назад", callback_data="back_to_plans")
    builder.adjust(1)
    return builder.as_markup()

def create_payment_method_keyboard(payment_methods: dict, action: str, key_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if payment_methods and payment_methods.get("yookassa"):
        if str(get_setting("sbp_enabled")).lower() == "true":
            builder.button(text="🏦 СБП / Банковская карта", callback_data="pay_yookassa")
        else:
            builder.button(text="🏦 Банковская карта", callback_data="pay_yookassa")
    if payment_methods and payment_methods.get("stars"):
        builder.button(text="⭐ Telegram Stars", callback_data="pay_stars")
    if payment_methods and payment_methods.get("heleket"):
        builder.button(text="💎 Криптовалюта", callback_data="pay_heleket")
    if get_setting("p2p_enabled") == "true":
        builder.button(text="💳 Оплата по карте (P2P)", callback_data="pay_p2p")
    if payment_methods and payment_methods.get("cryptobot"):
        builder.button(text="🤖 CryptoBot", callback_data="pay_cryptobot")
    if payment_methods and payment_methods.get("tonconnect"):
        callback_data_ton = "pay_tonconnect"
        logger.info(f"Creating TON button with callback_data: '{callback_data_ton}'")
        builder.button(text="🪙 TON Connect", callback_data=callback_data_ton)

    builder.button(text="← Назад", callback_data="back_to_email_prompt")
    builder.adjust(1)
    return builder.as_markup()

def create_ton_connect_keyboard(connect_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Открыть кошелек", url=connect_url)
    builder.button(text="← Назад", callback_data="back_to_email_prompt")
    builder.adjust(1)
    return builder.as_markup()

def create_payment_keyboard(payment_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Перейти к оплате", url=payment_url)
    builder.button(text="← Назад", callback_data="back_to_email_prompt")
    builder.adjust(1)
    return builder.as_markup()

def create_keys_management_keyboard(keys: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if keys:
        for i, key in enumerate(keys):
            expiry_date = time_utils.parse_iso_to_msk(key.get('expiry_date'))
            
            if expiry_date:
                status_icon = "✅" if expiry_date > time_utils.get_msk_now() else "❌"
                expiry_str = time_utils.format_msk(expiry_date, '%d.%m.%Y')
            else:
                 status_icon = "❓"
                 expiry_str = "Ошибка даты"

            host_name = key.get('host_name', 'Неизвестный хост')
            button_text = f"{status_icon} Ключ #{i+1} ({host_name}) (до {expiry_str})"
            builder.button(text=f"🔑 {button_text}", callback_data=f"show_key_{key['key_id']}")
            builder.button(text="📱 QR", callback_data=f"show_qr_{key['key_id']}")
    builder.button(text="➕ Купить новый ключ", callback_data="buy_new_key")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")

    if keys:
        # one row per key: [show_key, qr]
        # then two separate rows for [buy] and [back]
        builder.adjust(*([2] * len(keys)), 1, 1)
    else:
        builder.adjust(1, 1)
    return builder.as_markup()

def create_key_info_keyboard(key_id: int, copy_text: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if copy_text:
        builder.row(
            InlineKeyboardButton(
                text="📋 Скопировать ключ",
                copy_text=CopyTextButton(text=copy_text)
            )
        )
    builder.button(text="➕ Продлить этот ключ", callback_data=f"extend_key_{key_id}")
    builder.button(text="📱 Показать QR-код", callback_data=f"show_qr_{key_id}")
    builder.button(text="📖 Инструкция", callback_data=f"howto_vless_{key_id}")
    builder.button(text="← Назад", callback_data="manage_keys")
    builder.adjust(1)
    return builder.as_markup()

def create_global_link_keyboard(subscription_link: str, subscription_token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📋 Скопировать ссылку",
            copy_text=CopyTextButton(text=subscription_link)
        )
    )
    builder.button(text="📱 Показать QR-код", callback_data=f"global_qr_{subscription_token}")
    builder.button(text="📖 Инструкция", callback_data="global_howto")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_global_sub_keyboard(subscription_token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Продлить подписку", callback_data="select_host_new_ALL")
    builder.button(text="🔗 Показать ссылку", callback_data=f"global_link_{subscription_token}")
    builder.button(text="📱 Показать QR-код", callback_data=f"global_qr_{subscription_token}")
    builder.button(text="📖 Инструкция", callback_data="global_howto")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_unified_keys_keyboard(keys_count: int, trial_keys_count: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🌍 Моя подписка (Серверов: {keys_count})", callback_data="show_global_info")
    
    # Trial keys button (if exists)
    if trial_keys_count > 0:
        builder.button(text=f"🎁 Пробный ключ ({trial_keys_count})", callback_data="show_trial_keys")
    
    builder.button(text="➕ Купить новый ключ", callback_data="buy_new_key")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_trial_only_keyboard(trial_keys_count: int) -> InlineKeyboardMarkup:
    """Keyboard for when user has only trial keys"""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🎁 Пробный ключ ({trial_keys_count})", callback_data="show_trial_keys")
    builder.button(text="➕ Купить новый ключ", callback_data="buy_new_key")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_global_info_keyboard(subscription_token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Продлить подписку", callback_data="select_host_new_ALL")
    builder.button(text="🔗 Показать ссылку", callback_data=f"global_link_{subscription_token}")
    builder.button(text="📱 Показать QR-код", callback_data=f"global_qr_{subscription_token}")
    builder.button(text="📖 Инструкция", callback_data="global_howto")
    builder.button(text="📋 Список ключей (подробно)", callback_data="show_keys_detailed")
    builder.button(text="← Назад", callback_data="manage_keys")
    builder.adjust(1)
    return builder.as_markup()

def create_howto_vless_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🌍 Что такое подписка", callback_data="global_howto")
    builder.button(text="📱 Android", callback_data="howto_android")
    builder.button(text="📱 iOS", callback_data="howto_ios")
    builder.button(text="💻 Windows", callback_data="howto_windows")
    builder.button(text="🍎 MacOS", callback_data="howto_macos")
    builder.button(text="🐧 Linux", callback_data="howto_linux")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1, 2, 2, 1)
    return builder.as_markup()

def create_howto_vless_keyboard_key(key_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🌍 Что такое подписка", callback_data="global_howto")
    builder.button(text="📱 Android", callback_data="howto_android")
    builder.button(text="📱 iOS", callback_data="howto_ios")
    builder.button(text="💻 Windows", callback_data="howto_windows")
    builder.button(text="🍎 MacOS", callback_data="howto_macos")
    builder.button(text="🐧 Linux", callback_data="howto_linux")
    builder.button(text="← Назад", callback_data=f"show_key_{key_id}")
    builder.adjust(1, 2, 2, 1)
    return builder.as_markup()

def create_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    return builder.as_markup()

def create_welcome_keyboard(channel_url: str | None, is_subscription_forced: bool = False, terms_url: str | None = None, privacy_url: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if channel_url and terms_url and privacy_url and is_subscription_forced:
        builder.button(text="📢 Перейти в канал", url=channel_url)
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(text="✅ Я подписался", callback_data="check_subscription_and_agree")
    elif channel_url and terms_url and privacy_url:
        builder.button(text="📢 Наш канал (не обязательно)", url=channel_url)
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(text="✅ Принимаю условия", callback_data="check_subscription_and_agree")
    elif terms_url and privacy_url:
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(text="✅ Принимаю условия", callback_data="check_subscription_and_agree")
    elif terms_url:
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="✅ Принимаю условия", callback_data="check_subscription_and_agree")
    elif privacy_url:
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(text="✅ Принимаю условия", callback_data="check_subscription_and_agree")
    else:
        # channel_url may be None, only show it if present
        if channel_url:
            builder.button(text="📢 Наш канал (не обязательно)", url=channel_url)
        builder.button(text="✅ Я подписался", callback_data="check_subscription_and_agree")
    builder.adjust(1)
    return builder.as_markup()

def get_main_menu_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🏠 В меню", callback_data="show_main_menu")

def get_buy_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_vpn")

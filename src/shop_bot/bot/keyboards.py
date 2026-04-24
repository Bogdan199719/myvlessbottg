import logging

from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CopyTextButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.data_manager.database import get_setting, get_all_mtg_hosts
from shop_bot.utils import time_utils

logger = logging.getLogger(__name__)

main_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏠 Главное меню")]], resize_keyboard=True
)


def create_main_menu_keyboard(
    user_keys: list, trial_available: bool, is_admin: bool
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.button(
        text="💳 Купить VPN подписку", callback_data="buy_subscription", style="primary"
    )

    mtg_hosts_available = bool(get_all_mtg_hosts(only_enabled=True))
    if mtg_hosts_available:
        builder.button(
            text="📡 Купить Telegram Proxy", callback_data="buy_proxy", style="primary"
        )

    if trial_available and get_setting("trial_enabled") == "true":
        builder.button(
            text="🎁 Попробовать бесплатно", callback_data="get_trial", style="success"
        )

    builder.button(text="👤 Мой профиль", callback_data="show_profile")
    builder.button(text="📦 Мои подписки", callback_data="manage_keys")

    referral_enabled = str(get_setting("enable_referrals")).lower() == "true"
    if referral_enabled:
        builder.button(
            text="🤝 Реферальная программа", callback_data="show_referral_program"
        )

    builder.button(text="📖 Инструкция", callback_data="howto_vless")
    builder.button(text="🆘 Поддержка", callback_data="show_help")
    builder.button(text="ℹ️ О проекте", callback_data="show_about")
    if is_admin:
        builder.button(text="📢 Рассылка", callback_data="start_broadcast")

    layout = [1]
    if mtg_hosts_available:
        layout.append(1)
    if trial_available and get_setting("trial_enabled") == "true":
        layout.append(1)
    layout.append(2)  # Profile + Keys
    if referral_enabled:
        layout.append(1)
    layout.append(2)  # Инструкция + Поддержка
    layout.append(1)  # О проекте
    if is_admin:
        layout.append(1)
    builder.adjust(*layout)

    return builder.as_markup()


def create_broadcast_options_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить кнопку", callback_data="broadcast_add_button")
    builder.button(text="➡️ Пропустить", callback_data="broadcast_skip_button")
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast", style="danger")
    builder.adjust(2, 1)
    return builder.as_markup()


def create_broadcast_confirmation_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить", callback_data="confirm_broadcast", style="success"
    )
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast", style="danger")
    builder.adjust(2)
    return builder.as_markup()


def create_broadcast_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast", style="danger")
    return builder.as_markup()


def create_about_keyboard(
    channel_url: str | None, terms_url: str | None, privacy_url: str | None
) -> InlineKeyboardMarkup:
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
    builder.button(text="🆘 Написать в поддержку", url=support_user, style="primary")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_p2p_payment_keyboard(request_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить", callback_data=f"p2p_paid_{request_id}", style="success"
    )
    builder.button(text="← Назад", callback_data="back_to_payment_methods")
    builder.adjust(1)
    return builder.as_markup()


def create_p2p_submitted_keyboard(
    support_user: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if support_user:
        builder.button(
            text="🆘 Написать в поддержку", url=support_user, style="primary"
        )
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_host_selection_keyboard(
    hosts: list, action: str, back_callback: str = "manage_keys"
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for host in hosts:
        callback_data = f"select_host_{action}_{host['host_name']}"
        text = host["host_name"]
        if text == "ALL":
            text = "🌍 Глобальная подписка (Все серверы)"
        builder.button(text=text, callback_data=callback_data, style="primary")
    builder.button(
        text="← Назад" if action == "new" else "🏠 В меню",
        callback_data=back_callback if action == "new" else "back_to_main_menu",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_plans_keyboard(
    plans: list[dict], action: str, host_name: str, key_id: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in plans:
        plan_host_name = plan.get("host_name", host_name)
        callback_data = f"buy_{plan_host_name}_{plan['plan_id']}_{action}_{key_id}"
        builder.button(
            text=plan.get(
                "display_name", f"{plan['plan_name']} — {plan['price']:.0f} ₽"
            ),
            callback_data=callback_data,
            style="primary",
        )
    back_callback = "manage_keys" if action == "extend" else "back_to_main_menu"
    builder.button(text="← Назад", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()


def create_skip_email_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➡️ Продолжить без почты", callback_data="skip_email", style="primary"
    )
    builder.button(text="← Назад", callback_data="back_to_payment_methods")
    builder.adjust(1)
    return builder.as_markup()


def create_payment_method_keyboard(
    payment_methods: dict, action: str, key_id: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if payment_methods and payment_methods.get("yookassa"):
        if str(get_setting("sbp_enabled")).lower() == "true":
            builder.button(
                text="🏦 СБП / Банковская карта",
                callback_data="pay_yookassa",
                style="primary",
            )
        else:
            builder.button(
                text="🏦 Банковская карта",
                callback_data="pay_yookassa",
                style="primary",
            )
    if payment_methods and payment_methods.get("stars"):
        builder.button(
            text="⭐ Telegram Stars", callback_data="pay_stars", style="primary"
        )
    if payment_methods and payment_methods.get("p2p"):
        builder.button(
            text="💳 Оплата по карте (P2P)", callback_data="pay_p2p", style="primary"
        )
    if payment_methods and payment_methods.get("cryptobot"):
        builder.button(
            text="🤖 CryptoBot", callback_data="pay_cryptobot", style="primary"
        )

    builder.button(text="← Назад", callback_data="back_to_plans")
    builder.adjust(1)
    return builder.as_markup()


def create_payment_keyboard(payment_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Перейти к оплате", url=payment_url, style="success")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_keys_management_keyboard(keys: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    row_widths = []
    if keys:
        vpn_counter = 0
        mtg_counter = 0
        for key in keys:
            expiry_date = time_utils.parse_iso_to_msk(key.get("expiry_date"))
            now = time_utils.get_msk_now()

            if expiry_date:
                status_icon = "✅" if expiry_date > now else "❌"
                expiry_str = time_utils.format_msk(expiry_date, "%d.%m.%Y")
            else:
                status_icon = "❓"
                expiry_str = "Ошибка даты"

            host_name = key.get("host_name", "Неизвестный хост")
            is_mtg = key.get("service_type") == "mtg"

            if is_mtg:
                mtg_counter += 1
                label = f"📡 {status_icon} Прокси #{mtg_counter} ({host_name}) до {expiry_str}"
                builder.button(text=label, callback_data=f"show_key_{key['key_id']}")
                row_widths.append(1)
            else:
                vpn_counter += 1
                label = f"🔑 {status_icon} Ключ #{vpn_counter} ({host_name}) до {expiry_str}"
                builder.button(text=label, callback_data=f"show_key_{key['key_id']}")
                builder.button(text="📱 QR", callback_data=f"show_qr_{key['key_id']}")
                row_widths.append(2)

    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    row_widths.append(1)
    builder.adjust(*row_widths)
    return builder.as_markup()


def create_key_info_keyboard(
    key_id: int, copy_text: str | None = None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if copy_text:
        builder.row(
            InlineKeyboardButton(
                text="📋 Скопировать ключ",
                copy_text=CopyTextButton(text=copy_text),
                style="primary",
            )
        )
    builder.button(
        text="➕ Продлить подписку",
        callback_data=f"extend_key_{key_id}",
        style="success",
    )
    builder.button(text="📱 QR-код", callback_data=f"show_qr_{key_id}")
    builder.button(text="📖 Инструкция", callback_data=f"howto_vless_{key_id}")
    builder.button(text="← Назад", callback_data="manage_keys")
    builder.adjust(1)
    return builder.as_markup()


def create_global_link_keyboard(
    subscription_link: str, subscription_token: str
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📋 Скопировать ссылку",
            copy_text=CopyTextButton(text=subscription_link),
            style="primary",
        )
    )
    builder.button(text="📱 QR-код", callback_data=f"global_qr_{subscription_token}")
    builder.button(text="📖 Инструкция", callback_data="global_howto")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_global_sub_keyboard(subscription_token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ Продлить подписку",
        callback_data="select_host_new_ALL",
        style="success",
    )
    builder.button(
        text="🔗 Ссылка VPN подписки",
        callback_data=f"global_link_{subscription_token}",
        style="primary",
    )
    builder.button(text="📱 QR-код", callback_data=f"global_qr_{subscription_token}")
    builder.button(text="📖 Инструкция", callback_data="global_howto")
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_unified_keys_keyboard(
    keys_count: int, trial_keys_count: int = 0, mtg_keys_count: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🌍 Моя VPN подписка", callback_data="show_global_info", style="primary"
    )

    if mtg_keys_count > 0:
        builder.button(
            text="📡 Мои Telegram Proxy",
            callback_data="show_proxy_keys",
            style="primary",
        )

    if trial_keys_count > 0:
        builder.button(
            text=f"🎁 Пробный период VPN ({trial_keys_count})",
            callback_data="show_trial_keys",
        )

    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_trial_only_keyboard(
    trial_keys_count: int, mtg_keys_count: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🎁 Пробный период VPN ({trial_keys_count})",
        callback_data="show_trial_keys",
        style="primary",
    )
    if mtg_keys_count > 0:
        builder.button(
            text="📡 Мои Telegram Proxy",
            callback_data="show_proxy_keys",
            style="primary",
        )
    builder.button(text="🏠 В меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_proxy_keys_keyboard(mtg_keys: list) -> InlineKeyboardMarkup:
    """Keyboard for the proxy screen — one row of actions per proxy, no extra tap needed."""
    from shop_bot.modules.mtg_api import make_t_me_proxy_url

    builder = InlineKeyboardBuilder()
    now = time_utils.get_msk_now()
    for key in mtg_keys:
        key_id = key["key_id"]
        proxy_link = key.get("connection_string", "")
        expiry_date = time_utils.parse_iso_to_msk(key.get("expiry_date"))
        is_active = expiry_date and expiry_date > now
        if proxy_link:
            # Connect button (full width, most used action)
            connect_url = make_t_me_proxy_url(proxy_link)
            builder.row(
                InlineKeyboardButton(
                    text="🔌 Подключить прокси", url=connect_url, style="success"
                )
            )
            # Copy + Extend on same row
            builder.row(
                InlineKeyboardButton(
                    text="📋 Скопировать ссылку",
                    copy_text=CopyTextButton(text=proxy_link),
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="➕ Продлить",
                    callback_data=f"extend_key_{key_id}",
                    style="success",
                ),
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text="➕ Продлить",
                    callback_data=f"extend_key_{key_id}",
                    style="success",
                )
            )
    builder.row(
        InlineKeyboardButton(
            text="📡 Купить ещё Proxy", callback_data="buy_proxy", style="primary"
        )
    )
    builder.row(InlineKeyboardButton(text="← Назад", callback_data="manage_keys"))
    return builder.as_markup()


def create_global_info_keyboard(subscription_token: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ Продлить подписку",
        callback_data="select_host_new_ALL",
        style="success",
    )
    builder.button(
        text="🔗 Ссылка VPN подписки",
        callback_data=f"global_link_{subscription_token}",
        style="primary",
    )
    builder.button(text="📱 QR-код", callback_data=f"global_qr_{subscription_token}")
    builder.button(text="📖 Инструкция", callback_data="global_howto")
    builder.button(
        text="📋 Список ключей (подробно)", callback_data="show_keys_detailed"
    )
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


def create_welcome_keyboard(
    channel_url: str | None,
    is_subscription_forced: bool = False,
    terms_url: str | None = None,
    privacy_url: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if channel_url and terms_url and privacy_url and is_subscription_forced:
        builder.button(text="📢 Перейти в канал", url=channel_url)
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(
            text="✅ Я подписался",
            callback_data="check_subscription_and_agree",
            style="success",
        )
    elif channel_url and terms_url and privacy_url:
        builder.button(text="📢 Наш канал (не обязательно)", url=channel_url)
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(
            text="✅ Принимаю условия",
            callback_data="check_subscription_and_agree",
            style="success",
        )
    elif terms_url and privacy_url:
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(
            text="✅ Принимаю условия",
            callback_data="check_subscription_and_agree",
            style="success",
        )
    elif terms_url:
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(
            text="✅ Принимаю условия",
            callback_data="check_subscription_and_agree",
            style="success",
        )
    elif privacy_url:
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(
            text="✅ Принимаю условия",
            callback_data="check_subscription_and_agree",
            style="success",
        )
    else:
        if channel_url:
            builder.button(text="📢 Наш канал (не обязательно)", url=channel_url)
        builder.button(
            text="✅ Я подписался",
            callback_data="check_subscription_and_agree",
            style="success",
        )
    builder.adjust(1)
    return builder.as_markup()


def create_mtg_host_selection_keyboard(
    hosts: list, back_callback: str = "back_to_main_menu"
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for host in hosts:
        builder.button(
            text=host["host_name"],
            callback_data=f"select_mtg_host_{host['host_name']}",
            style="primary",
        )
    builder.button(text="← Назад", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()


def create_proxy_info_keyboard(key_id: int, proxy_link: str) -> InlineKeyboardMarkup:
    from shop_bot.modules.mtg_api import make_t_me_proxy_url

    builder = InlineKeyboardBuilder()
    if proxy_link:
        builder.row(
            InlineKeyboardButton(
                text="📋 Скопировать ссылку",
                copy_text=CopyTextButton(text=proxy_link),
                style="primary",
            )
        )
        builder.button(
            text="🔌 Подключить прокси",
            url=make_t_me_proxy_url(proxy_link),
            style="success",
        )
    builder.button(
        text="➕ Продлить", callback_data=f"extend_key_{key_id}", style="success"
    )
    builder.button(text="← Назад", callback_data="manage_keys")
    builder.adjust(1)
    return builder.as_markup()

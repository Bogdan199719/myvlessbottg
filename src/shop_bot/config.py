CHOOSE_PLAN_MESSAGE = "Выберите тариф:"
CHOOSE_PROXY_HOST_MESSAGE = "Выберите сервер Telegram Proxy:"
CHOOSE_PAYMENT_METHOD_MESSAGE = "Выберите способ оплаты:"
VPN_INACTIVE_TEXT = "❌ истёк"
VPN_NO_DATA_TEXT = "нет подписки"


def get_profile_text(username, total_spent, vpn_status_text):
    # Kept for backward compatibility — profile is built inline in the handler
    return f"👤 <b>{username}</b>\n\n{vpn_status_text}"


def get_vpn_active_text(days_left, hours_left):
    return f"✅ активен · ещё {days_left} дн. {hours_left} ч."


def get_key_info_text(key_number, expiry_date, created_date, connection_string):
    expiry_fmt = expiry_date.strftime("%d.%m.%Y в %H:%M")
    created_fmt = created_date.strftime("%d.%m.%Y")
    return (
        f"🔑 <b>Ключ #{key_number}</b>\n"
        f"<blockquote>📅 Действует до {expiry_fmt} (МСК)\n"
        f"🗓 Куплен {created_fmt}</blockquote>\n\n"
        f"<code>{connection_string}</code>"
    )


def get_purchase_success_text(
    action: str, key_number: int, expiry_date, connection_string: str
):
    verb = "продлён" if action == "extend" else "готов"
    expiry_fmt = expiry_date.strftime("%d.%m.%Y в %H:%M")
    return (
        f"🎉 <b>Ключ #{key_number} {verb}!</b>\n"
        f"<blockquote>📅 Действует до {expiry_fmt} (МСК)</blockquote>\n\n"
        f"<code>{connection_string}</code>"
    )


def get_proxy_purchase_success_text(
    action: str, key_number: int, expiry_date, proxy_link: str
):
    verb = "продлён" if action == "extend" else "готов"
    expiry_fmt = expiry_date.strftime("%d.%m.%Y в %H:%M")
    return (
        f"🎉 <b>Telegram Proxy #{key_number} {verb}!</b>\n"
        f"<blockquote>📅 Действует до {expiry_fmt} (МСК)</blockquote>\n\n"
        f"Нажмите <b>🔌 Подключить</b> или скопируйте ссылку:\n\n"
        f"<code>{proxy_link}</code>"
    )


def get_proxy_info_text(key_number: int, expiry_date, created_date, proxy_link: str):
    expiry_fmt = expiry_date.strftime("%d.%m.%Y в %H:%M")
    created_fmt = created_date.strftime("%d.%m.%Y")
    return (
        f"📡 <b>Telegram Proxy #{key_number}</b>\n"
        f"<blockquote>📅 Действует до {expiry_fmt} (МСК)\n"
        f"🗓 Куплен {created_fmt}</blockquote>\n\n"
        f"<code>{proxy_link}</code>"
    )

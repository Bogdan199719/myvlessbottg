#!/usr/bin/env python3
"""Validate Stopurban metadata exposed to Happ without touching runtime data."""

import base64
from datetime import timedelta
from pathlib import Path

from shop_bot.utils import time_utils
from shop_bot.webhook_server.subscription_api import (
    _build_telegram_renew_url,
    _safe_external_url,
    _subscription_expiry,
    _subscription_update_interval_hours,
)


def main() -> int:
    now = time_utils.get_msk_now()
    earlier = now + timedelta(days=10)
    later = now + timedelta(days=30)
    expired = now - timedelta(days=2)

    all_keys = [
        {"service_type": "xui", "expiry_date": expired.isoformat()},
        {"service_type": "xui", "expiry_date": later.isoformat()},
        {"service_type": "mtg", "expiry_date": (later + timedelta(days=5)).isoformat()},
    ]
    selected = [
        {"service_type": "xui", "expiry_date": earlier.isoformat()},
        {"service_type": "xui", "expiry_date": later.isoformat()},
    ]

    assert _subscription_expiry(all_keys, selected) == earlier
    assert _subscription_expiry(all_keys, []) == later
    assert _subscription_expiry([], []) is None

    assert (
        _build_telegram_renew_url("@stopurban_bot")
        == "https://t.me/stopurban_bot?start=renew"
    )
    assert _build_telegram_renew_url("bad name") is None
    assert _subscription_update_interval_hours("6") == 6
    assert _subscription_update_interval_hours("24") == 24
    assert _subscription_update_interval_hours("2") == 6
    assert _subscription_update_interval_hours("bad") == 6

    assert _safe_external_url("https://t.me/stopurban_support")
    assert (
        _safe_external_url("t.me/stopurban_support")
        == "https://t.me/stopurban_support"
    )
    assert (
        _safe_external_url("@stopurban_support")
        == "https://t.me/stopurban_support"
    )
    assert _safe_external_url("javascript:alert(1)") is None
    assert _safe_external_url("https://example.com/\r\nInjected: value") is None

    root = Path(__file__).resolve().parents[1]
    subscription_source = (
        root / "src/shop_bot/webhook_server/subscription_api.py"
    ).read_text(encoding="utf-8")
    handlers_source = (root / "src/shop_bot/bot/handlers.py").read_text(
        encoding="utf-8"
    )
    assert "expire=0" not in subscription_source
    assert '"Support-Url"' in subscription_source
    assert '"Profile-Web-Page-Url"' in subscription_source
    assert '"Announce"' in subscription_source
    assert 'f"base64:{encoded_announce}"' in subscription_source
    assert 'command.args == "renew"' in handlers_source

    announce = "🔥 Безлимитный VPN. Продление и поддержка — в Telegram."
    encoded_announce = base64.b64encode(announce.encode("utf-8")).decode("ascii")
    assert base64.b64decode(encoded_announce).decode("utf-8") == announce

    print("OK: Happ expiry and Telegram metadata are safe and consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

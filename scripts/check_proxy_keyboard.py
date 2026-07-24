#!/usr/bin/env python3
"""Check that expired MTG proxy credentials are not offered as active actions."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shop_bot.bot.keyboards import create_proxy_keys_keyboard  # noqa: E402
from shop_bot.utils import time_utils  # noqa: E402


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def main() -> int:
    now = time_utils.get_msk_now()
    base_key = {
        "key_id": 123,
        "connection_string": (
            "tg://proxy?server=proxy.example&port=443&secret=0123456789abcdef"
        ),
    }

    active = create_proxy_keys_keyboard(
        [{**base_key, "expiry_date": (now + timedelta(days=1)).isoformat()}]
    )
    active_buttons = _buttons(active)
    assert any(button.text == "🔌 Подключить прокси" for button in active_buttons)
    assert any(button.text == "📋 Скопировать ссылку" for button in active_buttons)
    assert any(button.text == "➕ Продлить" for button in active_buttons)

    expired = create_proxy_keys_keyboard(
        [{**base_key, "expiry_date": (now - timedelta(seconds=1)).isoformat()}]
    )
    expired_buttons = _buttons(expired)
    assert not any(button.text == "🔌 Подключить прокси" for button in expired_buttons)
    assert not any(button.text == "📋 Скопировать ссылку" for button in expired_buttons)
    assert any(button.text == "➕ Продлить" for button in expired_buttons)

    print("Proxy keyboard checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Guard partial onboarding configuration and welcome keyboard behavior."""

from __future__ import annotations

from pathlib import Path

from shop_bot.bot.keyboards import create_welcome_keyboard


ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "src" / "shop_bot" / "bot" / "handlers.py"


def _buttons(**kwargs):
    markup = create_welcome_keyboard(**kwargs)
    return [button for row in markup.inline_keyboard for button in row]


def main() -> int:
    source = HANDLERS.read_text(encoding="utf-8")
    assert "if not channel_url or not terms_url or not privacy_url" not in source
    assert "terms_url or privacy_url" in source

    forced = _buttons(
        channel_url="https://t.me/example",
        is_subscription_forced=True,
        terms_url=None,
        privacy_url="https://example.test/privacy",
    )
    assert [button.url for button in forced[:-1]] == [
        "https://t.me/example",
        "https://example.test/privacy",
    ]
    assert forced[-1].callback_data == "check_subscription_and_agree"
    assert forced[-1].text == "✅ Я подписался"

    terms_only = _buttons(
        channel_url=None,
        is_subscription_forced=False,
        terms_url="https://example.test/terms",
        privacy_url=None,
    )
    assert terms_only[0].url == "https://example.test/terms"
    assert terms_only[-1].text == "✅ Принимаю условия"

    print("Onboarding safety checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

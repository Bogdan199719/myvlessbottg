#!/usr/bin/env python3
"""Validate that settings used by the app have database defaults."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from shop_bot.data_manager.database import DEFAULT_BOT_SETTINGS

REQUIRED_DEFAULT_SETTINGS = {
    "panel_login",
    "panel_password",
    "flask_secret_key",
    "telegram_bot_token",
    "telegram_bot_username",
    "admin_telegram_id",
    "support_bot_token",
    "support_group_id",
    "enable_global_plans",
    "enable_admin_payment_notifications",
    "enable_admin_trial_notifications",
    "email_prompt_enabled",
    "subscription_name",
    "subscription_live_sync",
    "subscription_live_stats",
    "subscription_allow_fallback_host_fetch",
    "subscription_auto_provision",
    "provision_timeout_seconds",
    "panel_sync_enabled",
    "xtls_sync_enabled",
}


def main() -> int:
    defaults = set(DEFAULT_BOT_SETTINGS)
    missing = sorted(REQUIRED_DEFAULT_SETTINGS - defaults)

    print(f"Default settings declared: {len(defaults)}")
    print(f"Required settings checked: {len(REQUIRED_DEFAULT_SETTINGS)}")

    if missing:
        print("\nERROR: settings used by code/UI without defaults:")
        for key in missing:
            print(f" - {key}")
        return 1

    print("\nOK: critical app/UI settings have database defaults.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

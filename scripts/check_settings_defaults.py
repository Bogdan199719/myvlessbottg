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
    "admin_ip_allowlist",
    "telegram_bot_token",
    "telegram_bot_username",
    "admin_telegram_id",
    "support_bot_token",
    "support_group_id",
    "show_about_menu_item",
    "enable_global_plans",
    "enable_admin_payment_notifications",
    "enable_admin_trial_notifications",
    "email_prompt_enabled",
    "subscription_name",
    "subscription_update_interval_hours",
    "subscription_announce",
    "auto_selector_enabled",
    "auto_selector_max_cpu_percent",
    "auto_selector_max_memory_percent",
    "auto_selector_health_max_age_seconds",
    "ip_limit_enabled",
    "ip_limit_max_ips",
    "ip_limit_warning_grace_hours",
    "subscription_live_sync",
    "subscription_live_stats",
    "subscription_allow_fallback_host_fetch",
    "subscription_auto_provision",
    "happ_routing_enabled",
    "happ_routing_rules",
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

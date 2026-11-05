#!/usr/bin/env python3
"""Exercise expiry reminders with a synthetic DB and a fake Telegram bot."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def check_notifications(database, scheduler, time_utils):
    from shop_bot.utils.subscription_rules import configured_trial_days, is_trial_xui_key
    from shop_bot.webhook_server.app import _key_is_trial_for_user

    now = time_utils.get_msk_now().replace(microsecond=0)
    database.register_user_if_not_exists(101, "synthetic-notifications", None)
    for value in (None, "invalid", "nan", "inf", "0", "-1"):
        assert configured_trial_days(value) == 1.0
    assert configured_trial_days("3") == 3.0
    legacy_trial = {
        "plan_id": 0,
        "created_date": now.isoformat(),
        "expiry_date": (now + timedelta(hours=30)).isoformat(),
    }
    assert is_trial_xui_key(legacy_trial)
    assert not is_trial_xui_key(
        {**legacy_trial, "expiry_date": (now + timedelta(hours=30, seconds=1)).isoformat()}
    )
    assert is_trial_xui_key(
        {**legacy_trial, "expiry_date": (now + timedelta(days=3)).isoformat()}, 3
    )
    assert is_trial_xui_key({"plan_id": 0})

    def keys_for(kind, expiry, count=3):
        return [
            {
                "user_id": 101,
                "key_id": index + 1,
                "plan_id": 0 if kind in {"trial", "free", "proxy"} else 27,
                "service_type": "mtg" if kind == "proxy" else "xui",
                "host_name": f"synthetic-{index}",
                "key_email": f"synthetic-global-{index}",
                "created_date": (
                    expiry - timedelta(days=1 if kind == "trial" else 30)
                ).isoformat(),
                "expiry_date": expiry.isoformat(),
            }
            for index in range(count)
        ]

    def reset():
        with sqlite3.connect(database.DB_FILE) as conn:
            conn.execute("DELETE FROM sent_notifications")

    async def run(keys, bot):
        with (
            patch.object(scheduler.database, "get_all_keys", return_value=keys),
            patch.object(scheduler.database, "get_global_plan_ids", return_value={27}),
        ):
            await scheduler.check_expiring_subscriptions(bot)

    with (
        patch.object(time_utils, "get_msk_now", return_value=now),
        patch.object(database, "get_setting", return_value="1"),
    ):
        # Exercise the real dispatcher and persisted deduplication for every
        # product, including free access retaining plan_id=0 and MTG plan_id=0.
        for kind in ("paid", "trial", "free", "proxy"):
            expected = (
                scheduler.TRIAL_NOTIFY_HOURS
                if kind == "trial"
                else scheduler.PAID_NOTIFY_HOURS
            )
            for hours in sorted(scheduler.PAID_NOTIFY_HOURS):
                reset()
                expiry = now + timedelta(hours=hours, minutes=-30)
                keys = keys_for(kind, expiry, count=1 if kind == "proxy" else 3)
                bot = SimpleNamespace(send_message=AsyncMock())
                await run(keys, bot)
                assert bot.send_message.await_count == int(hours in expected), (
                    kind, hours, "incorrect reminder chain or per-host duplicates"
                )
                await run(keys, bot)
                assert bot.send_message.await_count == int(hours in expected), (
                    kind, hours, "duplicate on next scheduler pass"
                )
                if kind != "proxy":
                    assert is_trial_xui_key(keys[0]) == (kind == "trial")
                    assert _key_is_trial_for_user(
                        keys[0], trial_duration_days=1
                    ) == (kind == "trial")
                else:
                    assert not is_trial_xui_key(keys[0])

        # A new absolute expiry starts its own cycle, even within the same
        # reminder window. The previous cycle must not suppress it.
        reset()
        bot = SimpleNamespace(send_message=AsyncMock())
        await run(keys_for("paid", now + timedelta(minutes=30)), bot)
        later = now + timedelta(days=30)
        with patch.object(time_utils, "get_msk_now", return_value=later):
            await run(keys_for("paid", later + timedelta(minutes=30)), bot)
        assert bot.send_message.await_count == 2

        # Telegram/network failure is retried; it must not create a sent row.
        reset()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline")))
        keys = keys_for("free", now + timedelta(minutes=30))
        await run(keys, bot)
        with sqlite3.connect(database.DB_FILE) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sent_notifications").fetchone()[0] == 0
        bot.send_message.side_effect = None
        await run(keys, bot)
        assert bot.send_message.await_count == 2

        # Paid VPN suppresses old trial reminders, while proxy is independent.
        reset()
        bot = SimpleNamespace(send_message=AsyncMock())
        expiry = now + timedelta(minutes=30)
        await run(
            keys_for("paid", expiry)
            + keys_for("trial", expiry)
            + keys_for("proxy", expiry, count=1),
            bot,
        )
        assert bot.send_message.await_count == 2

        # A successful old per-host trial reminder suppresses the new grouped
        # flow during migration, including when the trial has several hosts.
        reset()
        bot = SimpleNamespace(send_message=AsyncMock())
        expiry = now + timedelta(minutes=30)
        database.mark_notification_sent(
            101, 1, scheduler._notification_type_for_expiry_cycle("expiry", expiry), 1
        )
        await run(keys_for("trial", expiry), bot)
        bot.send_message.assert_not_awaited()

    # All onboarding stages must agree with the trial setting in both copy
    # and buttons. This checks actual Telegram message payloads.
    for enabled in (False, True):
        with patch.object(scheduler, "_bool_setting", return_value=enabled):
            for hours in scheduler.ONBOARDING_IDLE_NOTIFY_HOURS:
                bot = SimpleNamespace(send_message=AsyncMock())
                assert await scheduler.send_idle_onboarding_notification(bot, 101, hours)
                payload = bot.send_message.await_args.kwargs
                callbacks = [
                    button.callback_data
                    for row in payload["reply_markup"].inline_keyboard
                    for button in row
                ]
                assert ("get_trial" in callbacks) == enabled
                assert "buy_subscription" in callbacks
                if not enabled:
                    text = payload["text"].lower()
                    assert all(word not in text for word in ("пробн", "бесплат", "без оплаты"))


def main():
    with tempfile.TemporaryDirectory(prefix="subscription-notifications-") as folder:
        os.environ["DB_PATH"] = str(Path(folder) / "synthetic.db")
        from shop_bot.data_manager import database, scheduler
        from shop_bot.utils import time_utils

        database.initialize_db()
        asyncio.run(check_notifications(database, scheduler, time_utils))
    print("Subscription notification checks: OK")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Exercise subscription classification and resumable promo invariants."""

from __future__ import annotations

import os
import sys
import tempfile
import ast
import json
import sqlite3
from datetime import timedelta
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))

    handlers_source = (root / "src/shop_bot/bot/handlers.py").read_text(
        encoding="utf-8"
    )
    app_source = (root / "src/shop_bot/webhook_server/app.py").read_text(
        encoding="utf-8"
    )
    scheduler_source = (
        root / "src/shop_bot/data_manager/scheduler.py"
    ).read_text(encoding="utf-8")
    ast.parse(handlers_source)
    ast.parse(app_source)
    ast.parse(scheduler_source)
    assert (
        "Trial key was created on host %s but DB persistence failed." in handlers_source
    )
    assert "Trial access was issued for user %s" in handlers_source
    assert "create_or_update_key_on_host_absolute_expiry(" in handlers_source
    assert "добавлена автоматически" in handlers_source
    assert "без изменения даты окончания" in handlers_source
    assert "if issued_count != len(hosts):" in app_source
    assert "Статистика не начислена" in app_source
    assert "PAID_NOTIFY_HOURS = {24, 1, 0, -24, -72, -168}" in scheduler_source
    assert "TRIAL_NOTIFY_HOURS = {1, 0, -24, -72}" in scheduler_source
    assert "ONBOARDING_IDLE_NOTIFY_HOURS = (3, 24, 72)" in scheduler_source
    assert 'ONBOARDING_IDLE_NOTIFICATION_TYPE = "onboarding_idle"' in scheduler_source
    assert "Твоя подписка уже 3 дня отдыхает без тебя" in scheduler_source
    assert "Подписка закончилась неделю назад" in scheduler_source
    assert "Ты заходил посмотреть VPN" in scheduler_source

    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["DB_PATH"] = str(Path(tmp_dir) / "test.db")

        from shop_bot.data_manager import database
        from shop_bot.utils import time_utils
        from shop_bot.webhook_server.app import _summarize_user_subscription

        database.initialize_db()
        database.run_migration()
        database.register_user_if_not_exists(101, "promo-user", None)
        database.register_user_if_not_exists(102, "referrer", None)
        database.register_user_if_not_exists(103, "idle-user", None)
        database.register_user_if_not_exists(104, "trial-used-user", None)
        database.register_user_if_not_exists(105, "key-user", None)
        database.register_user_if_not_exists(106, "tx-user", None)

        with sqlite3.connect(database.DB_FILE) as conn:
            old_registration = (
                time_utils.get_msk_now() - timedelta(hours=3, minutes=15)
            ).isoformat()
            for user_id in (103, 104, 105, 106):
                conn.execute(
                    """
                    UPDATE users
                    SET registration_date = ?, agreed_to_terms = 1
                    WHERE telegram_id = ?
                    """,
                    (old_registration, user_id),
                )
            conn.execute("UPDATE users SET trial_used = 1 WHERE telegram_id = 104")
            conn.execute(
                """
                INSERT INTO vpn_keys
                    (user_id, host_name, xui_client_uuid, key_email, expiry_date,
                     created_date, connection_string, plan_id, service_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    101,
                    "test-host",
                    "client-1",
                    "user101-global-testhost",
                    time_utils.get_msk_now(),
                    time_utils.get_msk_now(),
                    "vless://old",
                    0,
                    "xui",
                ),
            )
            key_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                """
                INSERT INTO vpn_keys
                    (user_id, host_name, xui_client_uuid, key_email, expiry_date,
                     created_date, connection_string, plan_id, service_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    105,
                    "test-host",
                    "client-105",
                    "user105-global-testhost",
                    time_utils.get_msk_now() + timedelta(days=1),
                    time_utils.get_msk_now(),
                    "vless://105",
                    0,
                    "xui",
                ),
            )

        assert database.create_pending_transaction("idle-filter-tx", 106, 100.0, {})
        idle_users = database.get_idle_onboarding_users(3, 5, 10)
        idle_ids = {int(user["telegram_id"]) for user in idle_users}
        assert 103 in idle_ids
        assert 104 not in idle_ids
        assert 105 not in idle_ids
        assert 106 not in idle_ids

        assert database.update_key_info(
            key_id,
            time_utils.get_msk_now() + timedelta(days=1),
            "vless://new",
        )
        assert database.update_key_plan_id(key_id, 1)
        assert not database.update_key_info(999999, time_utils.get_msk_now())
        assert not database.update_key_plan_id(999999, 1)

        payment_metadata = {
            "user_id": 101,
            "host_name": "ALL",
            "payment_method": "Telegram Stars",
            "provider_payment_id": "stars-recovery",
            "fulfillment_target_expiry_ms": 2_000_000_000_000,
        }
        assert database.create_pending_transaction(
            "stars-recovery", 101, 100.0, payment_metadata
        )
        with sqlite3.connect(database.DB_FILE) as conn:
            conn.execute(
                "UPDATE transactions SET status = 'expired' WHERE payment_id = ?",
                ("stars-recovery",),
            )
        reserved = database.reserve_pending_transaction(
            "stars-recovery",
            payment_method="Telegram Stars",
            amount_currency=50,
            currency_name="XTR",
            allowed_statuses=("pending", "expired"),
        )
        assert reserved
        reserved.update(payment_metadata)
        reserved["processing_started_at"] = (
            time_utils.get_msk_now() - timedelta(minutes=20)
        ).isoformat()
        assert database.update_reserved_transaction_metadata(
            "stars-recovery", reserved
        )
        assert database.recover_stale_global_processing_transactions(15) == 1
        with sqlite3.connect(database.DB_FILE) as conn:
            status, stored_metadata = conn.execute(
                "SELECT status, metadata FROM transactions WHERE payment_id = ?",
                ("stars-recovery",),
            ).fetchone()
        assert status == "pending"
        assert json.loads(stored_metadata).get(
            "recovered_from_interrupted_processing_at"
        )

        accounting_metadata = {
            "user_id": 101,
            "host_name": "ALL",
            "provider_payment_id": "accounting-once",
        }
        assert database.create_pending_transaction(
            "accounting-once", 101, 200.0, accounting_metadata
        )
        assert database.reserve_pending_transaction("accounting-once")
        assert database.apply_payment_accounting_once(
            "accounting-once", 101, 200.0, 1, 102, 20.0, accounting_metadata
        )
        assert not database.apply_payment_accounting_once(
            "accounting-once", 101, 200.0, 1, 102, 20.0, accounting_metadata
        )
        with sqlite3.connect(database.DB_FILE) as conn:
            spent, months = conn.execute(
                "SELECT total_spent, total_months FROM users WHERE telegram_id = 101"
            ).fetchone()
            balance, balance_all = conn.execute(
                """
                SELECT referral_balance, referral_balance_all
                FROM users WHERE telegram_id = 102
                """
            ).fetchone()
        assert spent == 200.0 and months == 1
        assert balance == 20.0 and balance_all == 20.0

        created, message = database.create_promo_code("RESUME", 10, 1, None)
        assert created, message

        status, promo = database.claim_promo_code("resume", 101)
        assert status == "ok" and promo
        assert promo.get("fulfillment_target_expiry_ms") is None

        target_expiry_ms = 2_000_000_000_000
        stored_target = database.set_promo_fulfillment_target(
            int(promo["promo_id"]), 101, target_expiry_ms
        )
        assert stored_target == target_expiry_ms

        status, resumed = database.claim_promo_code("RESUME", 101)
        assert status == "ok" and resumed
        assert resumed["fulfillment_target_expiry_ms"] == target_expiry_ms

        assert database.mark_promo_code_applied(int(promo["promo_id"]), 101)
        status, _ = database.claim_promo_code("RESUME", 101)
        assert status == "already_used"

        now = time_utils.get_msk_now()
        active_expiry = (now + timedelta(days=10)).isoformat()
        expired_expiry = (now - timedelta(days=1)).isoformat()
        base_key = {
            "service_type": "xui",
            "plan_id": 1,
            "expiry_date": active_expiry,
            "created_date": now.isoformat(),
        }

        free_summary = _summarize_user_subscription(
            {"paid_transaction_count": 0, "free_transaction_count": 1},
            [base_key],
            now,
        )
        assert free_summary["status"] == "free"

        paid_summary = _summarize_user_subscription(
            {"paid_transaction_count": 1, "free_transaction_count": 0},
            [base_key],
            now,
        )
        assert paid_summary["status"] == "paid"

        trial_summary = _summarize_user_subscription(
            {"trial_used": 1, "paid_transaction_count": 0},
            [
                {
                    **base_key,
                    "plan_id": 0,
                    "expiry_date": (now + timedelta(days=1)).isoformat(),
                }
            ],
            now,
        )
        assert trial_summary["status"] == "trial"

        extended_trial_summary = _summarize_user_subscription(
            {"trial_used": 1, "paid_transaction_count": 0},
            [{**base_key, "plan_id": 0}],
            now,
        )
        assert extended_trial_summary["status"] == "free"

        expired_free_summary = _summarize_user_subscription(
            {"paid_transaction_count": 0, "free_transaction_count": 1},
            [{**base_key, "expiry_date": expired_expiry}],
            now,
        )
        assert expired_free_summary["status"] == "free_expired"

    print("Subscription business-rule checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

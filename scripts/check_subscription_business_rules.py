#!/usr/bin/env python3
"""Exercise subscription classification and resumable promo invariants."""

from __future__ import annotations

import os
import sys
import tempfile
import ast
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
    ast.parse(handlers_source)
    ast.parse(app_source)
    assert (
        "Trial key was created on host %s but DB persistence failed." in handlers_source
    )
    assert "if issued_count != len(hosts):" in app_source
    assert "Статистика не начислена" in app_source

    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["DB_PATH"] = str(Path(tmp_dir) / "test.db")

        from shop_bot.data_manager import database
        from shop_bot.utils import time_utils
        from shop_bot.webhook_server.app import _summarize_user_subscription

        database.initialize_db()
        database.run_migration()
        database.register_user_if_not_exists(101, "promo-user", None)

        with sqlite3.connect(database.DB_FILE) as conn:
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

        assert database.update_key_info(
            key_id,
            time_utils.get_msk_now() + timedelta(days=1),
            "vless://new",
        )
        assert database.update_key_plan_id(key_id, 1)
        assert not database.update_key_info(999999, time_utils.get_msk_now())
        assert not database.update_key_plan_id(999999, 1)

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

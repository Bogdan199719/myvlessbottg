#!/usr/bin/env python3
"""Check restart-only payment recovery against an isolated temporary SQLite DB."""

from __future__ import annotations

import copy
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="check-early-payment-") as temporary:
        # Set the path before importing application modules. No dotenv loading,
        # app startup, network calls or production data are needed.
        db_path = Path(temporary) / "check.db"
        os.environ["DB_PATH"] = str(db_path)
        sys.path.insert(0, str(ROOT / "src"))
        from shop_bot.data_manager import database
        from shop_bot.utils import time_utils

        assert database.DB_FILE.resolve() == db_path.resolve()
        current = database._PAYMENT_PROCESS_INSTANCE_ID
        assert isinstance(current, str) and uuid.UUID(hex=current).hex == current
        previous = uuid.uuid4().hex
        assert previous != current
        real_connect = sqlite3.connect
        with real_connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY, username TEXT,
                    total_spent REAL, total_months INTEGER,
                    referral_balance REAL, referral_balance_all REAL,
                    pending_payment INTEGER
                );
                CREATE TABLE transactions (
                    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT, payment_id TEXT UNIQUE NOT NULL,
                    user_id INTEGER, status TEXT, amount_rub REAL,
                    amount_currency REAL, currency_name TEXT,
                    payment_method TEXT, metadata TEXT,
                    created_date TEXT, paid_date TEXT
                );
                CREATE TABLE p2p_requests (
                    user_id INTEGER, referral_discount_applied INTEGER,
                    request_id TEXT
                );
                INSERT INTO users VALUES (101, 'fake-user', 750, 3, 45, 90, 1);
                INSERT INTO users VALUES (102, 'fake-referrer', 500, 2, 80, 120, 1);
                """
            )

        def users_snapshot():
            with real_connect(db_path) as conn:
                return conn.execute("SELECT * FROM users ORDER BY telegram_id").fetchall()

        def row(payment_id):
            with real_connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                value = conn.execute(
                    "SELECT * FROM transactions WHERE payment_id = ?", (payment_id,)
                ).fetchone()
                return dict(value)

        def put(payment_id, metadata, *, status="processing", method="YooKassa"):
            raw = metadata if isinstance(metadata, str) else json.dumps(metadata)
            with real_connect(db_path) as conn:
                conn.execute(
                    """INSERT INTO transactions
                    (payment_id,user_id,status,amount_rub,amount_currency,currency_name,
                     payment_method,metadata,created_date)
                    VALUES (?,101,?,250,250,'RUB',?,?,?)""",
                    (payment_id, status, method, raw, old_timestamp),
                )

        def early_metadata(payment_id):
            return {
                "provider_payment_id": payment_id,
                "fulfillment_protocol_version": 1,
                "processing_instance_id": previous,
                "processing_started_at": old_timestamp,
                "user_id": 101,
                "plan_id": 7,
                "host_name": "ALL",
                "service_type": "xui",
                "payment_method": "YooKassa",
                "months": 1,
                "amount_rub": 250,
                "referral_discount_applied": False,
                "fulfillment_status_message_sent_at": old_timestamp,
            }

        old_timestamp = (time_utils.get_msk_now() - timedelta(hours=2)).isoformat()
        financial_before = users_snapshot()
        invoice = {"provider_payment_id": "created", "plan_id": 7, "months": 1}
        invoice_before = copy.deepcopy(invoice)
        assert database.create_pending_transaction("created", 101, 250, invoice)
        assert invoice == invoice_before, "Creation must not mutate the caller's invoice"
        assert json.loads(row("created")["metadata"])["fulfillment_protocol_version"] == 1
        reservation = database.reserve_pending_transaction(
            "created", payment_method="YooKassa", preserve_fulfillment_state=True
        )
        assert reservation["processing_instance_id"] == current

        assert database.create_pending_transaction("default", 101, 250, {})
        reservation = database.reserve_pending_transaction("default")
        assert "processing_instance_id" not in reservation

        put("legacy-incoming-version", {"provider_payment_id": "legacy-incoming-version"},
            status="pending")
        reservation = database.reserve_pending_transaction(
            "legacy-incoming-version",
            {"fulfillment_protocol_version": 1},
            payment_method="YooKassa",
            preserve_fulfillment_state=True,
        )
        assert "fulfillment_protocol_version" not in reservation
        assert reservation["processing_instance_id"] == current

        eligible = "eligible"
        original = early_metadata(eligible)
        put(eligible, original)
        eligible_before = row(eligible)
        excluded = []

        def exclude(name, mutate=None, *, status="processing", method="YooKassa", raw=None):
            metadata = early_metadata(name)
            if mutate:
                mutate(metadata)
            put(name, metadata if raw is None else raw, status=status, method=method)
            excluded.append((name, row(name)))

        exclude("legacy-no-version", lambda m: m.pop("fulfillment_protocol_version"))
        for version in (0, 2, "1", True, None, 1.0):
            exclude(f"version-{type(version).__name__}-{version}",
                    lambda m, v=version: m.update(fulfillment_protocol_version=v))
        exclude("missing-owner", lambda m: m.pop("processing_instance_id"))
        exclude("current-owner", lambda m: m.update(processing_instance_id=current))
        for owner in (None, "", "broken", 123, "f" * 31, "g" * 32):
            exclude(f"owner-{owner}", lambda m, v=owner: m.update(processing_instance_id=v))
        for target in (None, 0, "", False, 1, 1893456000000):
            exclude(f"target-{type(target).__name__}-{target}",
                    lambda m, v=target: m.update(fulfillment_target_expiry_ms=v))
        for accounting in (True, 1, "true", "false"):
            exclude(f"accounting-{accounting}",
                    lambda m, v=accounting: m.update(accounting_applied=v))
        exclude("other-fulfillment", lambda m: m.update(fulfillment_user_notified=False))
        exclude("mismatched-provider", lambda m: m.update(provider_payment_id="different"))
        exclude("missing-provider", lambda m: m.pop("provider_payment_id"))
        exclude("missing-metadata-method", lambda m: m.pop("payment_method"))
        exclude("wrong-metadata-method", lambda m: m.update(payment_method="CryptoBot"))
        exclude("missing-service", lambda m: m.pop("service_type"))
        exclude("wrong-service", lambda m: m.update(service_type="other"))
        for method in ("Telegram Stars", "CryptoBot", "P2P", None):
            exclude(f"method-{method}", method=method)
        for status in ("pending", "paid", "expired"):
            exclude(f"status-{status}", status=status)
        for index, raw in enumerate(("{broken", "[]", "null", '"string"', "1")):
            exclude(f"raw-{index}", raw=raw)

        assert database.recover_pre_fulfillment_yookassa_on_startup() == 1
        recovered = row(eligible)
        assert recovered["status"] == "pending"
        recovered_metadata = json.loads(recovered["metadata"])
        for key, value in original.items():
            assert recovered_metadata[key] == value, f"Invoice/checkpoint changed: {key}"
        for key in eligible_before.keys() - {"status", "metadata"}:
            assert recovered[key] == eligible_before[key], f"Transaction field changed: {key}"
        assert users_snapshot() == financial_before, "Recovery must not change balances or pending flags"
        assert database.recover_pre_fulfillment_yookassa_on_startup() == 0
        for payment_id, before in excluded:
            assert row(payment_id) == before, f"Unsafe recovery of {payment_id}"

        # The excluded target-bearing rows belong to regular recovery's scope;
        # remove these fixtures before checking the separate no-target rule.
        with real_connect(db_path) as conn:
            conn.executemany("DELETE FROM transactions WHERE payment_id = ?",
                             [(payment_id,) for payment_id, _before in excluded])

        mtg_metadata = early_metadata("eligible-mtg")
        mtg_metadata.update(service_type="mtg", accounting_applied=False)
        mtg_metadata.pop("fulfillment_status_message_sent_at")
        put("eligible-mtg", mtg_metadata)
        assert database.recover_pre_fulfillment_yookassa_on_startup() == 1
        assert row("eligible-mtg")["status"] == "pending"
        assert json.loads(row("eligible-mtg")["metadata"])["accounting_applied"] is False

        reservation = database.reserve_pending_transaction(
            eligible, payment_method="YooKassa", preserve_fulfillment_state=True
        )
        assert reservation["processing_instance_id"] == current
        # Age alone must not recover a current-process pre-issuance operation.
        reservation["processing_started_at"] = old_timestamp
        assert database.update_reserved_transaction_metadata(eligible, reservation)
        assert database.recover_stale_processing_transactions(15) == 0
        assert row(eligible)["status"] == "processing"

        class CursorProxy:
            def __init__(self, cursor, concurrent_write):
                self.cursor = cursor
                self.concurrent_write = concurrent_write
                self.selected = False

            def execute(self, sql, parameters=()):
                self.selected = sql.lstrip().upper().startswith("SELECT") and "transactions" in sql
                self.cursor.execute(sql, parameters)
                return self

            def fetchall(self):
                result = self.cursor.fetchall()
                if self.selected:
                    self.concurrent_write()
                    self.selected = False
                return result

            def __getattr__(self, name):
                return getattr(self.cursor, name)

        class ConnectionProxy:
            def __init__(self, connection, concurrent_write):
                object.__setattr__(self, "connection", connection)
                object.__setattr__(self, "concurrent_write", concurrent_write)

            def cursor(self):
                return CursorProxy(self.connection.cursor(), self.concurrent_write)

            def __enter__(self):
                self.connection.__enter__()
                return self

            def __exit__(self, *args):
                return self.connection.__exit__(*args)

            def __setattr__(self, name, value):
                setattr(self.connection, name, value)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        # Deterministically insert a real independent connection's write between
        # SELECT and UPDATE: stale readers must not overwrite the newer state.
        for recovery_name in (
            "recover_pre_fulfillment_yookassa_on_startup",
            "recover_stale_processing_transactions",
        ):
            for change in ("target", "paid", "owner"):
                with real_connect(db_path) as conn:
                    conn.execute("DELETE FROM transactions")
                payment_id = f"race-{recovery_name}-{change}"
                metadata = early_metadata(payment_id)
                if recovery_name == "recover_stale_processing_transactions":
                    metadata["fulfillment_target_expiry_ms"] = 1893456000000
                put(payment_id, metadata)
                replacement = dict(metadata)
                new_status = "processing"
                if change == "target":
                    replacement["fulfillment_target_expiry_ms"] = 1893542400000
                elif change == "paid":
                    new_status = "paid"
                    replacement["accounting_applied"] = True
                else:
                    replacement["processing_instance_id"] = uuid.uuid4().hex
                new_raw = json.dumps(replacement)

                def concurrent_write():
                    with real_connect(db_path) as conn:
                        conn.execute(
                            "UPDATE transactions SET status = ?, metadata = ? WHERE payment_id = ?",
                            (new_status, new_raw, payment_id),
                        )

                def connect_proxy(*args, **kwargs):
                    return ConnectionProxy(real_connect(*args, **kwargs), concurrent_write)

                with patch.object(database.sqlite3, "connect", side_effect=connect_proxy):
                    assert getattr(database, recovery_name)() == 0, (
                        f"{recovery_name} overwrote concurrent {change}"
                    )
                after = row(payment_id)
                assert after["status"] == new_status and after["metadata"] == new_raw
                assert users_snapshot() == financial_before

        with real_connect(db_path) as conn:
            conn.execute("DELETE FROM transactions")
        malformed = ("{broken", "[]", "null", '"string"', "1")
        for index, raw in enumerate(malformed):
            put(f"malformed-{index}", raw)
        before_malformed = [row(f"malformed-{index}") for index in range(len(malformed))]
        assert database.recover_stale_processing_transactions(15) == 0
        assert database.recover_pre_fulfillment_yookassa_on_startup() == 0
        assert [row(f"malformed-{index}") for index in range(len(malformed))] == before_malformed
        assert users_snapshot() == financial_before

        print("Early payment recovery: OK (fail-closed eligibility, invariants and 6 CAS races)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Exercise payment-attempt fencing and user locks using only a temporary DB."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="payment-attempt-check-") as temporary:
        db_path = Path(temporary) / "check.db"
        os.environ["DB_PATH"] = str(db_path)
        sys.path.insert(0, str(ROOT / "src"))
        from shop_bot.data_manager import database

        assert database.DB_FILE.resolve() == db_path.resolve()
        with sqlite3.connect(db_path) as conn:
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
                INSERT INTO users VALUES (101,'fake-buyer',100,1,0,0,0);
                INSERT INTO users VALUES (102,'fake-referrer',0,0,20,30,0);
                """
            )

        def transaction(payment_id):
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                return dict(conn.execute(
                    "SELECT * FROM transactions WHERE payment_id = ?", (payment_id,)
                ).fetchone())

        def finances():
            with sqlite3.connect(db_path) as conn:
                return conn.execute(
                    "SELECT total_spent,total_months,referral_balance,referral_balance_all "
                    "FROM users ORDER BY telegram_id"
                ).fetchall()

        def accounting(payment_id, metadata):
            return database.apply_payment_accounting_once(
                payment_id, 101, 250, 1, 102, 25, metadata
            )

        def create(payment_id):
            invoice = {"months": 1, "plan_id": 7, "host_name": "ALL"}
            assert database.create_pending_transaction(payment_id, 101, 250, invoice)
            return invoice

        def move_to_pending(payment_id):
            # Simulate a recovery reclaiming a processing transaction. The old
            # worker still retains its original metadata in memory.
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE transactions SET status='pending' WHERE payment_id=?",
                             (payment_id,))

        def valid_token(metadata):
            token = metadata["processing_attempt_id"]
            assert isinstance(token, str) and uuid.UUID(hex=token).hex == token
            return token

        payment_id = "attempt-replaced"
        invoice = create(payment_id)
        invoice_before = copy.deepcopy(invoice)
        attempt_a = database.reserve_pending_transaction(payment_id, invoice)
        assert invoice == invoice_before, "Reservation mutated caller metadata"
        token_a = valid_token(attempt_a)
        assert database.is_reserved_transaction_owner(payment_id, attempt_a)
        move_to_pending(payment_id)
        assert not database.is_reserved_transaction_owner(payment_id, attempt_a)
        original_a = copy.deepcopy(attempt_a)
        attempt_b = database.reserve_pending_transaction(payment_id, attempt_a)
        assert attempt_a == original_a, "A retry changed the old worker's token"
        token_b = valid_token(attempt_b)
        assert token_b != token_a
        assert database.is_reserved_transaction_owner(payment_id, attempt_b)
        assert not database.is_reserved_transaction_owner(payment_id, attempt_a)

        before_row, before_money = transaction(payment_id), finances()
        for supplied in (
            attempt_a,
            {**attempt_a, "accounting_applied": True},
            {},
            {"processing_attempt_id": None},
            {"processing_attempt_id": True},
            {"processing_attempt_id": 123},
            {"processing_attempt_id": "invalid"},
            {"processing_attempt_id": "g" * 32},
            {"processing_attempt_id": "f" * 31},
        ):
            supplied_before = copy.deepcopy(supplied)
            assert not database.is_reserved_transaction_owner(payment_id, supplied)
            assert not database.update_reserved_transaction_metadata(payment_id, supplied)
            assert accounting(payment_id, supplied) is None
            assert not database.finalize_reserved_transaction(
                payment_id, success=True, metadata=supplied
            )
            assert supplied == supplied_before, "A rejected call learned/changed current state"
            assert transaction(payment_id) == before_row
            assert finances() == before_money
        for supplied_token in (None, token_a, "invalid"):
            assert not database.finalize_reserved_transaction(
                payment_id, success=False, expected_attempt_id=supplied_token
            )
            assert transaction(payment_id) == before_row
        assert not database.finalize_reserved_transaction(
            payment_id, success=True, metadata=attempt_a, expected_attempt_id=token_b
        ), "An explicit token must not disguise conflicting metadata ownership"
        assert transaction(payment_id) == before_row

        # Only B may accrue revenue. A simultaneous second B call must observe
        # the committed accounting marker instead of applying money again.
        barrier = threading.Barrier(2)

        def account_b(_index):
            barrier.wait(timeout=5)
            return accounting(payment_id, dict(attempt_b))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(account_b, range(2)))
        assert sum(result is True for result in results) == 1
        assert sum(result is False for result in results) == 1
        expected_finances = [(350.0, 2, 0.0, 0.0), (0.0, 0, 45.0, 55.0)]
        assert finances() == expected_finances
        assert accounting(payment_id, dict(attempt_a)) is None
        assert finances() == expected_finances
        assert not database.finalize_reserved_transaction(
            payment_id, success=True, expected_attempt_id=token_a
        )

        # A valid owner's stale local copy cannot undo committed accounting.
        checkpoint = {**attempt_b, "accounting_applied": False,
                      "fulfillment_target_expiry_ms": 1893456000000,
                      "fulfillment_user_notified": True}
        assert database.update_reserved_transaction_metadata(payment_id, checkpoint)
        stored = json.loads(transaction(payment_id)["metadata"])
        assert stored["accounting_applied"] is True
        assert stored["processing_attempt_id"] == token_b
        assert database.finalize_reserved_transaction(
            payment_id, success=True, metadata=attempt_b
        )
        stored = json.loads(transaction(payment_id)["metadata"])
        assert stored["accounting_applied"] is True
        assert stored["fulfillment_target_expiry_ms"] == 1893456000000
        assert stored["fulfillment_user_notified"] is True
        assert transaction(payment_id)["status"] == "paid"
        assert not database.is_reserved_transaction_owner(payment_id, attempt_b)
        assert finances() == expected_finances

        # Explicit tokens permit metadata-free finalization without looking up
        # and silently borrowing a newer attempt's identity.
        create("explicit-finalize")
        explicit = database.reserve_pending_transaction("explicit-finalize")
        assert database.finalize_reserved_transaction(
            "explicit-finalize", success=False,
            expected_attempt_id=valid_token(explicit)
        )
        assert transaction("explicit-finalize")["status"] == "pending"

        create("default-replacement")
        original_default = database.reserve_pending_transaction("default-replacement")
        original_default["fulfillment_target_expiry_ms"] = 1893456000000
        assert database.update_reserved_transaction_metadata("default-replacement", original_default)
        assert database.finalize_reserved_transaction(
            "default-replacement", success=False, metadata=original_default
        )
        incoming = {"custom_invoice_field": 7, "processing_attempt_id": "provider-input"}
        incoming_before = copy.deepcopy(incoming)
        replaced = database.reserve_pending_transaction("default-replacement", incoming)
        assert incoming == incoming_before
        assert replaced["custom_invoice_field"] == 7
        assert "fulfillment_target_expiry_ms" not in replaced
        assert valid_token(replaced) != valid_token(original_default)

        # Compatibility is limited to rows with NO attempt key, not rows with
        # a present but malformed key or a matching malformed supplied value.
        for suffix, stored_metadata in (
            ("legacy", {}), ("null", {"processing_attempt_id": None}),
            ("invalid", {"processing_attempt_id": "invalid"}),
        ):
            name = f"compat-{suffix}"
            create(name)
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE transactions SET status='processing',metadata=? WHERE payment_id=?",
                             (json.dumps(stored_metadata), name))
            if suffix == "legacy":
                assert database.is_reserved_transaction_owner(name, {})
                assert database.update_reserved_transaction_metadata(name, {})
                assert accounting(name, {}) is True
                assert accounting(name, {}) is False
                assert database.finalize_reserved_transaction(name, success=True)
                assert json.loads(transaction(name)["metadata"])["accounting_applied"] is True
            else:
                before = transaction(name)
                before_money = finances()
                for supplied in ({}, stored_metadata):
                    assert not database.is_reserved_transaction_owner(name, supplied)
                    assert not database.update_reserved_transaction_metadata(name, supplied)
                    assert accounting(name, supplied) is None
                    assert not database.finalize_reserved_transaction(name, success=True, metadata=supplied)
                assert transaction(name) == before
                assert finances() == before_money

        # Transaction ownership and the user flag have distinct lifetimes.
        # An old finally-block can release its own flag after reclaim, but
        # cannot release a subsequently acquired B flag.
        assert database.set_pending_payment(101, True, owner=token_a)
        assert not database.set_pending_payment(101, True, owner=token_b)
        assert not database.set_pending_payment(101, False)
        assert database.get_pending_payment_status(101)
        assert not database.set_pending_payment(101, False, owner=token_b)
        assert database.set_pending_payment(101, False, owner=token_a)
        assert database.set_pending_payment(101, True, owner=token_b)
        assert not database.set_pending_payment(101, False, owner=token_a)
        assert not database.set_pending_payment(101, False)
        assert database.get_pending_payment_status(101)
        assert database.set_pending_payment(101, False, owner=token_b)
        assert database.set_pending_payment(101, True)
        assert database.set_pending_payment(101, False)
        assert database.set_pending_payment(101, True, owner=token_a)
        assert database.clear_all_pending_payments() == 1
        assert not database.get_pending_payment_status(101)
        assert database.set_pending_payment(101, True)
        assert database.set_pending_payment(101, False), "Startup left an old local owner behind"

        # Competing recovery retries must have exactly one new owner.
        create("competing")
        former = database.reserve_pending_transaction("competing")
        move_to_pending("competing")
        former_before = copy.deepcopy(former)
        barrier = threading.Barrier(4)

        def reserve_again(_index):
            barrier.wait(timeout=5)
            return database.reserve_pending_transaction(
                "competing", former, preserve_fulfillment_state=True
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            attempts = list(pool.map(reserve_again, range(4)))
        winners = [result for result in attempts if result is not None]
        assert len(winners) == 1
        assert valid_token(winners[0]) != valid_token(former)
        assert former == former_before
        assert database.is_reserved_transaction_owner("competing", winners[0])
        assert not database.is_reserved_transaction_owner("competing", former)

        print("Payment attempt ownership: OK (stale writes, accounting, finalization, flags and concurrency)")


if __name__ == "__main__":
    main()

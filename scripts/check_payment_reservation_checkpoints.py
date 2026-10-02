#!/usr/bin/env python3
"""Check fresh payment checkpoints, concurrent reservation and accounting retries."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    with tempfile.TemporaryDirectory(prefix="payment-reservation-check-") as tmp:
        os.environ["DB_PATH"] = str(Path(tmp) / "test.db")
        from shop_bot.data_manager import database

        database.initialize_db()
        database.register_user_if_not_exists(1, "test-buyer", 2)
        database.register_user_if_not_exists(2, "test-referrer", None)

        invoice = {
            "user_id": 1,
            "price": 100.0,
            "months": 1,
            "host_name": "ALL",
            "payment_method": "YooKassa",
        }
        checkpoints = {
            "fulfillment_target_expiry_ms": 2_000_000_000_000,
            "fulfillment_key_email": "stable-test-client",
            "fulfillment_status_message_sent_at": "test-notification-checkpoint",
            "fulfillment_target_corrected_at": "test-correction-checkpoint",
            "recovered_from_interrupted_processing_at": "test-recovery-checkpoint",
        }

        def read_transaction(payment_id: str) -> tuple[str, dict]:
            with sqlite3.connect(database.DB_FILE) as conn:
                status, raw = conn.execute(
                    "SELECT status, metadata FROM transactions WHERE payment_id=?",
                    (payment_id,),
                ).fetchone()
            return status, json.loads(raw)

        def accounting_snapshot() -> list[tuple]:
            with sqlite3.connect(database.DB_FILE) as conn:
                return conn.execute(
                    "SELECT total_spent, total_months, referral_balance, "
                    "referral_balance_all FROM users ORDER BY telegram_id"
                ).fetchall()

        payment_id = "checkpoint-retry"
        assert database.create_pending_transaction(payment_id, 1, 100.0, invoice)
        stale_snapshot = dict(invoice)
        first = database.reserve_pending_transaction(payment_id)
        assert first is not None
        first.update(checkpoints)
        assert database.update_reserved_transaction_metadata(payment_id, first)
        assert database.apply_payment_accounting_once(
            payment_id, 1, 100.0, 1, 2, 10.0, first
        )
        assert database.finalize_reserved_transaction(
            payment_id, success=False, metadata=first
        )
        before = accounting_snapshot()
        assert before == [(100.0, 1, 0.0, 0.0), (0.0, 0, 10.0, 10.0)]

        # A scheduler snapshot predates the first attempt's completed writes.
        # Conflicting remote/local stale values must not replace fresh checkpoints.
        incoming = {
            **stale_snapshot,
            **{key: "stale-value" for key in checkpoints},
            "accounting_applied": False,
            "processing_started_at": "stale-start",
            "provider_payment_id": payment_id,
        }
        original_incoming = dict(incoming)
        retry = database.reserve_pending_transaction(
            payment_id, incoming, preserve_fulfillment_state=True
        )
        assert retry is not None
        assert incoming == original_incoming, "opt-in reservation must not mutate input"
        for key, value in checkpoints.items():
            assert retry[key] == value, f"fresh checkpoint lost: {key}"
        assert retry["accounting_applied"] is True
        assert retry["processing_started_at"] != "stale-start"
        assert retry["provider_payment_id"] == payment_id
        assert retry["price"] == invoice["price"]
        assert not database.apply_payment_accounting_once(
            payment_id, 1, 100.0, 1, 2, 10.0, retry
        )
        assert accounting_snapshot() == before, "retry duplicated accounting"
        assert database.finalize_reserved_transaction(
            payment_id, success=True, metadata=retry
        )
        assert read_transaction(payment_id)[0] == "paid"
        assert database.reserve_pending_transaction(
            payment_id, incoming, preserve_fulfillment_state=True
        ) is None
        assert database.reserve_pending_transaction(
            "unknown", incoming, preserve_fulfillment_state=True
        ) is None

        # Concurrent webhook/scheduler reservations have exactly one winner.
        race_id = "concurrent-reservation"
        assert database.create_pending_transaction(
            race_id, 1, 100.0, {**invoice, **checkpoints}
        )
        barrier = threading.Barrier(2)

        def reserve_concurrently():
            barrier.wait(timeout=5)
            return database.reserve_pending_transaction(
                race_id, dict(invoice), preserve_fulfillment_state=True
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve_concurrently(), range(2)))
        winners = [result for result in results if result is not None]
        assert len(winners) == 1, "reservation must have exactly one owner"
        assert winners[0]["fulfillment_target_expiry_ms"] == checkpoints[
            "fulfillment_target_expiry_ms"
        ]
        assert read_transaction(race_id)[0] == "processing"

        # Other providers retain the existing explicit replacement behavior.
        for method in ("Telegram Stars", "CryptoBot", "P2P"):
            other_id = f"default-{method}"
            assert database.create_pending_transaction(
                other_id, 1, 100.0,
                {**invoice, **checkpoints, "payment_method": method},
            )
            replacement = {"user_id": 1, "payment_method": method}
            reserved = database.reserve_pending_transaction(other_id, replacement)
            assert reserved is not None
            assert "fulfillment_target_expiry_ms" not in reserved
            assert reserved["payment_method"] == method

        # Malformed local state cannot silently discard durable checkpoints.
        for index, invalid_raw in enumerate(("{bad json", "[]", "null")):
            corrupt_id = f"invalid-local-{index}"
            assert database.create_pending_transaction(corrupt_id, 1, 100.0, invoice)
            with sqlite3.connect(database.DB_FILE) as conn:
                conn.execute(
                    "UPDATE transactions SET metadata=? WHERE payment_id=?",
                    (invalid_raw, corrupt_id),
                )
            assert database.reserve_pending_transaction(
                corrupt_id, dict(invoice), preserve_fulfillment_state=True
            ) is None
            with sqlite3.connect(database.DB_FILE) as conn:
                assert conn.execute(
                    "SELECT status, metadata FROM transactions WHERE payment_id=?",
                    (corrupt_id,),
                ).fetchone() == ("pending", invalid_raw)

    print("Payment reservation checkpoint checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

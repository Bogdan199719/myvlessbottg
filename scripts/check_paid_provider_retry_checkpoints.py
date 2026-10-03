#!/usr/bin/env python3
"""Run real paid-retry scheduler/fulfillment with fake Telegram and XUI only."""

from __future__ import annotations

import asyncio
import ast
import copy
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="paid-provider-retry-") as temporary:
        os.environ["DB_PATH"] = str(Path(temporary) / "check.db")
        sys.path.insert(0, str(ROOT / "src"))
        from shop_bot.data_manager import database, scheduler
        from shop_bot.bot import handlers
        from shop_bot.utils import time_utils

        assert database.DB_FILE.resolve() == Path(os.environ["DB_PATH"]).resolve()
        if "--baseline-scheduler" in sys.argv:
            # Optional regression proof: only substitute the old retry function
            # supplied on stdin, retaining the current temp DB and fake services.
            tree = ast.parse(sys.stdin.read())
            retry = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                         and node.name == "process_pending_paid_provider_retries")
            for arg in retry.args.args:
                arg.annotation = None
            retry.returns = None
            exec(compile(ast.Module(body=[retry], type_ignores=[]), "baseline_scheduler", "exec"),
                 scheduler.__dict__)
        logging.getLogger().setLevel(logging.ERROR)
        database.initialize_db()
        database.register_user_if_not_exists(999, "fake-referrer", None)
        database.set_setting("referral_percentage", "10")
        actual_select = database.get_pending_paid_retry_transactions
        actual_process = handlers.process_successful_payment
        sequence = 0

        def transaction(payment_id):
            with sqlite3.connect(database.DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                return dict(conn.execute(
                    "SELECT * FROM transactions WHERE payment_id=?", (payment_id,)
                ).fetchone())

        def finances(user_id):
            with sqlite3.connect(database.DB_FILE) as conn:
                return conn.execute(
                    "SELECT telegram_id,total_spent,total_months,referral_balance,referral_balance_all "
                    "FROM users WHERE telegram_id IN (?,999) ORDER BY telegram_id", (user_id,)
                ).fetchall()

        async def scenario(method, mode):
            nonlocal sequence
            sequence += 1
            user_id = 100 + sequence
            payment_id = f"fake-{sequence}"
            host = f"fake-host-{sequence}"
            email = f"stable-client-{sequence}"
            database.register_user_if_not_exists(user_id, f"fake-buyer-{sequence}", 999)
            invoice = {
                "user_id": user_id, "months": 1, "price": 250,
                "action": "new", "key_id": 0, "host_name": host,
                "plan_id": 7, "service_type": "xui", "plan_name": "Fake plan",
                "payment_method": method, "provider_payment_id": payment_id,
            }
            assert database.create_pending_transaction(payment_id, user_id, 250, invoice)
            with sqlite3.connect(database.DB_FILE) as conn:
                conn.execute(
                    "UPDATE transactions SET payment_method=?,amount_currency=3,currency_name='OLD' "
                    "WHERE payment_id=?", (method, payment_id)
                )
            target = time_utils.get_timestamp_ms(time_utils.get_msk_now() + timedelta(days=12))
            checkpoint_time = "2026-01-01T00:00:00+03:00"
            old_attempt = None
            captured = None
            fresh_snapshot = None
            money_before = finances(user_id)
            panel_calls = []
            processing_message = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
            bot = SimpleNamespace(send_message=AsyncMock(return_value=processing_message),
                                  delete_message=AsyncMock())

            def select_then_change(limit):
                nonlocal old_attempt, captured, fresh_snapshot, money_before
                snapshot = [tx for tx in actual_select(limit) if tx["payment_id"] == payment_id]
                captured = copy.deepcopy(snapshot)
                if mode in {"accounted", "partial"}:
                    assert len(snapshot) == 1
                    old_attempt = database.reserve_pending_transaction(
                        payment_id, preserve_fulfillment_state=True
                    )
                    old_attempt.update(
                        fulfillment_target_expiry_ms=target,
                        fulfillment_key_email=email,
                        fulfillment_status_message_sent_at=checkpoint_time,
                        accounting_applied=False,
                    )
                    assert database.update_reserved_transaction_metadata(payment_id, old_attempt)
                    assert database.add_new_key(
                        user_id, host, f"fake-uuid-{sequence}", email, target,
                        "vless://fake-client@fake.invalid:443", 7
                    )
                    if mode == "accounted":
                        assert database.apply_payment_accounting_once(
                            payment_id, user_id, 250, 1, 999, 25, old_attempt
                        ) is True
                    assert database.finalize_reserved_transaction(
                        payment_id, success=False, metadata=old_attempt,
                        # Distinct current column sentinels prove that stale queue
                        # columns must never be written back during local retry.
                        payment_method=f"current-{method}", amount_currency=77,
                        currency_name="CURRENT",
                    )
                elif mode in {"bad-json", "non-object", "became-paid", "became-processing"}:
                    assert len(snapshot) == 1
                    with sqlite3.connect(database.DB_FILE) as conn:
                        if mode in {"bad-json", "non-object"}:
                            conn.execute("UPDATE transactions SET metadata=? WHERE payment_id=?",
                                         ("{broken" if mode == "bad-json" else "[]", payment_id))
                        else:
                            conn.execute("UPDATE transactions SET status=? WHERE payment_id=?",
                                         ("paid" if mode == "became-paid" else "processing", payment_id))
                fresh_snapshot = transaction(payment_id)
                money_before = finances(user_id)
                return snapshot

            async def absolute_xui(**kwargs):
                panel_calls.append(dict(kwargs))
                if old_attempt is not None:
                    current = transaction(payment_id)
                    assert current["status"] == "processing"
                    current_metadata = json.loads(current["metadata"])
                    assert current_metadata["processing_attempt_id"] != old_attempt["processing_attempt_id"]
                    assert not database.update_reserved_transaction_metadata(payment_id, old_attempt)
                    assert not database.finalize_reserved_transaction(payment_id, success=False, metadata=old_attempt)
                    assert transaction(payment_id) == current, "Old worker changed the active retry"
                return {
                    "host_name": kwargs["host_name"], "email": kwargs["email"],
                    "client_uuid": f"fake-uuid-{sequence}",
                    "expiry_timestamp_ms": kwargs["target_expiry_ms"],
                    "connection_string": "vless://fake-client@fake.invalid:443",
                }

            process = AsyncMock(wraps=actual_process)
            with (
                patch.object(database, "get_pending_paid_retry_transactions", side_effect=select_then_change),
                patch.object(handlers, "process_successful_payment", process),
                patch.object(handlers.xui_api, "create_or_update_key_on_host_absolute_expiry", side_effect=absolute_xui),
                patch.object(handlers.xui_api, "create_or_update_key_on_host", AsyncMock(side_effect=AssertionError("relative issuance forbidden"))),
                patch.object(handlers, "notify_admin_of_purchase", AsyncMock()),
            ):
                await scheduler.process_pending_paid_provider_retries(bot)

            after = transaction(payment_id)
            assert captured is not None and fresh_snapshot is not None
            if mode in {"bad-json", "non-object", "became-paid", "became-processing", "p2p"}:
                process.assert_not_awaited()
                assert not panel_calls
                assert not bot.send_message.await_count
                assert after == fresh_snapshot
                assert finances(user_id) == money_before
                if mode == "p2p":
                    assert not captured, "Manual P2P must stay out of automatic paid retries"
                return

            process.assert_awaited_once()
            assert after["status"] == "paid", (method, mode, after["status"])
            stored = json.loads(after["metadata"])
            assert stored["accounting_applied"] is True
            assert len(panel_calls) == 1
            if old_attempt is not None:
                assert panel_calls[0]["target_expiry_ms"] == target, (
                    "Paid retry replaced the fresh absolute expiry with a stale queue snapshot"
                )
            keys = database.get_user_keys(user_id)
            assert len(keys) == 1, "Retry created duplicate local client keys"
            current_attempt = process.await_args.args[1]
            assert database.get_user(user_id)["total_spent"] == 250
            assert database.get_user(user_id)["total_months"] == 1
            assert not database.get_pending_payment_status(user_id)
            messages = [call.kwargs.get("text", call.args[1] if len(call.args) > 1 else "")
                        for call in bot.send_message.await_args_list]
            processing_count = sum("Оплата получена!" in text for text in messages)
            referral_count = sum("Ваш реферал" in text for text in messages)
            assert processing_count == (1 if mode == "ordinary" else 0)
            assert referral_count == (0 if mode == "accounted" else 1)
            assert sum("Ключ" in text or "продл" in text.lower() or "актив" in text.lower()
                       for text in messages) >= 1, "Customer received no successful issuance response"

            if old_attempt is not None:
                assert panel_calls[0]["email"] == email
                assert stored["fulfillment_target_expiry_ms"] == target
                assert stored["fulfillment_key_email"] == email
                assert stored["fulfillment_status_message_sent_at"] == checkpoint_time
                assert stored["processing_attempt_id"] != old_attempt["processing_attempt_id"]
                assert current_attempt["processing_attempt_id"] == stored["processing_attempt_id"]
                for column in ("payment_method", "amount_currency", "currency_name"):
                    assert after[column] == fresh_snapshot[column], f"Stale queue overwrote {column}"
                assert not database.update_reserved_transaction_metadata(payment_id, old_attempt)
                assert not database.finalize_reserved_transaction(payment_id, success=False, metadata=old_attempt)
                if mode == "accounted":
                    assert finances(user_id) == money_before
                else:
                    after_money = finances(user_id)
                    assert after_money[0][1:3] == (250, 1)
                    assert after_money[1][3] - money_before[1][3] == 25
                    assert after_money[1][4] - money_before[1][4] == 25
            else:
                assert panel_calls[0]["target_expiry_ms"] == stored["fulfillment_target_expiry_ms"]

        async def checks():
            for method in ("Telegram Stars", "CryptoBot"):
                for mode in ("accounted", "partial", "ordinary", "bad-json", "non-object",
                             "became-paid", "became-processing"):
                    await scenario(method, mode)
            await scenario("P2P", "p2p")

        asyncio.run(checks())
        print("Paid provider retry checkpoints: OK (15 real scheduler/fulfillment scenarios)")


if __name__ == "__main__":
    main()

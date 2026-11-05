#!/usr/bin/env python3
"""Exercise YooKassa's real Flask route and fulfillment against disposable data.

No production database or provider is used. All external effects are replaced.
Run in a fresh Python process; DB_PATH must be set before application imports.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    with tempfile.TemporaryDirectory(prefix="yookassa-retry-") as tmp:
        os.environ["DB_PATH"] = str(Path(tmp) / "test.db")
        os.environ["FLASK_SECRET_KEY"] = "synthetic-test-secret-" + "x" * 40
        # Fail closed if an unexpected integration tries a network connection.
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden")):
            from shop_bot.data_manager import database as db
            from shop_bot.bot import handlers
            from shop_bot.webhook_server import app as web

            assert Path(db.DB_FILE) == Path(tmp) / "test.db"
            db.initialize_db()
            for key, value in {
                "yookassa_shop_id": "synthetic-shop",
                "yookassa_secret_key": "synthetic-secret",
                "referral_percentage": "10",
                "enable_admin_payment_notifications": "false",
                "domain": "vpn.example.invalid",
            }.items():
                db.update_setting(key, value)
            db.register_user_if_not_exists(1, "synthetic-buyer", None)
            db.register_user_if_not_exists(2, "synthetic-referrer", None)
            with sqlite3.connect(db.DB_FILE) as conn:
                conn.execute("UPDATE users SET referred_by=2 WHERE telegram_id=1")
            for host in ("alpha", "beta"):
                db.create_host(host, f"https://{host}.example.invalid", "test", "test", 1)
            db.create_plan("ALL", "Test", 1, 100)
            plan_id = db.get_plans_for_host("ALL")[0]["plan_id"]

            class Message:
                async def delete(self, **kwargs):
                    pass

                async def edit_text(self, *args, **kwargs):
                    pass

            class Bot:
                async def send_message(self, *args, **kwargs):
                    return Message()

                async def delete_message(self, **kwargs):
                    pass

            bot = Bot()
            controller = SimpleNamespace(get_bot_instance=lambda: bot)
            app = web.create_webhook_app(controller)
            app.config["TESTING"] = True
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run_loop():
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            thread = threading.Thread(target=run_loop, daemon=True)
            thread.start()
            assert ready.wait(5)
            app.config["EVENT_LOOP"] = loop
            client = app.test_client()
            providers = {}
            panel_calls = []
            fail_beta = False

            async def absolute_key(**kwargs):
                panel_calls.append(dict(kwargs))
                if fail_beta and kwargs["host_name"] == "beta":
                    return None
                return {
                    "host_name": kwargs["host_name"],
                    "email": kwargs["email"],
                    "client_uuid": "synthetic-" + kwargs["host_name"],
                    "expiry_timestamp_ms": kwargs["target_expiry_ms"],
                    "connection_string": "vless://synthetic@example.invalid:443",
                }

            def metadata():
                return dict(user_id=1, months=1, price=100, action="new", key_id=0,
                            host_name="ALL", plan_id=plan_id, plan_name="Test", service_type="xui")

            def order(payment_id, *, currency="RUB", amount="100.00", method=None):
                snapshot = metadata()
                if method:
                    snapshot["payment_method"] = method
                assert db.create_pending_transaction(payment_id, 1, 100, snapshot)
                providers[payment_id] = SimpleNamespace(status="succeeded", metadata=snapshot,
                    amount=SimpleNamespace(value=amount, currency=currency))

            def post(payment_id):
                return client.post("/yookassa-webhook", json={"event": "payment.succeeded", "object": {"id": payment_id}})

            def state(payment_id):
                with sqlite3.connect(db.DB_FILE) as conn:
                    row = conn.execute("SELECT status, metadata FROM transactions WHERE payment_id=?", (payment_id,)).fetchone()
                return row[0], json.loads(row[1])

            def money():
                with sqlite3.connect(db.DB_FILE) as conn:
                    buyer = conn.execute("SELECT total_spent, total_months FROM users WHERE telegram_id=1").fetchone()
                    referrer = conn.execute("SELECT referral_balance FROM users WHERE telegram_id=2").fetchone()
                return (*buyer, *referrer)

            try:
                with patch.object(web.Payment, "find_one", side_effect=lambda pid: providers[pid]) as provider, patch.object(handlers.xui_api, "create_or_update_key_on_host_absolute_expiry", side_effect=absolute_key), patch.object(handlers.xui_api, "create_or_update_key_on_host", side_effect=AssertionError("Relative issuance forbidden")):
                    assert client.post("/yookassa-webhook", data="invalid", content_type="application/json").status_code == 400
                    assert post("unknown").status_code == 200
                    assert provider.call_count == 0 and not panel_calls

                    order("stopped")
                    controller.get_bot_instance = lambda: None
                    assert post("stopped").status_code == 503
                    assert state("stopped")[0] == "pending" and not panel_calls
                    assert not web._is_webhook_processed("yookassa", "stopped")
                    controller.get_bot_instance = lambda: bot
                    assert post("stopped").status_code == 200
                    assert state("stopped")[0] == "paid"
                    assert money() == (100, 1, 10)
                    calls = len(panel_calls)
                    assert post("stopped").status_code == 200 and len(panel_calls) == calls

                    order("no-processor")
                    with patch.object(handlers, "process_successful_payment", None):
                        assert post("no-processor").status_code == 503
                    assert state("no-processor")[0] == "pending"

                    order("release-fails")
                    controller.get_bot_instance = lambda: None
                    with patch.object(web, "finalize_reserved_transaction", return_value=False):
                        assert post("release-fails").status_code == 503
                    assert state("release-fails")[0] == "processing"
                    calls = len(panel_calls)
                    assert post("release-fails").status_code == 200 and len(panel_calls) == calls

                    order("release-exception")
                    with patch.object(web, "finalize_reserved_transaction", side_effect=[RuntimeError("synthetic failure"), True]):
                        assert post("release-exception").status_code == 500
                    assert not web._is_webhook_processed("yookassa", "release-exception")
                    controller.get_bot_instance = lambda: bot

                    order("reserve-fails")
                    with patch.object(web, "reserve_pending_transaction", return_value=None):
                        assert post("reserve-fails").status_code == 503
                    assert state("reserve-fails")[0] == "pending"
                    with patch.object(web, "reserve_pending_transaction", side_effect=RuntimeError("synthetic failure")):
                        assert post("reserve-fails").status_code == 500
                    assert state("reserve-fails")[0] == "pending"

                    order("no-loop")
                    app.config["EVENT_LOOP"] = None
                    assert post("no-loop").status_code == 503
                    assert state("no-loop")[0] == "pending"
                    app.config["EVENT_LOOP"] = loop

                    for pid, currency, amount in (("wrong-currency", "USD", "100.00"), ("wrong-sum", "RUB", "99.00")):
                        order(pid, currency=currency, amount=amount)
                        assert post(pid).status_code == 503
                        assert state(pid)[0] == "pending"

                    order("partial")
                    fail_beta = True
                    start = len(panel_calls)
                    assert post("partial").status_code == 503
                    status, checkpoint = state("partial")
                    assert status == "pending"
                    target = checkpoint["fulfillment_target_expiry_ms"]
                    assert checkpoint["fulfillment_status_message_sent_at"]
                    assert money() == (100, 1, 10)
                    fail_beta = False
                    assert post("partial").status_code == 200
                    assert state("partial")[0] == "paid"
                    assert state("partial")[1]["fulfillment_target_expiry_ms"] == target
                    assert all(call["target_expiry_ms"] == target for call in panel_calls[start:])
                    assert len(panel_calls[start:]) == 4
                    assert money() == (200, 2, 20)
                    calls = len(panel_calls)
                    assert post("partial").status_code == 200
                    assert len(panel_calls) == calls and money() == (200, 2, 20)
                    assert web._is_webhook_processed("yookassa", "partial")
                    assert len(db.get_user_keys(1)) == 2

                    # Force a retry after real accounting has already succeeded.
                    # This catches lost accounting checkpoints in fresh provider metadata.
                    order("accounting-retry")
                    real_finalize = web.finalize_reserved_transaction

                    def defer_completion(payment_id, **kwargs):
                        kwargs["success"] = False
                        assert real_finalize(payment_id, **kwargs)
                        return False

                    with patch.object(web, "finalize_reserved_transaction", side_effect=defer_completion):
                        assert post("accounting-retry").status_code == 503
                    assert state("accounting-retry")[0] == "pending"
                    accounted = state("accounting-retry")[1]
                    assert accounted["accounting_applied"] is True
                    assert money() == (300, 3, 30)
                    assert post("accounting-retry").status_code == 200
                    assert state("accounting-retry")[1]["fulfillment_target_expiry_ms"] == accounted["fulfillment_target_expiry_ms"]
                    assert money() == (300, 3, 30)

                    # Cancel the real fulfillment before it saves a target,
                    # then simulate a restart without contacting any provider.
                    order("early-crash")
                    reserved = db.reserve_pending_transaction(
                        "early-crash",
                        {**metadata(), "provider_payment_id": "early-crash",
                         "payment_method": "YooKassa"},
                        payment_method="YooKassa", preserve_fulfillment_state=True,
                    )
                    assert reserved is not None
                    before_crash = len(panel_calls)

                    async def cancel_before_target():
                        try:
                            await handlers.process_successful_payment(bot, reserved)
                        except asyncio.CancelledError:
                            return
                        raise AssertionError("Synthetic interruption was not reached")

                    with patch.object(bot, "send_message", side_effect=asyncio.CancelledError):
                        asyncio.run_coroutine_threadsafe(
                            cancel_before_target(), loop
                        ).result(timeout=5)
                    status, interrupted = state("early-crash")
                    assert status == "processing"
                    assert "fulfillment_target_expiry_ms" not in interrupted
                    assert interrupted["fulfillment_status_message_sent_at"]
                    assert len(panel_calls) == before_crash
                    assert money() == (300, 3, 30)
                    assert db.get_pending_payment_status(1)
                    assert db.recover_pre_fulfillment_yookassa_on_startup() == 0

                    with patch.object(db, "_PAYMENT_PROCESS_INSTANCE_ID", "f" * 32):
                        # initialize_db already clears abandoned user flags at
                        # actual startup, before this new recovery is called.
                        db.clear_all_pending_payments()
                        assert db.recover_pre_fulfillment_yookassa_on_startup() >= 1
                        assert state("early-crash")[0] == "pending"
                        assert post("early-crash").status_code == 200
                        completed = state("early-crash")[1]
                        assert completed["processing_instance_id"] == "f" * 32
                        assert completed["accounting_applied"] is True
                        assert money() == (400, 4, 40)
                        assert len(panel_calls) == before_crash + 2
                        assert post("early-crash").status_code == 200
                        assert money() == (400, 4, 40)
                        assert len(panel_calls) == before_crash + 2

                    # An old route's exception must not release a newer owner.
                    for method in ("YooKassa", "CryptoBot"):
                        pid = f"stale-exception-{method}"
                        order(pid, method=method)
                        replacement = {}
                        before_panels, before_money = len(panel_calls), money()

                        async def fail_after_takeover(_bot, old):
                            assert db.finalize_reserved_transaction(
                                pid, success=False, metadata=old
                            )
                            newer = db.reserve_pending_transaction(
                                pid, {**metadata(), "provider_payment_id": pid,
                                      "payment_method": method},
                                payment_method=method, preserve_fulfillment_state=True,
                            )
                            assert newer is not None
                            assert newer["processing_attempt_id"] != old["processing_attempt_id"]
                            newer.update(fulfillment_target_expiry_ms=2_000_000_000_000,
                                         accounting_applied=True)
                            assert db.update_reserved_transaction_metadata(pid, newer)
                            replacement.update(newer)
                            raise RuntimeError("old attempt failed after takeover")

                        with patch.object(handlers, "process_successful_payment", side_effect=fail_after_takeover):
                            if method == "YooKassa":
                                response = post(pid)
                            else:
                                db.update_setting("cryptobot_token", "synthetic-token")
                                with patch.object(web, "_is_valid_cryptobot_signature", return_value=True):
                                    response = client.post("/cryptobot-webhook", json={
                                        "update_type": "invoice_paid", "payload": {
                                            "invoice_id": "synthetic-invoice", "amount": "100.00",
                                            "currency": "RUB", "currency_type": "fiat",
                                            "payload": json.dumps({"tx_id": pid}),
                                        },
                                    })
                        assert response.status_code == 500
                        assert state(pid) == ("processing", replacement)
                        assert len(panel_calls) == before_panels and money() == before_money

                    # An in-flight old panel reply cannot persist keys/accounting
                    # or clear the per-user flag acquired by the newer attempt.
                    order("blocked-old-panel")

                    async def blocked_panel_takeover():
                        pid = "blocked-old-panel"
                        old = db.reserve_pending_transaction(
                            pid, {**metadata(), "provider_payment_id": pid,
                                  "payment_method": "YooKassa"},
                            payment_method="YooKassa", preserve_fulfillment_state=True,
                        )
                        assert old is not None
                        entered, proceed = asyncio.Event(), asyncio.Event()

                        async def blocked_key(**kwargs):
                            entered.set()
                            await proceed.wait()
                            return {
                                "host_name": kwargs["host_name"], "email": kwargs["email"],
                                "client_uuid": "obsolete-client",
                                "expiry_timestamp_ms": kwargs["target_expiry_ms"],
                                "connection_string": "vless://obsolete@example.invalid:443",
                            }

                        keys_before, money_before = db.get_user_keys(1), money()
                        with patch.object(handlers.xui_api, "create_or_update_key_on_host_absolute_expiry", side_effect=blocked_key):
                            task = asyncio.create_task(handlers.process_successful_payment(bot, old))
                            try:
                                await asyncio.wait_for(entered.wait(), timeout=5)
                                assert db.finalize_reserved_transaction(pid, success=False, metadata=old)
                                assert db.set_pending_payment(1, False, owner=old["processing_attempt_id"])
                                newer = db.reserve_pending_transaction(
                                    pid, payment_method="YooKassa", preserve_fulfillment_state=True
                                )
                                assert newer is not None
                                assert db.set_pending_payment(1, True, owner=newer["processing_attempt_id"])
                            finally:
                                proceed.set()
                            assert await asyncio.wait_for(task, timeout=5) is False
                        assert db.get_user_keys(1) == keys_before and money() == money_before
                        assert db.get_pending_payment_status(1)
                        assert db.is_reserved_transaction_owner(pid, newer)
                        assert db.set_pending_payment(1, False, owner=newer["processing_attempt_id"])
                        assert db.finalize_reserved_transaction(pid, success=False, metadata=newer)

                    asyncio.run_coroutine_threadsafe(blocked_panel_takeover(), loop).result(timeout=10)

                    # Real Stars confirmation and P2P routing retain the token
                    # required by metadata updates and ordinary finalizers.
                    assert db.create_pending_transaction(
                        "stars-owner", 1, 100, {**metadata(), "expected_stars_amount": 10}
                    )
                    stars = handlers._stars_complete_transaction("stars-owner", 10, "test-charge", 1)
                    assert stars is not None
                    assert db.is_reserved_transaction_owner("stars-owner", stars)
                    assert state("stars-owner")[1]["processing_attempt_id"] == stars["processing_attempt_id"]
                    assert db.finalize_reserved_transaction("stars-owner", success=False, metadata=stars)

                    async def fail_owned_p2p(_bot, reserved):
                        assert reserved["processing_attempt_id"]
                        assert db.is_reserved_transaction_owner("p2p-owner-routing", reserved)
                        return False

                    with patch.object(handlers, "process_successful_payment", side_effect=fail_owned_p2p):
                        assert asyncio.run_coroutine_threadsafe(
                            handlers._process_approved_p2p_request(bot, "owner-routing", metadata()), loop
                        ).result(timeout=5) is False
                    assert state("p2p-owner-routing")[0] == "pending"
                    assert money() == (400, 4, 40)
            finally:
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=5)
                assert not thread.is_alive()
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
                loop.close()
    print("YooKassa webhook retry checks passed (synthetic DB, real route and fulfillment).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

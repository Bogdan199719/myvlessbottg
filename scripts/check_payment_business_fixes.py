#!/usr/bin/env python3
"""Deterministic checks for payment snapshots, recovery, and discount claims."""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))

    handlers_source = (root / "src/shop_bot/bot/handlers.py").read_text(
        encoding="utf-8"
    )
    scheduler_source = (
        root / "src/shop_bot/data_manager/scheduler.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(handlers_source)
    scheduler_tree = ast.parse(scheduler_source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    process_source = ast.get_source_segment(
        handlers_source, functions["process_successful_payment"]
    )
    retry_source = ast.get_source_segment(
        handlers_source, functions["_is_payment_fulfillment_retry"]
    )
    mtg_source = ast.get_source_segment(
        handlers_source, functions["_create_mtg_proxy_after_payment"]
    )
    assert 'months = plan[' not in process_source
    assert 'service_type = metadata.get("service_type")' in process_source
    assert "_target_expiry_ms_for_xui_payment(" in process_source
    assert "_target_expiry_ms_for_service_payment(" in process_source
    assert 'metadata.get("fulfillment_key_email")' in process_source
    assert 'metadata["fulfillment_key_email"]' in process_source
    assert "if is_fulfillment_retry:" in process_source
    assert "Suppressing duplicate user status message" in process_source
    assert "fulfillment_status_message_sent_at" in process_source
    assert "update_reserved_transaction_metadata(" in process_source
    assert "Оплата получена, но не все серверы выданы" not in process_source
    assert "Could not remove incomplete global payment status" in process_source
    assert 'metadata.get("fulfillment_target_corrected_at")' in process_source
    assert "_merge_yookassa_payment_metadata(" in scheduler_source

    retry_namespace: dict = {}
    exec(retry_source, retry_namespace)
    is_fulfillment_retry = retry_namespace["_is_payment_fulfillment_retry"]
    assert not is_fulfillment_retry({})
    assert is_fulfillment_retry({"fulfillment_target_expiry_ms": 1})
    assert is_fulfillment_retry(
        {"fulfillment_status_message_sent_at": "2026-08-25T21:00:00+03:00"}
    )

    scheduler_functions = {
        node.name: node
        for node in scheduler_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    merge_namespace: dict = {}
    exec(
        compile(
            ast.Module(
                body=[scheduler_functions["_merge_yookassa_payment_metadata"]],
                type_ignores=[],
            ),
            "scheduler_metadata_merge_check",
            "exec",
        ),
        merge_namespace,
    )
    merge_yookassa_metadata = merge_namespace["_merge_yookassa_payment_metadata"]
    assert "apply_payment_accounting_once(" in mtg_source
    assert "get_proxy_details(" in mtg_source
    assert "refusing a relative renew" in mtg_source
    assert "panel_expiry_ms or" not in mtg_source
    assert "if not update_key_info(" in mtg_source
    assert "if not update_key_plan_id(" in mtg_source
    assert "if not used_key_id:" in mtg_source

    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["DB_PATH"] = str(Path(tmp_dir) / "test.db")
        from shop_bot.data_manager import database
        from shop_bot.utils import time_utils

        merged_metadata = merge_yookassa_metadata(
            {
                "price": "549.0",
                "fulfillment_target_expiry_ms": 1_795_000_000_000,
                "fulfillment_key_email": "stable-client",
                "accounting_applied": True,
            },
            {
                "price": "549.0",
                "host_name": "ALL",
                "fulfillment_target_expiry_ms": 9_999,
            },
        )
        assert merged_metadata["host_name"] == "ALL"
        assert merged_metadata["fulfillment_target_expiry_ms"] == 1_795_000_000_000
        assert merged_metadata["fulfillment_key_email"] == "stable-client"
        assert merged_metadata["accounting_applied"] is True

        database.initialize_db()
        database.register_user_if_not_exists(1, "buyer", None)
        database.register_user_if_not_exists(2, "mtg-only", None)
        database.register_user_if_not_exists(3, "legacy-vpn", None)

        with sqlite3.connect(database.DB_FILE) as conn:
            conn.execute(
                """
                INSERT INTO transactions
                    (payment_id, user_id, status, amount_rub, metadata, created_date)
                VALUES (?, ?, 'paid', 100, ?, ?)
                """,
                (
                    "mtg-paid",
                    2,
                    json.dumps({"service_type": "mtg", "host_name": "proxy"}),
                    time_utils.get_msk_now().isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO transactions
                    (payment_id, user_id, status, amount_rub, metadata, created_date)
                VALUES (?, ?, 'paid', 100, ?, ?)
                """,
                (
                    "legacy-xui-paid",
                    3,
                    json.dumps({"host_name": "legacy-vpn"}),
                    time_utils.get_msk_now().isoformat(),
                ),
            )
        assert not database.has_paid_vpn_transaction(2)
        assert database.has_paid_vpn_transaction(3)

        common = {
            "user_id": 1,
            "months": 1,
            "price": 100.0,
            "action": "new",
            "key_id": 0,
            "host_name": "single-host",
            "plan_id": 999,
            "plan_name": "Snapshot plan",
            "service_type": "xui",
            "payment_method": "Telegram Stars",
            "provider_payment_id": "single-recovery",
            "fulfillment_target_expiry_ms": 2_000_000_000_000,
        }
        assert database.create_pending_transaction(
            "single-recovery", 1, 100.0, common
        )
        reserved = database.reserve_pending_transaction("single-recovery")
        assert reserved
        stale = dict(reserved)
        stale["processing_started_at"] = (
            time_utils.get_msk_now() - timedelta(minutes=20)
        ).isoformat()
        assert database.update_reserved_transaction_metadata(
            "single-recovery", stale
        )
        assert database.recover_stale_processing_transactions(15) == 1

        unsafe = dict(common)
        unsafe.pop("fulfillment_target_expiry_ms")
        unsafe["provider_payment_id"] = "unsafe-recovery"
        assert database.create_pending_transaction(
            "unsafe-recovery", 1, 100.0, unsafe
        )
        unsafe = database.reserve_pending_transaction("unsafe-recovery")
        assert unsafe
        unsafe["processing_started_at"] = stale["processing_started_at"]
        assert database.update_reserved_transaction_metadata(
            "unsafe-recovery", unsafe
        )
        assert database.recover_stale_processing_transactions(15) == 0

        discounted = dict(common)
        discounted.update(
            {
                "provider_payment_id": "discount-1",
                "referral_discount_applied": True,
            }
        )
        assert database.create_pending_transaction(
            "discount-1", 1, 90.0, discounted
        )
        discounted["provider_payment_id"] = "discount-2"
        assert not database.create_pending_transaction(
            "discount-2", 1, 90.0, discounted
        )
        with sqlite3.connect(database.DB_FILE) as conn:
            conn.execute(
                "UPDATE transactions SET status='canceled' WHERE payment_id='discount-1'"
            )
        assert database.create_pending_transaction(
            "discount-2", 1, 90.0, discounted
        )

        p2p = {
            **common,
            "request_id": "request-1",
            "referral_discount_applied": False,
            "submitted": True,
        }
        assert database.create_p2p_request("request-1", p2p)
        stored = database.get_p2p_request("request-1")
        assert stored
        assert stored["months"] == 1
        assert stored["service_type"] == "xui"
        assert stored["plan_name"] == "Snapshot plan"

        with sqlite3.connect(database.DB_FILE) as conn:
            status = conn.execute(
                "SELECT status FROM transactions WHERE payment_id='single-recovery'"
            ).fetchone()[0]
        assert status == "pending"

    print("Payment business fix checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

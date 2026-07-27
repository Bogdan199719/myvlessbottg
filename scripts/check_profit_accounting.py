#!/usr/bin/env python3
"""Check profit accounting date and edit invariants."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))

    app_source = (root / "src/shop_bot/webhook_server/app.py").read_text(
        encoding="utf-8"
    )
    required_fragments = (
        "Revenue is always rebuilt from the paid transaction journal.",
        "revenue = get_paid_revenue_between(period_start, period_end)",
        'revenue_rub=calculated["revenue_rub"]',
        "Invalid partner shares in settings",
        "vlad_net = profit_pool - bogdan_profit",
    )
    missing = [fragment for fragment in required_fragments if fragment not in app_source]
    if missing:
        print("Profit accounting checks FAILED:")
        for fragment in missing:
            print(f" - missing edit-route fragment: {fragment}")
        return 1

    database_source = (
        root / "src/shop_bot/data_manager/database.py"
    ).read_text(encoding="utf-8")
    delete_user_source = database_source.split(
        "def delete_user_everywhere(", 1
    )[1].split("\ndef ", 1)[0]
    if "DELETE FROM transactions" in delete_user_source:
        print("Profit accounting checks FAILED:")
        print(" - deleting a user still deletes immutable payment journal rows")
        return 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        os.environ["DB_PATH"] = str(Path(tmp_dir) / "test.db")

        from shop_bot.data_manager import database

        database.initialize_db()
        database.run_migration()

        with sqlite3.connect(database.DB_FILE) as conn:
            conn.execute(
                "INSERT INTO users (telegram_id, username) VALUES (?, ?)",
                (999001, "ledger-delete-check"),
            )
            conn.execute(
                """
                INSERT INTO transactions
                    (payment_id, user_id, status, amount_rub, created_date)
                VALUES (?, ?, 'paid', ?, ?)
                """,
                ("deletion-ledger", 999001, 50.0, "2026-06-04 12:00:00"),
            )
            conn.executemany(
                """
                INSERT INTO transactions
                    (payment_id, user_id, status, amount_rub, created_date)
                VALUES (?, ?, 'paid', ?, ?)
                """,
                (
                    (
                        "before-window",
                        1,
                        100.0,
                        "2026-06-01 23:59:59.999999+03:00",
                    ),
                    (
                        "inside-window",
                        1,
                        200.0,
                        "2026-06-02 00:00:00.000000+03:00",
                    ),
                    (
                        "inside-window-z",
                        1,
                        300.0,
                        "2026-06-02T01:00:00Z",
                    ),
                    (
                        "after-window",
                        1,
                        400.0,
                        "2026-06-03 00:00:00.000000+03:00",
                    ),
                ),
            )

        if not database.delete_user_everywhere(999001):
            print("Profit accounting checks FAILED:")
            print(" - test user profile deletion failed")
            return 1
        with sqlite3.connect(database.DB_FILE) as conn:
            deleted_user = conn.execute(
                "SELECT 1 FROM users WHERE telegram_id = ?", (999001,)
            ).fetchone()
            preserved_payment = conn.execute(
                "SELECT 1 FROM transactions WHERE payment_id = ?",
                ("deletion-ledger",),
            ).fetchone()
        if deleted_user or not preserved_payment:
            print("Profit accounting checks FAILED:")
            print(" - profile deletion did not preserve the payment journal")
            return 1

        revenue = database.get_paid_revenue_between(
            "2026-06-02 00:00:00", "2026-06-03 00:00:00"
        )
        if revenue != 500.0:
            print("Profit accounting checks FAILED:")
            print(f" - expected revenue 500.0 for local day window, got {revenue}")
            return 1

        first_paid = database.get_first_paid_transaction_date()
        if first_paid != "2026-06-01 23:59:59.999999+03:00":
            print("Profit accounting checks FAILED:")
            print(f" - unexpected first paid transaction date: {first_paid}")
            return 1

        distribution_id = database.create_profit_distribution(
            period_start="2026-06-02 00:00:00",
            period_end="2026-06-03 00:00:00",
            revenue_rub=500.0,
            bogdan_share_percent=40.0,
            vlad_share_percent=60.0,
            vlad_tax_percent=9.0,
            server_cost_rub=100.0,
            bogdan_profit_rub=142.0,
            vlad_gross_rub=213.0,
            vlad_tax_rub=45.0,
            vlad_net_rub=213.0,
        )
        if not distribution_id:
            print("Profit accounting checks FAILED:")
            print(" - failed to create test profit distribution")
            return 1

        overlap_cases = (
            ("2026-06-01 00:00:00", "2026-06-02 00:00:00", False),
            ("2026-06-02 12:00:00", "2026-06-04 00:00:00", True),
            ("2026-06-03 00:00:00", "2026-06-04 00:00:00", False),
            (None, "2026-06-02 00:00:00", False),
            (None, "2026-06-02 00:00:01", True),
        )
        for start_at, end_at, expected in overlap_cases:
            actual = database.has_profit_distribution_overlap(start_at, end_at)
            if actual != expected:
                print("Profit accounting checks FAILED:")
                print(
                    " - overlap mismatch for "
                    f"{start_at!r} -> {end_at!r}: expected {expected}, got {actual}"
                )
                return 1

        if not database.mark_profit_distribution_paid(distribution_id):
            print("Profit accounting checks FAILED:")
            print(" - failed to mark the test distribution as paid")
            return 1
        if database.void_profit_distribution(distribution_id):
            print("Profit accounting checks FAILED:")
            print(" - a paid distribution can still be voided")
            return 1

    print("Profit accounting checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

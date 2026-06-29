"""Checks payment dates used by dashboard and profit analytics."""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shop_bot.data_manager import database  # noqa: E402
from shop_bot.utils import time_utils  # noqa: E402


def _copy_db_to_temp() -> tuple[tempfile.TemporaryDirectory, Path]:
    temp_dir = tempfile.TemporaryDirectory()
    temp_db = Path(temp_dir.name) / "users.db"
    shutil.copy2(ROOT / "users.db", temp_db)
    return temp_dir, temp_db


def main() -> int:
    temp_dir, temp_db = _copy_db_to_temp()
    try:
        database.DB_FILE = temp_db
        database.run_migration()

        with sqlite3.connect(temp_db) as conn:
            conn.row_factory = sqlite3.Row
            columns = [row[1] for row in conn.execute("PRAGMA table_info(transactions)")]
            if "paid_date" not in columns:
                print("paid_date column is missing after migration")
                return 1

            paid_rows = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE status = 'paid'"
            ).fetchone()[0]
            paid_date_rows = conn.execute(
                """
                SELECT COUNT(*)
                FROM transactions
                WHERE status = 'paid'
                  AND paid_date IS NOT NULL
                  AND paid_date != ''
                """
            ).fetchone()[0]
            if paid_rows != paid_date_rows:
                print(
                    f"paid_date backfill mismatch: paid={paid_rows}, "
                    f"with_paid_date={paid_date_rows}"
                )
                return 1

            direct_revenue = float(
                conn.execute(
                    "SELECT COALESCE(SUM(amount_rub), 0) FROM transactions WHERE status = 'paid'"
                ).fetchone()[0]
            )
            function_revenue = database.get_paid_revenue_between()
            if abs(direct_revenue - function_revenue) > 0.01:
                print(
                    f"all-time revenue mismatch: direct={direct_revenue}, "
                    f"function={function_revenue}"
                )
                return 1

            payment_id = f"analytics-check-{uuid.uuid4()}"
            conn.execute(
                """
                INSERT INTO transactions
                    (payment_id, user_id, status, amount_rub, metadata, created_date)
                VALUES (?, ?, 'processing', 123.0, '{}', ?)
                """,
                (payment_id, 0, time_utils.get_msk_now().isoformat()),
            )
            conn.commit()

        if not database.finalize_reserved_transaction(payment_id, success=True):
            print("finalize_reserved_transaction failed for analytics check payment")
            return 1

        with sqlite3.connect(temp_db) as conn:
            row = conn.execute(
                "SELECT status, paid_date FROM transactions WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            if not row or row[0] != "paid" or not row[1]:
                print("paid finalization did not store paid_date")
                return 1

        print("Payment analytics checks: OK")
        return 0
    finally:
        temp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

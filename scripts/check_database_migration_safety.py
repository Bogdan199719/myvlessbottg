#!/usr/bin/env python3
"""Regression checks for non-destructive migrations and key identity rules."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="shopbot-migration-safety-") as tmp_dir:
        db_path = Path(tmp_dir) / "users.db"
        os.environ["DB_PATH"] = str(db_path)

        from shop_bot.data_manager import database

        database.initialize_db()
        assert database.add_new_key(
            50, "shared", "xui-uuid", "xui-key", 2_000_000_000_000, plan_id=1
        )
        assert database.add_new_key(
            50,
            "shared",
            "mtg-uuid",
            "mtg-key",
            2_000_000_000_000,
            plan_id=1,
            service_type="mtg",
        )
        assert not database.update_key_by_email(
            "missing-key", "shared", "missing", 2_000_000_000_000
        )
        with sqlite3.connect(db_path) as conn:
            index_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_vpn_keys_user_host_paid_unique'"
            ).fetchone()[0]
            assert "service_type" in index_sql

            conn.execute(
                "INSERT INTO xui_ip_limit_user_events "
                "(user_id, limit_count, first_exceeded_at, last_exceeded_at, last_checked_at) "
                "VALUES (77, 2, 'now', 'now', 'now')"
            )
            conn.commit()
        assert database.delete_user_keys(77)
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM xui_ip_limit_user_events WHERE user_id=77"
                ).fetchone()[0]
                == 0
            )

            conn.execute("DROP TABLE transactions")
            conn.execute(
                "CREATE TABLE transactions "
                "(user_id INTEGER, metadata TEXT, legacy_value TEXT)"
            )
            conn.execute("INSERT INTO transactions VALUES (1, NULL, 'must-survive')")
            conn.commit()
        try:
            database.run_migration()
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("legacy transaction schema must stop migration")
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT * FROM transactions").fetchall() == [
                (1, None, "must-survive")
            ]

    with tempfile.TemporaryDirectory(prefix="shopbot-host-migration-") as tmp_dir:
        db_path = Path(tmp_dir) / "users.db"
        database.DB_FILE = db_path
        database.initialize_db()
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP INDEX uq_xui_hosts_name")
            conn.executemany(
                "INSERT INTO xui_hosts "
                "(host_name, host_url, host_username, host_pass, host_inbound_id) "
                "VALUES ('duplicate', ?, 'user', 'pass', ?)",
                [("https://one.example", 1), ("https://two.example", 2)],
            )
            conn.commit()
        try:
            database.run_migration()
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate host configs must stop migration")
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM xui_hosts WHERE host_name='duplicate'"
                ).fetchone()[0]
                == 2
            )

    print("Database migration safety checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

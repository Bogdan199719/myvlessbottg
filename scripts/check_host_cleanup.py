#!/usr/bin/env python3
"""Smoke-check host deletion leftovers in users.db.

Usage:
  python3 scripts/check_host_cleanup.py "Kansas City,USA🇺🇸"
  python3 scripts/check_host_cleanup.py "Kansas City,USA🇺🇸" --db ./users.db

Exit codes:
  0: no leftovers found
  1: leftovers detected
  2: invalid usage / db error
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "users.db"


def _host_slug(host_name: str) -> str:
    return (host_name or "").replace(" ", "").lower()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host_name", help="Exact host name as stored in the database")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to SQLite database")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        return 2

    host_name = args.host_name
    host_slug = _host_slug(host_name)
    findings: list[str] = []

    try:
        with sqlite3.connect(db_path) as conn:
            checks = [
                (
                    "xui_hosts",
                    "SELECT COUNT(*) FROM xui_hosts WHERE host_name = ?",
                    (host_name,),
                ),
                (
                    "plans",
                    "SELECT COUNT(*) FROM plans WHERE host_name = ?",
                    (host_name,),
                ),
                (
                    "vpn_keys",
                    "SELECT COUNT(*) FROM vpn_keys WHERE host_name = ?",
                    (host_name,),
                ),
                (
                    "vpn_keys_missing.host_name",
                    "SELECT COUNT(*) FROM vpn_keys_missing WHERE host_name = ?",
                    (host_name,),
                ),
                (
                    "p2p_requests",
                    "SELECT COUNT(*) FROM p2p_requests WHERE host_name = ?",
                    (host_name,),
                ),
                (
                    "payment_method_rules",
                    "SELECT COUNT(*) FROM payment_method_rules WHERE context_key = ?",
                    (f"xui:{host_name}",),
                ),
                (
                    "bot_settings.trial_host_name",
                    "SELECT COUNT(*) FROM bot_settings WHERE key = 'trial_host_name' AND value = ?",
                    (host_name,),
                ),
            ]

            for label, query, params in checks:
                count = conn.execute(query, params).fetchone()[0]
                if count:
                    findings.append(f"{label}: {count}")

            if host_slug:
                missing_by_slug = conn.execute(
                    "SELECT key_email FROM vpn_keys_missing WHERE lower(key_email) LIKE ? ORDER BY key_email",
                    (f"%{host_slug}%",),
                ).fetchall()
                if missing_by_slug:
                    findings.append(
                        "vpn_keys_missing.key_email: "
                        + ", ".join(row[0] for row in missing_by_slug[:5])
                    )
    except sqlite3.Error as exc:
        print(f"ERROR: database query failed: {exc}")
        return 2

    print(f"Checked host: {host_name}")
    print(f"Database: {db_path}")

    if findings:
        print("\nERROR: leftovers found:")
        for item in findings:
            print(f" - {item}")
        return 1

    print("\nOK: no host deletion leftovers found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

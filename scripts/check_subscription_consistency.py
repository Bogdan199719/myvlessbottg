#!/usr/bin/env python3
"""Check global VPN host coverage in users.db.

Usage:
  python3 scripts/check_subscription_consistency.py
  python3 scripts/check_subscription_consistency.py --db ./users.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict


def _rows(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _global_plan_ids(conn: sqlite3.Connection) -> set[int]:
    ids = {
        int(row["plan_id"])
        for row in _rows(
            conn,
            "SELECT plan_id FROM plans WHERE host_name = 'ALL' AND service_type = 'xui'",
        )
        if row.get("plan_id")
    }

    for row in _rows(
        conn,
        """
        SELECT metadata
        FROM transactions
        WHERE status = 'paid'
          AND metadata IS NOT NULL
          AND metadata != ''
        """,
    ):
        try:
            metadata = json.loads(row["metadata"])
            if str(metadata.get("host_name") or "").upper() == "ALL":
                plan_id = int(metadata.get("plan_id") or 0)
                if plan_id > 0:
                    ids.add(plan_id)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return ids


def _active_key_rows(conn: sqlite3.Connection) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT
            u.telegram_id,
            u.username,
            k.host_name,
            k.plan_id,
            k.key_email,
            k.expiry_date,
            COALESCE(k.service_type, 'xui') AS service_type
        FROM users u
        JOIN vpn_keys k ON k.user_id = u.telegram_id
        WHERE datetime(k.expiry_date) > datetime('now')
          AND COALESCE(k.service_type, 'xui') = 'xui'
        ORDER BY u.telegram_id, k.host_name
        """,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="users.db", help="Path to SQLite DB")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    enabled_hosts = {
        row["host_name"]
        for row in _rows(
            conn,
            "SELECT host_name FROM xui_hosts WHERE is_enabled = 1 ORDER BY host_name",
        )
    }
    global_plan_ids = _global_plan_ids(conn)

    print(f"Enabled XUI hosts: {len(enabled_hosts)}")
    for host in sorted(enabled_hosts):
        print(f"  - {host}")
    print(f"Global plan IDs: {sorted(global_plan_ids)}")

    duplicate_urls = _rows(
        conn,
        """
        SELECT host_url, COUNT(*) AS count, group_concat(host_name, ' | ') AS hosts
        FROM xui_hosts
        GROUP BY host_url
        HAVING COUNT(*) > 1
        """,
    )
    if duplicate_urls:
        print("\nDuplicate host URLs:")
        for row in duplicate_urls:
            print(f"  - {row['host_url']}: {row['hosts']}")

    trial_by_user: dict[tuple[int, str | None], set[str]] = defaultdict(set)
    paid_global_by_user: dict[tuple[int, str | None], set[str]] = defaultdict(set)

    for key in _active_key_rows(conn):
        user = (int(key["telegram_id"]), key.get("username"))
        host_name = key.get("host_name")
        if not host_name:
            continue
        try:
            plan_id = int(key.get("plan_id") or 0)
        except (TypeError, ValueError):
            plan_id = 0

        if plan_id == 0:
            trial_by_user[user].add(host_name)
            continue

        key_email = str(key.get("key_email") or "").lower()
        if plan_id in global_plan_ids or "-global-" in key_email:
            paid_global_by_user[user].add(host_name)

    issues = 0

    print("\nActive trial coverage:")
    if not trial_by_user:
        print("  no active trial users")
    for (user_id, username), hosts in sorted(trial_by_user.items()):
        missing = enabled_hosts - hosts
        if missing:
            issues += 1
            print(
                f"  MISSING user={user_id} @{username or '-'} "
                f"{len(hosts)}/{len(enabled_hosts)} missing={sorted(missing)}"
            )
        else:
            print(f"  OK user={user_id} @{username or '-'} {len(hosts)}/{len(enabled_hosts)}")

    print("\nActive paid global coverage:")
    if not paid_global_by_user:
        print("  no active paid global users")
    for (user_id, username), hosts in sorted(paid_global_by_user.items()):
        missing = enabled_hosts - hosts
        if missing:
            issues += 1
            print(
                f"  MISSING user={user_id} @{username or '-'} "
                f"{len(hosts)}/{len(enabled_hosts)} missing={sorted(missing)}"
            )
        else:
            print(f"  OK user={user_id} @{username or '-'} {len(hosts)}/{len(enabled_hosts)}")

    if issues:
        print(f"\nFAIL: {issues} incomplete global access record(s).")
        return 1

    print("\nOK: all active global access records cover every enabled host.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate subscription-wide warning and enforcement rules without panels."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


with tempfile.TemporaryDirectory(prefix="shopbot-global-ip-limit-") as temp_dir:
    os.environ["DB_PATH"] = str(Path(temp_dir) / "users.db")

    from shop_bot.data_manager import database
    from shop_bot.utils import time_utils

    database.initialize_db()

    now_text = time_utils.get_msk_now().isoformat(timespec="seconds")
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            """
            INSERT INTO xui_ip_limit_events (
                key_id, user_id, host_name, key_email, observed_ip_count,
                limit_count, state, first_exceeded_at, last_exceeded_at,
                last_checked_at, warned_at, enforced_at
            ) VALUES (9000, 9000, 'legacy', 'legacy@example', 8, 6,
                      'enforced', ?, ?, ?, ?, ?)
            """,
            (now_text, now_text, now_text, now_text, now_text),
        )
        conn.commit()
    database.initialize_db()
    assert database.get_enforced_xui_ip_limit_user_ids() == {9000}
    assert database.release_xui_ip_limit_users([9000]) == [{"user_id": 9000}]
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            "DELETE FROM xui_ip_limit_user_events WHERE user_id=9000"
        )
        conn.commit()

    stale_candidate = {"user_id": 999, "ip_count": 7}
    database.process_xui_ip_limit_user_observations(
        [stale_candidate], limit_count=6, warning_grace_hours=24
    )
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            """
            UPDATE xui_ip_limit_user_events
            SET last_checked_at=?
            WHERE user_id=999
            """,
            (
                (
                    time_utils.get_msk_now() - timedelta(minutes=16)
                ).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    not_consecutive = database.process_xui_ip_limit_user_observations(
        [stale_candidate], limit_count=6, warning_grace_hours=24
    )
    assert not not_consecutive["warnings"]
    confirmed_after_reset = database.process_xui_ip_limit_user_observations(
        [stale_candidate], limit_count=6, warning_grace_hours=24
    )
    assert len(confirmed_after_reset["warnings"]) == 1
    database.process_xui_ip_limit_user_observations(
        [{"user_id": 999, "ip_count": 0}],
        limit_count=6,
        warning_grace_hours=24,
    )
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute("DELETE FROM xui_ip_limit_user_events WHERE user_id=999")
        conn.commit()

    breach = {"user_id": 1001, "ip_count": 7}
    first = database.process_xui_ip_limit_user_observations(
        [breach], limit_count=6, warning_grace_hours=24
    )
    assert not first["warnings"], "one isolated scan must only create a candidate"
    assert database.get_xui_ip_limit_user_events() == []

    second = database.process_xui_ip_limit_user_observations(
        [breach], limit_count=6, warning_grace_hours=24
    )
    assert second["warnings"] == [
        {"user_id": 1001, "ip_count": 7, "limit_count": 6}
    ]
    assert database.mark_xui_ip_limit_user_warning_result(1001)
    assert database.get_enforced_xui_ip_limit_user_ids() == set()

    before_grace = database.process_xui_ip_limit_user_observations(
        [breach], limit_count=6, warning_grace_hours=24
    )
    assert not before_grace["enforced"]

    old = time_utils.get_msk_now() - timedelta(hours=25)
    old_text = old.isoformat(timespec="seconds")
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            "UPDATE xui_ip_limit_user_events SET warned_at=? WHERE user_id=1001",
            (old_text,),
        )
        conn.commit()

    enforced = database.process_xui_ip_limit_user_observations(
        [breach], limit_count=6, warning_grace_hours=24
    )
    assert enforced["enforced"] == [
        {"user_id": 1001, "ip_count": 7, "limit_count": 6}
    ]
    assert database.get_enforced_xui_ip_limit_user_ids() == {1001}

    clean = database.process_xui_ip_limit_user_observations(
        [{"user_id": 1001, "ip_count": 1}],
        limit_count=6,
        warning_grace_hours=24,
    )
    assert not clean["resolved"], "an enforced decision stays until admin release"

    early_user = {"user_id": 1002, "ip_count": 8}
    database.process_xui_ip_limit_user_observations(
        [early_user], limit_count=6, warning_grace_hours=24
    )
    database.process_xui_ip_limit_user_observations(
        [early_user], limit_count=6, warning_grace_hours=24
    )
    forced = database.enforce_xui_ip_limit_user_now(1002)
    assert forced == {"user_id": 1002}
    assert database.get_enforced_xui_ip_limit_user_ids() == {1001, 1002}

    released = database.release_xui_ip_limit_users([1001])
    assert released == [{"user_id": 1001}]
    assert database.get_enforced_xui_ip_limit_user_ids() == {1002}

    repeat_first = database.process_xui_ip_limit_user_observations(
        [breach], limit_count=6, warning_grace_hours=24
    )
    assert not repeat_first["warnings"]
    repeat_second = database.process_xui_ip_limit_user_observations(
        [breach], limit_count=6, warning_grace_hours=24
    )
    assert len(repeat_second["warnings"]) == 1

    normalizing_user = {"user_id": 1003, "ip_count": 9}
    database.process_xui_ip_limit_user_observations(
        [normalizing_user], limit_count=6, warning_grace_hours=24
    )
    database.process_xui_ip_limit_user_observations(
        [normalizing_user], limit_count=6, warning_grace_hours=24
    )
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            """
            UPDATE xui_ip_limit_user_events
            SET last_exceeded_at=?
            WHERE user_id=1003
            """,
            (
                (
                    time_utils.get_msk_now() - timedelta(minutes=61)
                ).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    normalized = database.process_xui_ip_limit_user_observations(
        [{"user_id": 1003, "ip_count": 2}],
        limit_count=6,
        warning_grace_hours=24,
    )
    assert normalized["resolved"] == [{"user_id": 1003}]

    bulk = database.release_xui_ip_limit_users()
    assert bulk == [{"user_id": 1002}]
    assert database.get_enforced_xui_ip_limit_user_ids() == set()

    history_cutoff = time_utils.get_msk_now() - timedelta(days=8)
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            """
            UPDATE xui_ip_limit_user_events
            SET resolved_at=?
            WHERE state IN ('resolved', 'released')
            """,
            (history_cutoff.isoformat(timespec="seconds"),),
        )
        conn.commit()
    assert database.prune_xui_ip_limit_user_history(7) == 2
    remaining = database.get_xui_ip_limit_user_events()
    assert [item["user_id"] for item in remaining] == [1001]

print("OK: subscription-wide IP-limit rules are stable.")

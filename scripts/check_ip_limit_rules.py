#!/usr/bin/env python3
"""Validate the warning-first IP-limit state machine without contacting panels."""

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

with tempfile.TemporaryDirectory(prefix="shopbot-ip-limit-check-") as temp_dir:
    os.environ["DB_PATH"] = str(Path(temp_dir) / "users.db")

    from shop_bot.data_manager import database
    from shop_bot.utils import time_utils

    database.initialize_db()

    observation = {
        "key_id": 42,
        "user_id": 1001,
        "host_name": "Test host",
        "key_email": "test@example",
        "ip_count": 11,
    }

    first = database.process_xui_ip_limit_observations(
        [observation], limit_count=10, warning_grace_hours=24
    )
    assert len(first["warnings"]) == 1
    assert not first["enforced"]
    assert database.get_enforced_xui_ip_limit_key_ids() == set()

    assert database.mark_xui_ip_limit_warning_result(42)
    second = database.process_xui_ip_limit_observations(
        [observation], limit_count=10, warning_grace_hours=24
    )
    assert not second["enforced"], "must not enforce before the grace period"

    old = time_utils.get_msk_now() - timedelta(hours=25)
    old_text = old.isoformat(timespec="seconds")
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            "UPDATE xui_ip_limit_events SET warned_at=? WHERE key_id=42",
            (old_text,),
        )
        conn.commit()

    enforced = database.process_xui_ip_limit_observations(
        [observation], limit_count=10, warning_grace_hours=24
    )
    assert len(enforced["enforced"]) == 1

    second_observation = dict(
        observation,
        key_id=43,
        user_id=1002,
        key_email="second@example",
        ip_count=12,
    )
    database.process_xui_ip_limit_observations(
        [second_observation], limit_count=10, warning_grace_hours=24
    )
    assert database.mark_xui_ip_limit_warning_result(43)
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            "UPDATE xui_ip_limit_events SET warned_at=? WHERE key_id=43",
            (old_text,),
        )
        conn.commit()
    second_enforced = database.process_xui_ip_limit_observations(
        [second_observation], limit_count=10, warning_grace_hours=24
    )
    assert len(second_enforced["enforced"]) == 1

    forced_observation = dict(
        observation,
        key_id=44,
        user_id=1003,
        key_email="forced@example",
        ip_count=13,
    )
    database.process_xui_ip_limit_observations(
        [forced_observation], limit_count=10, warning_grace_hours=24
    )
    forced = database.enforce_xui_ip_limit_now(44)
    assert forced and forced["key_id"] == 44
    assert database.enforce_xui_ip_limit_now(44) is None
    assert database.get_enforced_xui_ip_limit_key_ids() == {42, 43, 44}

    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            "UPDATE xui_ip_limit_events SET last_exceeded_at=? WHERE key_id=42",
            (old_text,),
        )
        conn.commit()
    clean_observation = dict(observation, ip_count=10)
    resolved = database.process_xui_ip_limit_observations(
        [clean_observation], limit_count=10, warning_grace_hours=24
    )
    assert not resolved["resolved"], "an enforced limit must remain active"
    assert database.get_enforced_xui_ip_limit_key_ids() == {42, 43, 44}

    released = database.release_xui_ip_limits([42])
    assert [item["key_id"] for item in released] == [42]
    assert database.get_enforced_xui_ip_limit_key_ids() == {43, 44}
    released_event = next(
        item for item in database.get_xui_ip_limit_events() if item["key_id"] == 42
    )
    assert released_event["state"] == "released"
    assert released_event["resolved_at"], (
        "an administrator release must be timestamped"
    )

    repeated = database.process_xui_ip_limit_observations(
        [observation], limit_count=10, warning_grace_hours=24
    )
    assert len(repeated["warnings"]) == 1
    assert not repeated["enforced"]
    repeated_event = next(
        item for item in database.get_xui_ip_limit_events() if item["key_id"] == 42
    )
    assert repeated_event["state"] == "warning"
    assert repeated_event["warned_at"] is None
    assert repeated_event["enforced_at"] is None
    assert repeated_event["resolved_at"] is None

    bulk_released = database.release_xui_ip_limits()
    assert [item["key_id"] for item in bulk_released] == [43, 44]
    assert database.get_enforced_xui_ip_limit_key_ids() == set()
    assert database.release_xui_ip_limits() == []

    history_cutoff = time_utils.get_msk_now() - timedelta(days=31)
    with sqlite3.connect(database.DB_FILE) as conn:
        conn.execute(
            "UPDATE xui_ip_limit_events SET resolved_at=? WHERE key_id IN (43, 44)",
            (history_cutoff.isoformat(timespec="seconds"),),
        )
        conn.commit()
    assert database.prune_xui_ip_limit_history(30) == 2
    remaining_ids = {
        item["key_id"] for item in database.get_xui_ip_limit_events()
    }
    assert remaining_ids == {42}, "active events must not be pruned with history"

print("OK: warning-first IP-limit rules are stable.")

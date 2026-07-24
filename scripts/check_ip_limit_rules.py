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
    assert database.get_enforced_xui_ip_limit_key_ids() == {42}

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
    assert database.get_enforced_xui_ip_limit_key_ids() == {42}

print("OK: warning-first IP-limit rules are stable.")

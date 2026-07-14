#!/usr/bin/env python3
"""Check admin analytics, proxy IP handling, rate-limit locks and staged restore."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from shop_bot.utils.ip_allowlist import get_client_ip  # noqa: E402
from shop_bot.webhook_server import restore_manager  # noqa: E402
from shop_bot.webhook_server.restore_manager import (  # noqa: E402
    apply_pending_restore,
    quarantine_pending_restore,
    schedule_process_restart,
    stage_pending_restore,
)


class _Request:
    def __init__(self, remote_addr: str, forwarded_for: str = ""):
        self.remote_addr = remote_addr
        self.headers = {"X-Forwarded-For": forwarded_for}


def _create_db(path: Path, marker: str) -> None:
    with sqlite3.connect(path) as conn:
        for table in ("users", "vpn_keys", "transactions", "bot_settings"):
            conn.execute(f"CREATE TABLE {table} (value TEXT)")
        conn.execute("INSERT INTO users VALUES (?)", (marker,))
        conn.commit()


def _read_marker(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return str(conn.execute("SELECT value FROM users").fetchone()[0])


def _check_client_ip() -> None:
    trusted = "127.0.0.0/8,172.16.0.0/12"
    assert (
        get_client_ip(_Request("203.0.113.9", "1.1.1.1"), trusted)
        == "203.0.113.9"
    ), "an untrusted direct client must not control X-Forwarded-For"
    assert (
        get_client_ip(_Request("172.18.0.1", "1.1.1.1, 198.51.100.7"), trusted)
        == "198.51.100.7"
    ), "nginx-appended rightmost client IP must win over a spoofed prefix"
    assert (
        get_client_ip(_Request("172.18.0.1", "garbage, 198.51.100.8"), trusted)
        == "198.51.100.8"
    )


def _check_staged_restore() -> None:
    with tempfile.TemporaryDirectory(prefix="check-admin-restore-") as temp:
        root = Path(temp)
        live_db = root / "users.db"
        incoming_db = root / "incoming.db"
        env_file = root / ".env"
        incoming_env = root / "incoming.env"
        _create_db(live_db, "live")
        _create_db(incoming_db, "restored")
        env_file.write_text("OLD=1\n", encoding="utf-8")
        incoming_env.write_text("SECRET=restored\n", encoding="utf-8")

        # Exercise a live database that has used WAL; the staged payload must
        # still be applied only by the startup helper.
        with sqlite3.connect(live_db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE users SET value = 'live-wal'")
            conn.commit()

        pending = stage_pending_restore(root, incoming_db, incoming_env)
        assert pending.exists()
        assert _read_marker(live_db) == "live-wal", "staging must not touch live DB"

        result = apply_pending_restore(root, live_db, env_file)
        assert result and result["rollback_path"]
        assert _read_marker(live_db) == "restored"
        assert _read_marker(Path(result["rollback_path"])) == "live-wal"
        assert env_file.read_text(encoding="utf-8") == "SECRET=restored\n"
        assert not pending.exists()
        assert not Path(f"{live_db}-wal").exists()
        assert not Path(f"{live_db}-shm").exists()
        assert os.stat(live_db).st_mode & 0o777 == 0o600
        assert os.stat(env_file).st_mode & 0o777 == 0o600

        pending = stage_pending_restore(root, incoming_db)
        with (pending / "users.db").open("ab") as damaged:
            damaged.write(b"tampered")
        try:
            apply_pending_restore(root, live_db, env_file)
        except RuntimeError as exc:
            assert "checksum mismatch" in str(exc)
        else:
            raise AssertionError("a modified pending database must be rejected")
        assert _read_marker(live_db) == "restored"
        failed_dir = quarantine_pending_restore(root)
        assert failed_dir and failed_dir.exists() and not pending.exists()

        # Fail after the replacement of .env but before its directory fsync.
        # Both live files must return to the pre-restore state.
        _create_db(root / "incoming-second.db", "second-restore")
        second_env = root / "incoming-second.env"
        second_env.write_text("SECRET=second\n", encoding="utf-8")
        pending = stage_pending_restore(
            root, root / "incoming-second.db", second_env
        )
        original_fsync_directory = restore_manager._fsync_directory
        fsync_calls = 0

        def fail_after_env_replace(path: Path) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 3:
                raise OSError("simulated env fsync failure")
            original_fsync_directory(path)

        restore_manager._fsync_directory = fail_after_env_replace
        try:
            apply_pending_restore(root, live_db, env_file)
        except OSError as exc:
            assert "simulated env fsync failure" in str(exc)
        else:
            raise AssertionError("simulated post-env failure must propagate")
        finally:
            restore_manager._fsync_directory = original_fsync_directory
        assert _read_marker(live_db) == "restored"
        assert env_file.read_text(encoding="utf-8") == "SECRET=restored\n"
        quarantine_pending_restore(root)


def _check_source_invariants() -> None:
    app_source = (SRC_ROOT / "shop_bot/webhook_server/app.py").read_text(
        encoding="utf-8"
    )
    subscription_source = (
        SRC_ROOT / "shop_bot/webhook_server/subscription_api.py"
    ).read_text(encoding="utf-8")
    main_source = (SRC_ROOT / "shop_bot/__main__.py").read_text(encoding="utf-8")
    required_app_fragments = (
        'is_free_promo = method == "Promo" and amount <= 0',
        "if is_monetary_payment:",
        'if item["orders"] > 0',
        "_login_attempts_lock = threading.Lock()",
        "stage_pending_restore(APP_ROOT, db_src, env_src)",
        "@after_this_request",
        "schedule_process_restart()",
    )
    for fragment in required_app_fragments:
        assert fragment in app_source, f"missing admin safety invariant: {fragment}"
    assert "shutil.copyfile(db_src, DB_FILE)" not in app_source, (
        "web request must never replace the live SQLite main file"
    )
    assert "_invalid_token_lock = threading.Lock()" in subscription_source
    assert "with _invalid_token_lock:" in subscription_source
    assert main_source.index("apply_pending_restore(") < main_source.index(
        "from shop_bot.webhook_server.app import create_webhook_app"
    ), "pending restore must run before application modules open SQLite"


def _check_restart_scheduler() -> None:
    called = []
    completed = threading.Event()

    def fake_exit(status: int) -> None:
        called.append(status)
        completed.set()

    timer = schedule_process_restart(0.01, exit_func=fake_exit)
    assert timer.daemon
    assert completed.wait(timeout=1), "scheduled restart callback did not run"
    assert called == [0]


def main() -> int:
    _check_client_ip()
    _check_staged_restore()
    _check_source_invariants()
    _check_restart_scheduler()
    print("Web admin safety checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

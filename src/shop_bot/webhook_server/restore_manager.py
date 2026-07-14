"""Staged, restart-time restore helpers for the SQLite application database."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone


PENDING_RESTORE_DIRNAME = ".restore_pending"
REQUIRED_TABLES = {"users", "vpn_keys", "transactions", "bot_settings"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sqlite_database(path: Path) -> None:
    database_uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise ValueError("Проверка целостности БД не пройдена.")
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    missing = sorted(REQUIRED_TABLES - tables)
    if missing:
        raise ValueError(
            "В backup-базе отсутствуют обязательные таблицы: " + ", ".join(missing)
        )


def _fsync_file(path: Path) -> None:
    with path.open("rb") as file_ref:
        os.fsync(file_ref.fileno())


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def stage_pending_restore(
    app_root: Path,
    database_source: Path,
    env_source: Path | None = None,
) -> Path:
    """Persist a validated restore payload for the next clean process start."""
    app_root = app_root.resolve()
    pending_dir = app_root / PENDING_RESTORE_DIRNAME
    if pending_dir.exists():
        raise ValueError(
            "Предыдущий импорт уже ожидает перезапуска. Дождитесь запуска приложения."
        )

    validate_sqlite_database(database_source)
    stage_dir = Path(tempfile.mkdtemp(prefix=".restore-stage-", dir=app_root))
    os.chmod(stage_dir, 0o700)
    try:
        staged_db = stage_dir / "users.db"
        shutil.copyfile(database_source, staged_db)
        os.chmod(staged_db, 0o600)
        _fsync_file(staged_db)

        apply_env = bool(env_source and env_source.exists())
        if apply_env:
            staged_env = stage_dir / ".env"
            shutil.copyfile(env_source, staged_env)
            os.chmod(staged_env, 0o600)
            _fsync_file(staged_env)

        manifest = {
            "version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "database_sha256": sha256_file(staged_db),
            "apply_env": apply_env,
        }
        manifest_path = stage_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)
        _fsync_file(manifest_path)
        _fsync_directory(stage_dir)
        os.replace(stage_dir, pending_dir)
        _fsync_directory(app_root)
        return pending_dir
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    with sqlite3.connect(source, timeout=30) as source_conn, sqlite3.connect(
        destination
    ) as destination_conn:
        source_conn.backup(destination_conn)
    os.chmod(destination, 0o600)
    _fsync_file(destination)


def _atomic_copy(source: Path, destination: Path, mode: int = 0o600) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.restore-new")
    try:
        shutil.copyfile(source, temp_path)
        os.chmod(temp_path, mode)
        _fsync_file(temp_path)
        os.replace(temp_path, destination)
        _fsync_directory(destination.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def apply_pending_restore(app_root: Path, db_file: Path, env_file: Path) -> dict | None:
    """Apply a pending restore before any application DB connection is opened."""
    pending_dir = app_root.resolve() / PENDING_RESTORE_DIRNAME
    if not pending_dir.exists():
        return None

    manifest_path = pending_dir / "manifest.json"
    incoming_db = pending_dir / "users.db"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("Pending restore manifest is missing or invalid") from exc

    expected_checksum = str(manifest.get("database_sha256") or "")
    if not expected_checksum or sha256_file(incoming_db) != expected_checksum:
        raise RuntimeError("Pending restore database checksum mismatch")
    validate_sqlite_database(incoming_db)

    db_file.parent.mkdir(parents=True, exist_ok=True)
    backup_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    rollback_path = db_file.with_name(
        f"{db_file.name}.bak.restore-{backup_timestamp}"
    )
    if db_file.exists():
        _sqlite_snapshot(db_file, rollback_path)

    apply_env = bool(manifest.get("apply_env"))
    env_existed = env_file.exists()
    env_rollback_path = env_file.with_name(
        f"{env_file.name}.bak.restore-{backup_timestamp}"
    )
    if apply_env and env_existed:
        _atomic_copy(env_file, env_rollback_path)

    # No application threads exist at this point. Remove sidecars belonging to
    # the old main file before atomically installing the self-contained backup.
    for suffix in ("-wal", "-shm"):
        Path(f"{db_file}{suffix}").unlink(missing_ok=True)

    try:
        _atomic_copy(incoming_db, db_file)
        validate_sqlite_database(db_file)
        if apply_env:
            incoming_env = pending_dir / ".env"
            if not incoming_env.exists():
                raise RuntimeError("Pending restore requested .env but it is missing")
            _atomic_copy(incoming_env, env_file)
    except Exception as restore_error:
        rollback_errors = []
        try:
            if apply_env:
                if env_existed and env_rollback_path.exists():
                    _atomic_copy(env_rollback_path, env_file)
                elif not env_existed:
                    env_file.unlink(missing_ok=True)
                    _fsync_directory(env_file.parent)
        except Exception as env_rollback_error:
            rollback_errors.append(f".env rollback: {env_rollback_error}")

        try:
            if rollback_path.exists():
                for suffix in ("-wal", "-shm"):
                    Path(f"{db_file}{suffix}").unlink(missing_ok=True)
                _atomic_copy(rollback_path, db_file)
        except Exception as database_rollback_error:
            rollback_errors.append(f"database rollback: {database_rollback_error}")

        if rollback_errors:
            raise RuntimeError(
                "Restore failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from restore_error
        raise

    shutil.rmtree(pending_dir)
    _fsync_directory(app_root.resolve())
    return {
        "rollback_path": str(rollback_path) if rollback_path.exists() else None,
        "env_rollback_path": (
            str(env_rollback_path) if env_rollback_path.exists() else None
        ),
    }


def quarantine_pending_restore(app_root: Path) -> Path | None:
    """Move a failed payload aside so a bad archive cannot cause a restart loop."""
    root = app_root.resolve()
    pending_dir = root / PENDING_RESTORE_DIRNAME
    if not pending_dir.exists():
        return None
    failed_dir = root / (
        ".restore_failed-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    )
    os.replace(pending_dir, failed_dir)
    _fsync_directory(root)
    return failed_dir


def schedule_process_restart(
    delay_seconds: float = 2.0, exit_func=os._exit
) -> threading.Timer:
    """Schedule process exit after an HTTP response without blocking its thread."""
    timer = threading.Timer(delay_seconds, exit_func, args=(0,))
    timer.daemon = True
    timer.start()
    return timer

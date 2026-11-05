#!/usr/bin/env python3
"""Exercise the local backup script with synthetic data only."""

import hashlib
import io
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/backup_project.sh"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="shopbot-backup-check-") as root_name:
        root = Path(root_name)
        project = root / "project"
        output = root / "archives"
        project.mkdir()
        output.mkdir()
        (project / "nested").mkdir()
        (project / "backups").mkdir()
        (project / ".env").write_text("TEST_ONLY=synthetic\n", encoding="utf-8")
        (project / "nested/config.txt").write_text("project file\n", encoding="utf-8")
        (project / "backups/legacy.db").write_bytes(b"legacy fixture")
        with sqlite3.connect(project / "users.db") as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            for table in ("users", "vpn_keys", "transactions", "bot_settings"):
                conn.execute(f"CREATE TABLE {table} (value TEXT)")
            conn.execute("CREATE TABLE example (value TEXT)")
            conn.execute("INSERT INTO example VALUES ('expected')")

        # Three completed old archives exercise the unchanged three-copy limit.
        for index in range(3):
            old = output / f"project_backup_2026-01-0{index + 1}_02-00.tar.gz"
            with tarfile.open(old, "w:gz") as archive:
                data = f"old-{index}".encode()
                member = tarfile.TarInfo("marker")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            Path(str(old)+".sha256").write_text(hashlib.sha256(old.read_bytes()).hexdigest()+"  "+old.name+"\n")
            os.utime(old, (1_700_000_000 + index, 1_700_000_000 + index))

        env = dict(os.environ)
        env.update(
            BACKUP_PROJECT_DIR=str(project),
            BACKUP_DB_PATH=str(project / "users.db"),
            BACKUP_OUTPUT_DIR=str(output),
            BACKUP_LOG_FILE=str(root / "backup.log"),
            BACKUP_RETENTION_COUNT="3",
        )
        subprocess.run(["bash", str(SCRIPT)], env=env, check=True)

        archives = sorted(output.glob("project_backup_*.tar.gz"))
        assert len(archives) == 3, archives
        latest = max(archives, key=lambda item: item.stat().st_mtime)
        assert (Path(str(latest) + ".sha256")).is_file()
        with tarfile.open(latest, "r:gz") as archive:
            names = set(archive.getnames())
            assert "project/.env" in names
            assert "project/nested/config.txt" in names
            assert "project/backups/legacy.db" in names
            assert "backup_extra/sqlite/users.db" in names
            assert "backup_extra/sqlite/users.db.sql" in names
            assert "project/users.db" not in names
            assert "project/users.db-wal" not in names
            snapshot = archive.extractfile("backup_extra/sqlite/users.db")
            assert snapshot is not None
            snapshot_path = root / "snapshot.db"
            snapshot_path.write_bytes(snapshot.read())
        with sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True) as conn:
            assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert conn.execute("SELECT value FROM example").fetchone() == ("expected",)

        # A broken source must not replace or rotate the last valid archives.
        original_archives = {item.name: item.stat().st_size for item in archives}
        (project / "users.db").unlink()
        failed = subprocess.run(
            ["bash", str(SCRIPT)], env=env, check=False, capture_output=True
        )
        assert failed.returncode != 0
        assert {
            item.name: item.stat().st_size
            for item in output.glob("project_backup_*.tar.gz")
        } == original_archives

        assert (project / ".backup_status.json").is_file()
        assert (project / ".backup_status.json").stat().st_mode & 0o777 == 0o600
        print("Backup project check: OK")


if __name__ == "__main__":
    main()

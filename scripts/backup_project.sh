#!/usr/bin/env bash
# Consistent local project backup. Cron supplies TZ=Europe/Moscow.
set -Eeuo pipefail

umask 077

PROJECT_DIR="${BACKUP_PROJECT_DIR:-/root/myvlessbottg}"
BACKUP_DIR="${BACKUP_OUTPUT_DIR:-/bacupmyproject}"
LOG_FILE="${BACKUP_LOG_FILE:-/var/log/backup_my_project.log}"
RETENTION_COUNT="${BACKUP_RETENTION_COUNT:-3}"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
PROJECT_PARENT="$(dirname "$PROJECT_DIR")"
# Resolve only the DB_PATH value; never execute .env as a shell script.
DB_FILE="$(python3 - "$PROJECT_DIR" <<'PYDB'
import os, shlex, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
value = os.environ.get('BACKUP_DB_PATH') or os.environ.get('DB_PATH')
if not value and (root/'.env').is_file():
    for line in (root/'.env').read_text().splitlines():
        line = line.strip().removeprefix('export ')
        if '=' not in line or line.split('=',1)[0].strip() != 'DB_PATH':
            continue
        parts = shlex.split(line.split('=',1)[1], comments=True)
        if len(parts) > 1:
            raise SystemExit('Invalid DB_PATH; set BACKUP_DB_PATH explicitly')
        value = parts[0] if parts else ''
if value and '$' in value:
    raise SystemExit('Interpolated DB_PATH requires an explicit BACKUP_DB_PATH')
path = Path(value or 'users.db')
# The standard container bind maps this prefix to the host project directory.
if str(path).startswith('/app/project/'):
    path = root / str(path).removeprefix('/app/project/')
elif not path.is_absolute():
    path = root/path
print(path.resolve())
PYDB
)"

if [[ ! -d "$PROJECT_DIR" || ! -f "$DB_FILE" || ! -f "$PROJECT_DIR/.env" ]]; then
    printf 'Backup requires the project directory, users.db and .env\n' >&2
    exit 1
fi
if [[ ! "$RETENTION_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Invalid backup retention count\n' >&2
    exit 1
fi
project_real="$(realpath -m "$PROJECT_DIR")"
backup_real="$(realpath -m "$BACKUP_DIR")"
if [[ "$backup_real" == / || "$backup_real" == "$(dirname "$project_real")" ||
      "$backup_real" == "$project_real" || "$backup_real" == "$project_real"/* ]]; then
    printf 'Backup directory must be a dedicated directory outside the project\n' >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR" "$(dirname "$LOG_FILE")"
chmod 700 "$BACKUP_DIR"
touch "$LOG_FILE"
exec >>"$LOG_FILE" 2>&1
exec 9>"$BACKUP_DIR/.backup.lock"
if ! flock -n 9; then
    printf '[%s] Backup skipped: another run is active\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    exit 1
fi

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*"
}

DB_ARCHIVE_PATH="$(realpath --relative-to="$PROJECT_PARENT" "$DB_FILE")"
WORK_DIR=""
TMP_ARCHIVE=""
cleanup() {
    if [[ -n "$TMP_ARCHIVE" && -f "$TMP_ARCHIVE" ]]; then
        rm -f -- "$TMP_ARCHIVE"
    fi
    if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
        rm -rf -- "$WORK_DIR"
    fi
}
trap cleanup EXIT

rotate_backups() {
    # Whitespace/line splitting is unsafe for custom directories containing spaces.
    # Only archives whose saved checksum still matches count as completed copies.
    python3 - "$BACKUP_DIR" "$RETENTION_COUNT" <<'PYROTATE'
import hashlib, sys
from pathlib import Path
root, keep = Path(sys.argv[1]), int(sys.argv[2])
verified = []
for archive in root.glob('project_backup_*.tar.gz'):
    checksum = Path(str(archive) + '.sha256')
    try:
        expected = checksum.read_text().split()[0]
        digest = hashlib.sha256()
        with archive.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() == expected:
            verified.append(archive)
    except (OSError, IndexError):
        continue
verified.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
for archive in verified[keep:]:
    archive.unlink()
    Path(str(archive) + '.sha256').unlink(missing_ok=True)
    print('Rotation: deleted an older verified backup')
PYROTATE
}

create_sqlite_backup() {
    python3 - "$DB_FILE" "$WORK_DIR/backup_extra/sqlite/users.db" \
        "$WORK_DIR/backup_extra/sqlite/users.db.sql" <<'PY'
import sqlite3
import sys
from pathlib import Path

source_db, backup_db, dump_sql = map(Path, sys.argv[1:])
src = sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(backup_db)
try:
    with dst:
        src.backup(dst)
    if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise RuntimeError("SQLite backup failed quick_check")
    required_tables = {"users", "vpn_keys", "transactions", "bot_settings"}
    actual_tables = {
        row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if not required_tables.issubset(actual_tables):
        raise RuntimeError("SQLite backup is missing required application tables")
    with open(dump_sql, "w", encoding="utf-8") as out:
        for line in dst.iterdump():
            out.write(line + "\n")
finally:
    dst.close()
    src.close()
PY
}

make_project_manifest() {
    # Archive files and symlinks only. A live SQLite database changes the root
    # directory timestamp while tar reads it, making tar exit with code 1.
    # The database itself is captured above through SQLite's backup API.
    (
        cd "$PROJECT_PARENT"
        find "$PROJECT_NAME" \
            \( -type d \( -name __pycache__ -o -name .pytest_cache -o \
                -name .mypy_cache -o -name .ruff_cache -o -name node_modules -o \
                -name .venv -o -name venv -o -name tmp -o -name temp \) -prune \) -o \
            \( \( -type f -o -type l \) \
                ! -path "$PROJECT_NAME/users.db" \
                ! -path "$PROJECT_NAME/users.db-*" \
                ! -path "$DB_ARCHIVE_PATH" ! -path "$DB_ARCHIVE_PATH-*" \
                ! -name '*.instance.lock' ! -name '.backup_status.json' -print0 \)
    ) >"$WORK_DIR/project-files.list"
}

main() {
    local timestamp archive attempt
    WORK_DIR="$(mktemp -d /tmp/backup_my_project.XXXXXX)"
    mkdir -p "$WORK_DIR/backup_extra/sqlite" "$WORK_DIR/backup_extra/docker"

    timestamp="$(date '+%Y-%m-%d_%H-%M-%S_%N')"
    archive="$BACKUP_DIR/project_backup_${timestamp}.tar.gz"
    TMP_ARCHIVE="${archive}.tmp"
    log "Backup started: $archive"

    create_sqlite_backup
    log "SQLite snapshot passed quick_check"

    docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' \
        >"$WORK_DIR/backup_extra/docker/docker-ps.txt" 2>/dev/null || true
    docker compose -f "$PROJECT_DIR/docker-compose.yml" ps \
        >"$WORK_DIR/backup_extra/docker/docker-compose-ps.txt" 2>/dev/null || true
    docker inspect myvlessbottg-bot-1 --format '{{json .Mounts}}' \
        >"$WORK_DIR/backup_extra/docker/container-mounts.json" 2>/dev/null || true
    docker volume ls \
        >"$WORK_DIR/backup_extra/docker/docker-volumes.txt" 2>/dev/null || true

    for attempt in 1 2 3; do
        make_project_manifest
        if tar -C "$PROJECT_PARENT" --null -T "$WORK_DIR/project-files.list" \
            -C "$WORK_DIR" backup_extra -czf "$TMP_ARCHIVE"; then
            break
        fi
        log "Archive attempt $attempt failed while project files changed"
        if [[ "$attempt" == 3 ]]; then
            log "ERROR: no archive was published; previous backups were retained"
            return 1
        fi
        sleep 2
    done

    gzip -t "$TMP_ARCHIVE"
    tar -tzf "$TMP_ARCHIVE" >"$WORK_DIR/archive.list"
    grep -Fqx "$PROJECT_NAME/.env" "$WORK_DIR/archive.list"
    grep -Fqx 'backup_extra/sqlite/users.db' "$WORK_DIR/archive.list"
    mv -f -- "$TMP_ARCHIVE" "$archive"
    TMP_ARCHIVE=""
    sha256sum "$archive" >"${archive}.sha256.tmp"
    mv -f -- "${archive}.sha256.tmp" "${archive}.sha256"
    # Publish the checksum last, then durably flush the completed pair before rotation.
    python3 - "$archive" "${archive}.sha256" <<'PYFLUSH'
import os, sys
from pathlib import Path
for name in sys.argv[1:]:
    with open(name, 'rb') as source:
        os.fsync(source.fileno())
descriptor = os.open(Path(sys.argv[1]).parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PYFLUSH
    log "Backup archive verified: $archive"
    rotate_backups
    python3 - "$DB_FILE" "$PROJECT_DIR/.backup_status.json" "${archive}.sha256" <<'PYRECEIPT'
import datetime, json, os, sys, tempfile
from pathlib import Path
source, destination, checksum = map(Path, sys.argv[1:])
receipt = dict(completed_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
               sha256=checksum.read_text().split()[0])
fd, temporary = tempfile.mkstemp(prefix='.backup-receipt-', dir=destination.parent)
try:
    with os.fdopen(fd, 'w') as out:
        json.dump(receipt, out); out.flush(); os.fsync(out.fileno())
    info = source.stat()
    os.chmod(temporary, 0o600)
    if os.geteuid() == 0:
        os.chown(temporary, info.st_uid, info.st_gid)
    with open(temporary, 'rb') as source:
        os.fsync(source.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
PYRECEIPT
    log "Backup finished successfully"
}

main "$@"

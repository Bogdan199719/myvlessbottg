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
DB_FILE="${PROJECT_DIR}/users.db"

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
    local old_backup
    mapfile -t old_backups < <(
        find "$BACKUP_DIR" -maxdepth 1 -type f -name 'project_backup_*.tar.gz' \
            -printf '%T@ %p\n' | sort -nr | awk -v keep="$RETENTION_COUNT" 'NR > keep {print $2}'
    )
    for old_backup in "${old_backups[@]}"; do
        rm -f -- "$old_backup" "${old_backup}.sha256"
        log "Rotation: deleted old completed backup $old_backup"
    done
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
                ! -path "$PROJECT_NAME/users.db-*" -print0 \)
    ) >"$WORK_DIR/project-files.list"
}

main() {
    local timestamp archive attempt
    WORK_DIR="$(mktemp -d /tmp/backup_my_project.XXXXXX)"
    mkdir -p "$WORK_DIR/backup_extra/sqlite" "$WORK_DIR/backup_extra/docker"

    timestamp="$(date '+%Y-%m-%d_%H-%M')"
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
    sha256sum "$archive" >"${archive}.sha256"
    log "Backup archive verified: $archive"
    rotate_backups
    log "Backup finished successfully"
}

main "$@"

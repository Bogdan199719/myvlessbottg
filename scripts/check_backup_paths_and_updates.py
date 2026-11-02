"""Custom DBs, inert .env parsing, verified retention and fail-closed updates."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def fixture(path, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        for table in ('users', 'vpn_keys', 'transactions', 'bot_settings'):
            conn.execute(f'CREATE TABLE {table} (value TEXT)')
        conn.execute('CREATE TABLE marker (value TEXT)')
        conn.execute('INSERT INTO marker VALUES (?)', (marker,))


def main():
    with tempfile.TemporaryDirectory(prefix='backup-paths-') as temporary:
        root = Path(temporary)
        project = root / 'project'; project.mkdir()
        output = root / 'archives with spaces'
        database = project / 'state with spaces' / 'active.sqlite'
        fixture(database, 'active-custom')
        fixture(project / 'users.db', 'inactive-default')
        sentinel = root / 'MUST_NOT_EXIST'
        (project / '.env').write_text(
            "DB_PATH='state with spaces/active.sqlite' # inert path\n"
            + 'UNRELATED=$(touch ' + str(sentinel) + ')\n'
        )
        (project / '.shop_bot.instance.lock').touch()
        env = dict(os.environ)
        env.pop('DB_PATH', None); env.pop('BACKUP_DB_PATH', None)
        env.update(BACKUP_PROJECT_DIR=str(project), BACKUP_OUTPUT_DIR=str(output),
                   BACKUP_LOG_FILE=str(root / 'backup.log'), BACKUP_RETENTION_COUNT='2')
        for i in range(3):
            subprocess.run(['bash', str(ROOT / 'scripts/backup_project.sh')], env=env, check=True)
        archives = list(output.glob('project_backup_*.tar.gz'))
        assert len(archives) == 2, 'Rapid backups must not overwrite and verified retention must work'
        assert not sentinel.exists(), '.env must never be sourced'
        latest = max(archives, key=lambda path: path.stat().st_mtime_ns)
        assert latest.stat().st_mode & 0o777 == 0o600
        assert output.stat().st_mode & 0o777 == 0o700
        receipt = json.loads((project / '.backup_status.json').read_text())
        assert receipt['sha256'] == hashlib.sha256(latest.read_bytes()).hexdigest()
        with tarfile.open(latest) as archive:
            assert 'project/state with spaces/active.sqlite' not in archive.getnames()
            assert 'project/.shop_bot.instance.lock' not in archive.getnames()
            snapshot = root / 'snapshot.db'
            snapshot.write_bytes(archive.extractfile('backup_extra/sqlite/users.db').read())
        with sqlite3.connect(snapshot) as conn:
            assert conn.execute('SELECT value FROM marker').fetchone() == ('active-custom',)
        # An incomplete/corrupt artifact must neither count toward retention nor be deleted.
        corrupt = output / 'project_backup_corrupt.tar.gz'
        corrupt.write_bytes(b'synthetic incomplete archive')
        Path(str(corrupt) + '.sha256').write_text('0' * 64 + '\n')
        subprocess.run(['bash', str(ROOT / 'scripts/backup_project.sh')], env=env, check=True)
        assert corrupt.exists()
        assert len(list(output.glob('project_backup_*.tar.gz'))) == 3
        # Interpolations must fail closed instead of selecting the wrong source.
        (project / '.env').write_text('DB_PATH="${UNSET_VALUE}/users.db"\n')
        result = subprocess.run(['bash', str(ROOT / 'scripts/backup_project.sh')], env=env, capture_output=True)
        assert result.returncode != 0
        # Explicit override is independent of unrelated shell-looking .env values.
        env['BACKUP_DB_PATH'] = str(database)
        subprocess.run(['bash', str(ROOT / 'scripts/backup_project.sh')], env=env, check=True)
        # Publication failure must not advance success or rotate known-good copies.
        previous_receipt = (project / '.backup_status.json').read_bytes()
        previous_archives = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in output.glob('project_backup_*.tar.gz')}
        # BASH_ENV injects a shell function without executing a file from a noexec tmpfs.
        fault = root / 'fail-publish.bash'
        fault.write_text('sha256sum() { return 1; }\n')
        failed_env = dict(env, BASH_ENV=str(fault))
        failed = subprocess.run(['bash', str(ROOT / 'scripts/backup_project.sh')], env=failed_env, capture_output=True)
        assert failed.returncode != 0
        assert (project / '.backup_status.json').read_bytes() == previous_receipt
        assert all(path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == checksum
                   for path, checksum in previous_archives.items())
    from shop_bot.utils import update_manager
    with patch.object(update_manager.subprocess, 'run', side_effect=AssertionError('Live update command forbidden')), patch.object(update_manager.os, '_exit', side_effect=AssertionError('Restart forbidden')):
        for flag in ('false', 'true', '1'):
            with patch.dict(os.environ, {'ENABLE_WEB_UPDATES': flag}):
                assert update_manager.perform_update()['status'] == 'error'
    print('Backup custom paths, retention and safe updates: OK')


if __name__ == '__main__':
    main()

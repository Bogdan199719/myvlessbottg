"""Synthetic application backup -> staged restore -> actual isolated process boot.

Must run in a Docker --network none fixture; no production DB/config is accepted.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    interfaces = {line.split(':', 1)[0].strip()
                  for line in Path('/proc/net/dev').read_text().splitlines()[2:]}
    assert interfaces == {'lo'}, 'Explicit network-none container required'
    with tempfile.TemporaryDirectory(prefix='full-restore-') as temporary:
        root = Path(temporary); project = root / 'project'; project.mkdir()
        shutil.copytree(ROOT / 'src', project / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        for name in ['pyproject.toml', 'README.md', 'Dockerfile', 'docker-compose.yml']:
            shutil.copy2(ROOT / name, project / name)
        original_env = b'TEST_ONLY=synthetic-restore\n'
        (project / '.env').write_bytes(original_env)
        os.environ['DB_PATH'] = str(project / 'users.db')
        from shop_bot.data_manager import database as db
        from shop_bot.webhook_server.restore_manager import stage_pending_restore, apply_pending_restore
        db.initialize_db()
        db.register_user_if_not_exists(101, 'synthetic-user', None)
        db.create_host('disabled-fixture', 'https://fixture.example.invalid', 'test', 'test', 1)
        with sqlite3.connect(db.DB_FILE) as conn:
            conn.execute('UPDATE xui_hosts SET is_enabled=0')
            conn.execute('UPDATE users SET total_spent=100, total_months=1 WHERE telegram_id=101')
            conn.execute("INSERT INTO transactions(payment_id,user_id,status,amount_rub,metadata,created_date,paid_date) VALUES('synthetic-paid',101,'paid',100,'{}',datetime('now'),datetime('now'))")
            conn.execute("""INSERT INTO vpn_keys(user_id,host_name,xui_client_uuid,key_email,expiry_date,
                created_date,connection_string,plan_id,service_type)
                VALUES(101,'disabled-fixture','synthetic-uuid','synthetic-key','2099-01-01T00:00:00+03:00',
                datetime('now'),'vless://synthetic@example.invalid:443',0,'xui')""")
        with sqlite3.connect(db.DB_FILE) as conn:
            expected = {
                table: conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                for table in ['users', 'vpn_keys', 'transactions']
            }
        output = root / 'archives'
        env = dict(os.environ, BACKUP_PROJECT_DIR=str(project), BACKUP_DB_PATH=str(project / 'users.db'),
                   BACKUP_OUTPUT_DIR=str(output), BACKUP_LOG_FILE=str(root / 'backup.log'))
        subprocess.run(['bash', str(ROOT / 'scripts/backup_project.sh')], env=env, check=True)
        archive_path = next(output.glob('project_backup_*.tar.gz'))
        expected_hash = Path(str(archive_path) + '.sha256').read_text().split()[0]
        assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == expected_hash
        restored = root / 'restored'; restored.mkdir()
        with tarfile.open(archive_path) as archive:
            archive.extractall(restored, filter='data')
        restored_project = restored / 'project'
        snapshot = restored / 'backup_extra/sqlite/users.db'
        assert not (restored_project / 'users.db').exists()
        staged_env = root / 'incoming.env'; staged_env.write_bytes(original_env)
        (restored_project / '.env').write_text('TEST_ONLY=old-state\n')
        with sqlite3.connect(restored_project / 'users.db') as conn:
            conn.execute('CREATE TABLE old_marker(value TEXT)')
            conn.execute("INSERT INTO old_marker VALUES('old')")
        stage_pending_restore(restored_project, snapshot, staged_env)
        result = apply_pending_restore(restored_project, restored_project / 'users.db', restored_project / '.env')
        assert result['rollback_path'] and Path(result['rollback_path']).is_file()
        assert result['env_rollback_path'] and Path(result['env_rollback_path']).read_text() == 'TEST_ONLY=old-state\n'
        assert (restored_project / '.env').read_bytes() == original_env
        assert not (restored_project / '.restore_pending').exists()
        assert apply_pending_restore(restored_project, restored_project / 'users.db', restored_project / '.env') is None
        with sqlite3.connect(restored_project / 'users.db') as conn:
            assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            for table, rows in expected.items():
                assert conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall() == rows
        for name in ['users.db', '.env']:
            assert (restored_project / name).stat().st_mode & 0o777 == 0o600
        child_env = dict(os.environ, PYTHONPATH=str(restored_project / 'src'),
                         DB_PATH=str(restored_project / 'users.db'))
        with (root / 'smoke.log').open('w+') as log:
            process = subprocess.Popen([sys.executable, '-B', '-m', 'shop_bot'],
                                       cwd=restored_project, env=child_env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    assert process.poll() is None, 'Restored application exited before health check'
                    try:
                        with urllib.request.urlopen('http://127.0.0.1:1488/healthz', timeout=1) as response:
                            data = json.load(response)
                        cycles = data['runtime']['cycles']
                        names = {'payments', 'subscriptions', 'notifications'}
                        if names <= cycles.keys() and all(cycles[name]['success_age_seconds'] is not None for name in names):
                            assert data['status'] == 'ok' and not data['runtime']['failed']
                            break
                    except OSError:
                        pass
                    time.sleep(0.5)
                else:
                    log.flush(); log.seek(0)
                    print(log.read()[-2500:], flush=True)
                    raise AssertionError('Restored application did not finish synthetic background passes')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            with sqlite3.connect(restored_project / 'users.db') as conn:
                assert conn.execute("SELECT amount_rub,status FROM transactions WHERE payment_id='synthetic-paid'").fetchone() == (100, 'paid')
                assert conn.execute('SELECT expiry_date,xui_client_uuid FROM vpn_keys').fetchone() == ('2099-01-01T00:00:00+03:00', 'synthetic-uuid')
        print('Full backup/restore and isolated application startup: OK')


if __name__ == '__main__':
    main()

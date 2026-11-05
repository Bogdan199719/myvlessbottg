"""Real OS locks: two processes, aliases, alternate DBs and crash release."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from shop_bot.utils.process_lock import acquire_instance_lock, InstanceAlreadyRunning

def contender(db, root):
    code = "from shop_bot.utils.process_lock import acquire_instance_lock,InstanceAlreadyRunning; import sys\ntry: h=acquire_instance_lock(sys.argv[1],sys.argv[2])\nexcept InstanceAlreadyRunning: sys.exit(73)\n"
    return subprocess.run([sys.executable, '-B', '-c', code, str(db),str(root)], env=dict(os.environ,PYTHONPATH=str(ROOT/'src'))).returncode

with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary);other=root/'other';other.mkdir();db=root/'users.db';db.touch()
    holder=acquire_instance_lock(db,root)
    assert contender(db,root)==73
    assert contender(db,other)==73, 'Shared DB must fence different checkouts'
    assert contender(root/'different.db',root)==73, 'Project lock must fence different DB paths'
    alias=root/'alias.db';alias.symlink_to(db)
    assert contender(alias,other)==73
    holder.close();assert contender(db,root)==0
    code="from shop_bot.utils.process_lock import acquire_instance_lock; import sys,time; h=acquire_instance_lock(sys.argv[1],sys.argv[2]); print('ready',flush=True);time.sleep(60)"
    p=subprocess.Popen([sys.executable,'-B','-c',code,str(db),str(root)],stdout=subprocess.PIPE,text=True,env=dict(os.environ,PYTHONPATH=str(ROOT/'src')))
    try:
        assert p.stdout.readline().strip()=='ready'
        assert contender(db,root)==73
    finally:
        p.kill();p.wait()
    assert contender(db,root)==0,'OS must release lock after crash; do not unlink lock files'
source=(ROOT/'src/shop_bot/__main__.py').read_text()
assert source.index('_INSTANCE_LOCK = acquire_instance_lock(')<source.index('    _restore_result = apply_pending_restore(')<source.index('from shop_bot.webhook_server.app import')
print('Process instance lock: OK')

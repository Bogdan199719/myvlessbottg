"""Recovery must not steal active users; races preserve CAS and financial state."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

with tempfile.TemporaryDirectory(prefix='recovery-owner-') as temporary:
    path = Path(temporary)/'check.db'
    os.environ['DB_PATH'] = str(path)
    from shop_bot.data_manager import database as db
    from shop_bot.utils import time_utils
    real_connect = sqlite3.connect
    with real_connect(path) as conn:
        conn.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE users(telegram_id INTEGER PRIMARY KEY, username TEXT,
          total_spent REAL,total_months INTEGER,referral_balance REAL,
          referral_balance_all REAL,pending_payment INTEGER);
        CREATE TABLE transactions(payment_id TEXT PRIMARY KEY,user_id INTEGER,
          status TEXT,metadata TEXT,created_date TEXT);
        INSERT INTO users VALUES(101,'synthetic',300,3,20,30,0);
        ''')
    old = (time_utils.get_msk_now()-timedelta(hours=1)).isoformat()
    def clean():
        db.clear_all_pending_payments()
        with real_connect(path) as c:c.execute('DELETE FROM transactions')
    def put(pid='order', target=2_000_000_000_000, attempt=None, raw=None):
        metadata=dict(provider_payment_id=pid,user_id=101,processing_attempt_id=attempt or uuid.uuid4().hex,processing_started_at=old)
        if target is not None:metadata['fulfillment_target_expiry_ms']=target
        with real_connect(path) as c:
            c.execute("INSERT INTO transactions VALUES(?,101,'processing',?,?)",(pid,raw if raw is not None else json.dumps(metadata),old))
        return metadata
    def row(pid='order'):
        with real_connect(path) as c:return c.execute('SELECT status,metadata FROM transactions WHERE payment_id=?',(pid,)).fetchone()
    def finances():
        with real_connect(path) as c:return c.execute('SELECT total_spent,total_months,referral_balance,referral_balance_all FROM users').fetchone()
    money = finances()
    # Exact owner, different currently-active order, and None legacy owner all fence recovery.
    for owner in [uuid.uuid4().hex, None]:
        clean();metadata=put();before=row()
        assert db.set_pending_payment(101,True,owner=owner)
        assert db.recover_stale_processing_transactions()==0
        assert row()==before and finances()==money
        assert db.get_payment_recovery_diagnostics()['stale_active']==1
        assert db.set_pending_payment(101,False,owner=owner)
        assert db.recover_stale_processing_transactions()==1
        status,raw=row();updated=json.loads(raw)
        assert status=='pending' and updated['fulfillment_target_expiry_ms']==metadata['fulfillment_target_expiry_ms']
        assert updated['processing_attempt_id']==metadata['processing_attempt_id']
        assert db.recover_stale_processing_transactions()==0
    # Ambiguous orders are counted but remain untouched.
    clean()
    for i,target in enumerate([None,True,-1,'NaN','1.5',{'bad':'target'},10**30,2_000_000_000_000.0]):put(str(i),target=target)
    put('bad-json',raw='{bad-json')
    before={str(i):row(str(i)) for i in range(8)}
    counts=db.get_payment_recovery_diagnostics()
    assert counts==dict(stale_active=0,stale_recoverable=0,stale_manual_review=8,invalid_processing_state=1),counts
    assert db.recover_stale_processing_transactions()==0
    assert all(row(pid)==value for pid,value in before.items())
    assert finances()==money
    clean();put('historical-string',target='2000000000000')
    assert db.get_payment_recovery_diagnostics()['stale_recoverable']==1
    assert db.recover_stale_processing_transactions()==1
    assert json.loads(row('historical-string')[1])['fulfillment_target_expiry_ms']=='2000000000000'
    # Acquisition wins first: a worker stays registered while recovery runs on another thread.
    clean();metadata=put();held=threading.Event();release=threading.Event()
    def active_worker():
        assert db.set_pending_payment(101,True,owner=metadata['processing_attempt_id'])
        held.set();assert release.wait(5)
        assert db.set_pending_payment(101,False,owner=metadata['processing_attempt_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker=pool.submit(active_worker)
        try:
            assert held.wait(3)
            assert pool.submit(db.recover_stale_processing_transactions).result(3)==0
            assert row()[0]=='processing'
        finally:release.set();worker.result(3)
    assert db.recover_stale_processing_transactions()==1
    # Recovery wins first: it holds owner-lock from selection through commit, so
    # flag acquisition cannot slip between the ownership decision and UPDATE.
    clean();metadata=put();selected=threading.Event();continue_scan=threading.Event();acquired=threading.Event()
    class Cursor:
        def __init__(self,cursor):self.cursor=cursor;self.recovery_select=False
        def execute(self,sql,*args):
            self.recovery_select='SELECT payment_id, user_id, metadata, created_date' in sql
            self.cursor.execute(sql,*args);return self
        def fetchall(self):
            values=self.cursor.fetchall()
            if self.recovery_select:
                selected.set();assert continue_scan.wait(5)
            return values
        def __getattr__(self,name):return getattr(self.cursor,name)
    class Connection:
        def __init__(self,conn):object.__setattr__(self,'conn',conn)
        def __enter__(self):self.conn.__enter__();return self
        def __exit__(self,*args):return self.conn.__exit__(*args)
        def __getattr__(self,name):return getattr(self.conn,name)
        def __setattr__(self,name,value):setattr(self.conn,name,value)
        def cursor(self):return Cursor(self.conn.cursor())
    def connect(*a,**kw):return Connection(real_connect(*a,**kw))
    def acquire_after_selection():
        result=db.set_pending_payment(101,True,owner=metadata['processing_attempt_id'])
        acquired.set();return result
    with ThreadPoolExecutor(max_workers=2) as pool,patch.object(db.sqlite3,'connect',connect):
        recovery=pool.submit(db.recover_stale_processing_transactions)
        try:
            assert selected.wait(3)
            acquisition=pool.submit(acquire_after_selection)
            assert not acquired.wait(.15),'Acquisition entered recovery critical section'
        finally:continue_scan.set()
        assert recovery.result(3)==1
        assert acquisition.result(3)
    assert row()[0]=='pending'
    assert db.set_pending_payment(101,False,owner=metadata['processing_attempt_id'])
    assert finances()==money
    # Diagnostics never create a missing database, expose identities or modify rows.
    clean();metadata=put();before=row();assert db.get_payment_recovery_diagnostics()['stale_recoverable']==1
    assert row()==before and 'order' not in json.dumps(db.get_payment_recovery_diagnostics())
    missing=Path(temporary)/'absent.db'
    with patch.object(db,'DB_FILE',missing):assert db.get_payment_recovery_diagnostics()['unavailable']
    assert not missing.exists()
print('Payment recovery active-owner/CAS diagnostics: OK')

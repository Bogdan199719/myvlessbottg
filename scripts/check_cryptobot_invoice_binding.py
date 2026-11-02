"""Binding races, legacy compatibility, cross-provider rejection and duplicate IDs."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

with tempfile.TemporaryDirectory(prefix='crypto-binding-') as tmp:
    os.environ['DB_PATH']=str(Path(tmp)/'check.db')
    os.environ['FLASK_SECRET_KEY']='synthetic-binding-secret-'+'x'*40
    with patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')):
        from shop_bot.data_manager import database as db
        from shop_bot.webhook_server.app import create_webhook_app
        from shop_bot.webhook_server import app as web
        db.initialize_db();db.register_user_if_not_exists(1,'fixture',None)
        token='synthetic-key';db.update_setting('cryptobot_token',token)
        app=create_webhook_app(SimpleNamespace(get_bot_instance=lambda:None))
        client=app.test_client()
        def create(pid,version=1,method='CryptoBot'):
            meta=dict(user_id=1,price=100,months=1,payment_method=method,service_type='xui',plan_id=1,host_name='ALL',action='new')
            if version:meta.update(cryptobot_binding_version=1,cryptobot_binding_state='creating')
            assert db.create_pending_transaction(pid,1,100,meta)
        def row(pid):
            with sqlite3.connect(db.DB_FILE) as c:return c.execute('SELECT status,metadata,payment_method FROM transactions WHERE payment_id=?',(pid,)).fetchone()
        def post(pid,iid,amount='100.00'):
            raw=json.dumps({'update_type':'invoice_paid','payload':dict(status='paid',invoice_id=iid,asset='RUB',currency_type='fiat',amount=amount,payload=json.dumps({'tx_id':pid}))}).encode()
            signature=hmac.new(hashlib.sha256(token.encode()).digest(),raw,hashlib.sha256).hexdigest()
            return client.post('/cryptobot-webhook',data=raw,content_type='application/json',headers={'crypto-pay-api-signature':signature})
        create('creating');before=row('creating')
        assert post('creating',101).status_code==503
        assert row('creating')==before,'Early callback must not reserve or change order'
        assert db.bind_cryptobot_invoice('creating',101)
        assert db.bind_cryptobot_invoice('creating','101')
        assert not db.bind_cryptobot_invoice('creating',102)
        before=row('creating')
        assert post('creating',102).status_code==400 and row('creating')==before
        assert post('creating',101).status_code==503  # signed receipt persisted, bot intentionally unavailable
        state,raw,method=row('creating');metadata=json.loads(raw)
        assert state=='pending' and metadata['payment_confirmation_verified'] is True
        assert metadata['cryptobot_invoice_id']=='101'
        assert post('creating',101).status_code==503  # repeat must not issue/account without bot
        create('foreign',None,'Telegram Stars');before=row('foreign')
        assert post('foreign',201).status_code==400 and row('foreign')==before
        assert db.reserve_pending_transaction('foreign',payment_method='CryptoBot',expected_cryptobot_invoice_id='201') is None
        assert row('foreign')==before
        assert not web._is_webhook_processed('cryptobot','201')
        create('foreign-paid',None,'Telegram Stars')
        with sqlite3.connect(db.DB_FILE) as c:
            c.execute("UPDATE transactions SET status='paid' WHERE payment_id='foreign-paid'")
        before=row('foreign-paid')
        assert post('foreign-paid',211).status_code==400 and row('foreign-paid')==before
        assert not web._is_webhook_processed('cryptobot','211')
        create('legacy',None)
        assert post('legacy',301).status_code==503
        assert json.loads(row('legacy')[1])['payment_confirmation_verified'] is True
        create('legacy-other',None);before=row('legacy-other')
        assert post('legacy-other',301).status_code==400 and row('legacy-other')==before
        create('new-other');assert not db.bind_cryptobot_invoice('new-other',101)
        # Duplicate historical provider IDs are ambiguous and excluded from automatic retries.
        create('duplicate-a',None);create('duplicate-b',None)
        with sqlite3.connect(db.DB_FILE) as c:
            for pid in ['duplicate-a','duplicate-b']:
                meta=json.loads(row(pid)[1]);meta.update(provider_payment_id=pid,cryptobot_invoice_id='shared-id',payment_confirmation_verified=True)
                c.execute("UPDATE transactions SET metadata=?,payment_method='CryptoBot',currency_name='RUB',amount_currency=100 WHERE payment_id=?",(json.dumps(meta),pid))
        queued={r['payment_id'] for r in db.get_pending_paid_retry_transactions(100)}
        assert not {'duplicate-a','duplicate-b'} & queued
        assert db.reserve_pending_transaction('duplicate-a',preserve_fulfillment_state=True,require_paid_confirmation=True) is None
        create('race-a',None);create('race-b',None)
        from concurrent.futures import ThreadPoolExecutor
        def reserve(pid):
            return db.reserve_pending_transaction(pid,payment_method='CryptoBot',expected_cryptobot_invoice_id='racing-invoice',expected_cryptobot_amount='100')
        with ThreadPoolExecutor(max_workers=2) as pool:
            reserved=list(pool.map(reserve,['race-a','race-b']))
        assert sum(item is not None for item in reserved)==1,'Provider invoice claimed twice'
        create('canonical-a',None);create('canonical-b',None)
        assert post('canonical-a','\t canonical-id \n').status_code==503
        assert json.loads(row('canonical-a')[1])['cryptobot_invoice_id']=='canonical-id'
        assert post('canonical-b','canonical-id').status_code==400
        create('canonical-new')
        assert not db.bind_cryptobot_invoice('canonical-new',' canonical-id ')
        assert db._cryptobot_identity({'invoice':1}) is None
        assert db._cryptobot_identity(1.0) is None
        create('historical-spaces',None);create('historical-spaces-other',None)
        historical=json.loads(row('historical-spaces')[1])
        historical['cryptobot_invoice_id']='\tlegacy-spaces\n'
        with sqlite3.connect(db.DB_FILE) as c:
            c.execute('UPDATE transactions SET metadata=? WHERE payment_id=?',(json.dumps(historical),'historical-spaces'))
        assert post('historical-spaces-other','legacy-spaces').status_code==400

        # Omitted or forged caller fields must not erase/reassign durable ownership.
        create('durable')
        assert db.bind_cryptobot_invoice('durable','durable-id')
        first=db.reserve_pending_transaction('durable',payment_method='CryptoBot',expected_cryptobot_invoice_id='durable-id',expected_cryptobot_amount='100')
        assert first is not None
        omitted={k:v for k,v in first.items() if not k.startswith('cryptobot_')}
        omitted['payment_confirmation_verified']=True
        assert db.update_reserved_transaction_metadata('durable',omitted)
        saved=json.loads(row('durable')[1])
        assert saved['cryptobot_invoice_id']=='durable-id' and saved['cryptobot_binding_version']==1 and saved['cryptobot_binding_state']=='bound'
        assert db.finalize_reserved_transaction('durable',success=False,metadata=omitted)
        saved=json.loads(row('durable')[1]);assert saved['cryptobot_invoice_id']=='durable-id'
        caller=dict(saved,cryptobot_invoice_id='forged',cryptobot_binding_version=0,cryptobot_binding_state='creating')
        second=db.reserve_pending_transaction('durable',caller,payment_method='CryptoBot')
        assert second['cryptobot_invoice_id']=='durable-id' and second['cryptobot_binding_version']==1 and second['cryptobot_binding_state']=='bound'
        create('durable-other')
        assert not db.bind_cryptobot_invoice('durable-other','durable-id')
        assert db.finalize_reserved_transaction('durable',success=False,metadata=second)

        for index, broken in enumerate([
            {'cryptobot_binding_version':True,'cryptobot_binding_state':'creating'},
            {'cryptobot_binding_version':'1','cryptobot_binding_state':'creating'},
            {'cryptobot_binding_version':2,'cryptobot_binding_state':'creating'},
            {'cryptobot_binding_version':1,'cryptobot_binding_state':{}},
            {'cryptobot_binding_state':'bound'},
            {'cryptobot_binding_state':'creating'},
            {'cryptobot_binding_version':1,'cryptobot_binding_state':'bound'},
        ]):
            pid='bad-protocol-'+str(index);create(pid,None)
            meta=json.loads(row(pid)[1]);meta.update(broken,provider_payment_id=pid,payment_confirmation_verified=True)
            with sqlite3.connect(db.DB_FILE) as c:
                c.execute('UPDATE transactions SET metadata=? WHERE payment_id=?',(json.dumps(meta),pid))
            before=row(pid)
            assert post(pid,'bad-protocol-invoice-'+str(index)).status_code==400 and row(pid)==before
            assert not db.bind_cryptobot_invoice(pid,'bad-protocol-invoice-'+str(index))
            assert db.reserve_pending_transaction(pid,preserve_fulfillment_state=True,require_paid_confirmation=True) is None

        # No changes to financial totals from any refused/queued callback.
        assert db.get_user(1)['total_spent']==0
print('CryptoBot invoice binding: OK')

#!/usr/bin/env python3
"""Check signed CryptoBot notifications and retry eligibility on disposable data.

Uses the real Flask route, scheduler, fulfillment and financial accounting.
Telegram and panel effects are synthetic; socket connections are forbidden.
"""
from __future__ import annotations

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
import threading
from types import SimpleNamespace
from unittest.mock import patch


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    with tempfile.TemporaryDirectory(prefix='crypto-confirmation-') as temporary:
        db_path = Path(temporary) / 'check.db'
        os.environ['DB_PATH'] = str(db_path)
        os.environ['FLASK_SECRET_KEY'] = 'synthetic-test-secret-' + 'x' * 40
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')), patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('Network forbidden')):
            from shop_bot.data_manager import database as db
            from shop_bot.data_manager import scheduler
            from shop_bot.bot import handlers
            from shop_bot.webhook_server import app as web

            assert Path(db.DB_FILE) == db_path
            db.initialize_db()
            token = 'synthetic-crypto-token'
            for key, value in {
                'cryptobot_token': token,
                'referral_percentage': '10',
                'enable_admin_payment_notifications': 'false',
                'domain': 'vpn.example.invalid',
            }.items():
                db.update_setting(key, value)
            db.register_user_if_not_exists(1, 'synthetic-buyer', None)
            db.register_user_if_not_exists(2, 'synthetic-referrer', None)
            with sqlite3.connect(db_path) as conn:
                conn.execute('UPDATE users SET referred_by=2 WHERE telegram_id=1')
            db.create_host('synthetic-host', 'https://panel.example.invalid', 'test', 'test', 1)
            db.create_plan('ALL', 'Synthetic', 1, 100)
            plan_id = db.get_plans_for_host('ALL')[0]['plan_id']

            class Message:
                async def edit_text(self, *args, **kwargs):
                    pass

                async def delete(self, *args, **kwargs):
                    pass

            class Bot:
                async def send_message(self, *args, **kwargs):
                    return Message()

                async def delete_message(self, *args, **kwargs):
                    pass

            bot = Bot()
            controller = SimpleNamespace(get_bot_instance=lambda: None)
            app = web.create_webhook_app(controller)
            app.config['TESTING'] = True
            client = app.test_client()
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run_loop():
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            thread = threading.Thread(target=run_loop, daemon=True)
            thread.start()
            assert ready.wait(5)
            app.config['EVENT_LOOP'] = loop
            panel_calls = []

            def invoice(pid):
                metadata = dict(user_id=1, months=1, price=100, action='new', key_id=0,
                                host_name='ALL', plan_id=plan_id, plan_name='Synthetic',
                                service_type='xui', payment_method='CryptoBot')
                assert db.create_pending_transaction(pid, 1, 100, metadata)
                assert 'payment_confirmation_verified' not in metadata
                assert state(pid)[1]['payment_confirmation_verified'] is False

            def state(pid):
                with sqlite3.connect(db_path) as conn:
                    row = conn.execute('SELECT status,metadata FROM transactions WHERE payment_id=?', (pid,)).fetchone()
                return row[0], json.loads(row[1])

            def queued_ids():
                return {row['payment_id'] for row in db.get_pending_paid_retry_transactions(100)}

            def post(pid, amount='100.00', *, omit_amount=False, signature=True, fiat_only=False):
                payload = dict(status='paid', invoice_id='invoice-' + pid, asset='RUB',
                               currency_type='fiat', payload=json.dumps({'tx_id': pid}))
                if not omit_amount:
                    payload['amount'] = amount
                if fiat_only:
                    payload.pop('asset')
                    payload['fiat'] = 'RUB'
                raw = json.dumps({'update_type': 'invoice_paid', 'payload': payload}).encode()
                signing_key = hashlib.sha256(token.encode()).digest()
                digest = hmac.new(signing_key, raw, hashlib.sha256).hexdigest()
                return client.post('/cryptobot-webhook', data=raw, content_type='application/json',
                    headers={'crypto-pay-api-signature': digest if signature else 'invalid'})

            def finances():
                with sqlite3.connect(db_path) as conn:
                    buyer = conn.execute('SELECT total_spent,total_months FROM users WHERE telegram_id=1').fetchone()
                    reward = conn.execute('SELECT referral_balance FROM users WHERE telegram_id=2').fetchone()[0]
                return (*buyer, reward)

            def legacy(pid, *, amount='100.00', price=100, currency='RUB', marker='absent', invoice_id='legacy-invoice', method='CryptoBot'):
                invoice(pid)
                metadata = state(pid)[1]
                metadata.update(provider_payment_id=pid, cryptobot_invoice_id=invoice_id, price=price)
                if marker == 'absent':
                    metadata.pop('payment_confirmation_verified')
                else:
                    metadata['payment_confirmation_verified'] = marker
                with sqlite3.connect(db_path) as conn:
                    conn.execute('UPDATE transactions SET metadata=?,payment_method=?,amount_currency=?,currency_name=? WHERE payment_id=?',
                        (json.dumps(metadata), method, amount, currency, pid))

            async def absolute_key(**kwargs):
                panel_calls.append(dict(kwargs))
                return dict(host_name=kwargs['host_name'], email=kwargs['email'],
                            client_uuid='synthetic-client', expiry_timestamp_ms=kwargs['target_expiry_ms'],
                            connection_string='vless://synthetic@example.invalid:443')

            def run_retry():
                future = asyncio.run_coroutine_threadsafe(scheduler.process_pending_paid_provider_retries(bot), loop)
                future.result(timeout=20)

            try:
                invoice('bad-signature')
                assert post('bad-signature', signature=False).status_code == 403
                assert state('bad-signature')[0] == 'pending'
                invalid = [('mismatch', '50.00'), ('above-tolerance', '101.01'), ('below-tolerance', '98.99'), ('zero', '0'), ('negative', '-1'),
                           ('nan', 'NaN'), ('inf', 'Infinity'), ('bad-text', 'bad'),
                           ('null', None), ('bool', True), ('list', []),
                           ('object', {}), ('float-inf', float('inf')),
                           ('missing', None)]
                for suffix, amount in invalid:
                    pid = 'invalid-' + suffix
                    invoice(pid)
                    assert post(pid, amount, omit_amount=suffix == 'missing').status_code == 400, suffix
                    status, metadata = state(pid)
                    assert status == 'pending' and metadata['payment_confirmation_verified'] is False, suffix
                    assert pid not in queued_ids(), suffix
                assert finances() == (0, 0, 0)

                invoice('proof-persist-fails')
                with patch.object(web, 'update_reserved_transaction_metadata', return_value=False), patch.object(handlers, 'process_successful_payment', side_effect=AssertionError('Unpersisted proof reached fulfillment')) as blocked_handler:
                    assert post('proof-persist-fails').status_code == 503
                    assert blocked_handler.call_count == 0
                assert state('proof-persist-fails')[0] == 'pending'
                assert state('proof-persist-fails')[1]['payment_confirmation_verified'] is False
                assert 'proof-persist-fails' not in queued_ids()

                for pid, amount in (('valid-tolerance', '101.00'), ('valid-low-tolerance', '99.00')):
                    invoice(pid)
                    assert post(pid, amount, fiat_only=True).status_code == 503
                    assert state(pid)[1]['payment_confirmation_verified'] is True
                    assert pid in queued_ids()
                for pid, amount in (('invalid-fiat-low', '98.99'), ('invalid-fiat-high', '101.01')):
                    invoice(pid)
                    assert post(pid, amount, fiat_only=True).status_code == 400
                    assert state(pid)[1]['payment_confirmation_verified'] is False
                    assert pid not in queued_ids()

                # The actual handler and monetary mutators may not trust a
                # caller's invented True over the stored False.
                forged_reservation_input = {
                    **state('invalid-mismatch')[1], 'payment_confirmation_verified': True,
                }
                reserved = db.reserve_pending_transaction('invalid-mismatch', forged_reservation_input)
                assert forged_reservation_input['payment_confirmation_verified'] is True
                assert reserved['payment_confirmation_verified'] is False
                omitted_proof = dict(reserved)
                omitted_proof.pop('payment_confirmation_verified')
                assert db.update_reserved_transaction_metadata('invalid-mismatch', omitted_proof)
                assert state('invalid-mismatch')[1]['payment_confirmation_verified'] is False
                forged = {**reserved, 'payment_confirmation_verified': True,
                          'provider_payment_id': 'invalid-mismatch', 'cryptobot_invoice_id': 'forged'}
                assert db.is_reserved_transaction_owner('invalid-mismatch', forged)
                assert not db.is_reserved_transaction_fulfillment_allowed('invalid-mismatch', forged)
                future = asyncio.run_coroutine_threadsafe(handlers.process_successful_payment(bot, forged), loop)
                assert future.result(timeout=20) is False
                missing_provider = dict(forged)
                missing_provider.pop('provider_payment_id')
                future = asyncio.run_coroutine_threadsafe(handlers.process_successful_payment(bot, missing_provider), loop)
                assert future.result(timeout=20) is False
                missing_provider.pop('payment_method')
                future = asyncio.run_coroutine_threadsafe(handlers.process_successful_payment(bot, missing_provider), loop)
                assert future.result(timeout=20) is False
                assert db.apply_payment_accounting_once('invalid-mismatch', 1, 100, 1, 2, 10, forged) is None
                assert not db.finalize_reserved_transaction('invalid-mismatch', success=True, metadata=forged)
                assert finances() == (0, 0, 0)
                assert db.finalize_reserved_transaction('invalid-mismatch', success=False, metadata=reserved)

                legacy('legacy-valid')
                legacy('legacy-tolerance', amount='100.75')
                invalid_legacy = {
                    'legacy-wrong-sum': {'amount': '50'},
                    'legacy-zero': {'amount': '0'},
                    'legacy-no-amount': {'amount': None},
                    'legacy-wrong-currency': {'currency': 'USD'},
                    'legacy-bad-price': {'price': 'NaN'},
                    'legacy-bad-amount': {'amount': 'NaN'},
                    'legacy-huge-amount': {'amount': '1e1000000'},
                    'legacy-huge-price': {'price': '1e1000000'},
                    'legacy-no-invoice': {'invoice_id': ''},
                    'legacy-wrong-method': {'method': 'Unknown'},
                    'explicit-false': {'marker': False},
                    'explicit-string': {'marker': 'true'},
                    'explicit-int': {'marker': 1},
                }
                for pid, kwargs in invalid_legacy.items():
                    legacy(pid, **kwargs)
                assert {'legacy-valid', 'legacy-tolerance'} <= queued_ids()
                assert not (set(invalid_legacy) & queued_ids())
                for pid in invalid_legacy:
                    assert db.reserve_pending_transaction(pid, preserve_fulfillment_state=True, require_paid_confirmation=True) is None

                legacy('legacy-forged-flag')
                legacy_reservation = db.reserve_pending_transaction('legacy-forged-flag', preserve_fulfillment_state=True, require_paid_confirmation=True)
                assert legacy_reservation is not None
                assert db.finalize_reserved_transaction('legacy-forged-flag', success=False,
                    metadata={**legacy_reservation, 'payment_confirmation_verified': True})
                assert 'payment_confirmation_verified' not in state('legacy-forged-flag')[1]

                legacy('legacy-reserve-forged')
                manufactured_proof = {**state('legacy-reserve-forged')[1], 'payment_confirmation_verified': True}
                legacy_unflagged = db.reserve_pending_transaction('legacy-reserve-forged', manufactured_proof)
                assert legacy_unflagged is not None
                assert 'payment_confirmation_verified' not in legacy_unflagged
                assert db.finalize_reserved_transaction('legacy-reserve-forged', success=False, metadata=legacy_unflagged)
                assert 'payment_confirmation_verified' not in state('legacy-reserve-forged')[1]

                for pid, raw_metadata in (
                    ('malformed-json', '{"provider_payment_id":bad'),
                    ('nonobject-json', '[{"provider_payment_id":"synthetic","payment_method":"CryptoBot"}]'),
                ):
                    invoice(pid)
                    with sqlite3.connect(db_path) as conn:
                        conn.execute('UPDATE transactions SET metadata=? WHERE payment_id=?', (raw_metadata, pid))
                    assert pid not in queued_ids()
                    assert db.reserve_pending_transaction(pid, preserve_fulfillment_state=True, require_paid_confirmation=True) is None

                # A valid receipt survives a stopped bot and is fulfilled once.
                invoice('valid-stopped')
                assert post('valid-stopped').status_code == 503
                status, proof = state('valid-stopped')
                assert status == 'pending' and proof['payment_confirmation_verified'] is True
                assert 'valid-stopped' in queued_ids()
                assert post('valid-stopped', '50.00').status_code == 400
                assert state('valid-stopped')[1]['payment_confirmation_verified'] is True
                assert 'valid-stopped' in queued_ids()
                with sqlite3.connect(db_path) as conn:
                    receipt_columns = conn.execute('SELECT amount_currency,currency_name FROM transactions WHERE payment_id=?', ('valid-stopped',)).fetchone()
                assert receipt_columns == (100, 'RUB'), receipt_columns
                controller.get_bot_instance = lambda: bot
                with patch.object(handlers.xui_api, 'create_or_update_key_on_host_absolute_expiry', side_effect=absolute_key), patch.object(handlers.xui_api, 'create_or_update_key_on_host', side_effect=AssertionError('Relative panel mutation forbidden')):
                    snapshot = next(row for row in db.get_pending_paid_retry_transactions(100) if row['payment_id'] == 'valid-stopped')
                    with patch.object(db, 'get_pending_paid_retry_transactions', return_value=[snapshot]):
                        run_retry()
                    assert state('valid-stopped')[0] == 'paid'
                    assert state('valid-stopped')[1]['payment_confirmation_verified'] is True
                    assert state('valid-stopped')[1]['accounting_applied'] is True
                    assert finances() == (100, 1, 10) and len(panel_calls) == 1
                    assert post('valid-stopped').status_code == 200
                    assert finances() == (100, 1, 10) and len(panel_calls) == 1

                    # Queue eligibility is only a snapshot. Recheck fresh proof
                    # while reserving; don't promote a now-revoked receipt.
                    legacy('snapshot-revoked')
                    snapshot = next(row for row in db.get_pending_paid_retry_transactions(100) if row['payment_id'] == 'snapshot-revoked')
                    revoked = state('snapshot-revoked')[1]
                    revoked['payment_confirmation_verified'] = False
                    with sqlite3.connect(db_path) as conn:
                        conn.execute('UPDATE transactions SET metadata=? WHERE payment_id=?', (json.dumps(revoked), 'snapshot-revoked'))
                    with patch.object(db, 'get_pending_paid_retry_transactions', return_value=[snapshot]):
                        run_retry()
                    assert state('snapshot-revoked')[0] == 'pending'
                    assert finances() == (100, 1, 10) and len(panel_calls) == 1
            finally:
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=5)
                assert not thread.is_alive()
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
                loop.close()
    print('CryptoBot confirmation: OK (signed route, legacy receipts, fresh retry proof, real fulfillment/accounting).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

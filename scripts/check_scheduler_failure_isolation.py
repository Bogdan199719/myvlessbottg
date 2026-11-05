"""Failures must not suppress independent steps; cancellation must propagate."""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
os.environ.setdefault('DB_PATH','/tmp/scheduler-test-only.db')
from shop_bot.data_manager import scheduler as s
from shop_bot.utils.runtime_health import RuntimeHealth

async def main():
    monitor=RuntimeHealth();controller=SimpleNamespace(runtime_health=monitor,get_status=lambda:{'is_running':True},get_bot_instance=lambda:object())
    monitor.cycle_started('payments')
    async def failure():raise RuntimeError('synthetic secret')
    async def cancel():raise asyncio.CancelledError()
    assert not await s._run_scheduler_step(controller,'payments','failing',failure)
    assert monitor.snapshot()['cycles']['payments']['step_errors']==['failing']
    try:await s._run_scheduler_step(controller,'payments','cancel',cancel)
    except asyncio.CancelledError:pass
    else:raise AssertionError('Cancellation swallowed')
    ran=[];notification=asyncio.Event();maintenance=asyncio.Event();payment=asyncio.Event()
    async def blocked():maintenance.set();await asyncio.Event().wait()
    async def recover():ran.append('recover');raise RuntimeError('synthetic failure')
    async def yookassa(bot):ran.append('yookassa')
    async def retry(bot):ran.append('retry')
    async def stars():ran.append('stars');payment.set()
    async def expire(bot):raise RuntimeError('one notification step failed')
    async def idle(bot):notification.set()
    with patch.multiple(s,_STARTUP_DELAY_SECONDS=0,enforce_clients_state_from_db=blocked,recover_interrupted_payment_processing=recover,process_pending_yookassa_payments=yookassa,process_pending_paid_provider_retries=retry,expire_stale_unpaid_stars_payments=stars,check_expiring_subscriptions=expire,check_idle_onboarding_users=idle):
        tasks=[asyncio.create_task(fn(controller)) for fn in [s.periodic_subscription_check,s.periodic_payment_check,s.periodic_notification_check]]
        try:
            await asyncio.wait_for(asyncio.gather(maintenance.wait(),payment.wait(),notification.wait()),3)
            assert ran==['recover','yookassa','retry','stars']
            assert monitor.snapshot()['cycles']['payments']['last_cycle_error']
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    print('Scheduler failure isolation: OK')
asyncio.run(main())

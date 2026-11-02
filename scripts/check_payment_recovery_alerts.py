"""Read-only payment diagnostics alert only on persistent manual-review states."""
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from shop_bot.utils import runtime_alerts as alerts
from shop_bot.data_manager import database as db

async def case(summary, transient=False):
    clock=[1000.0];ticks=[0];stored=[]
    bot=SimpleNamespace(send_message=AsyncMock())
    monitor=SimpleNamespace(snapshot=lambda:dict(failed=[],warnings=[],closing=False),payment_recovery_status=lambda value:stored.append(value))
    controller=SimpleNamespace(runtime_health=monitor,get_bot_instance=lambda:bot)
    def get_counts():
        if transient and ticks[0]>=20:
            return dict(stale_active=0,stale_recoverable=0,stale_manual_review=0,invalid_processing_state=0)
        return summary.copy()
    async def tick(seconds):
        ticks[0]+=1;clock[0]+=15
        if ticks[0]>=63:raise asyncio.CancelledError()
    receipt=json.dumps(dict(completed_at_utc=datetime.now(timezone.utc).isoformat()))
    with patch.object(alerts.time,'monotonic',lambda:clock[0]),patch.object(alerts.asyncio,'sleep',tick),patch.object(db,'get_payment_recovery_diagnostics',side_effect=get_counts),patch.object(db,'get_setting',return_value='101'),patch.object(alerts.Path,'read_text',return_value=receipt):
        try:await alerts.runtime_watchdog(controller)
        except asyncio.CancelledError:pass
    assert stored
    return bot.send_message

base=dict(stale_active=0,stale_recoverable=0,stale_manual_review=0,invalid_processing_state=0)
sent=asyncio.run(case({**base,'stale_manual_review':2}))
assert sent.await_count==1
assert 'payment_manual_review_required' in sent.call_args.args[1]
assert '2' not in sent.call_args.args[1], 'Only category, no payment details in alerts'
assert asyncio.run(case({**base,'stale_manual_review':2},transient=True)).await_count==0
assert asyncio.run(case({**base,'stale_active':1})).await_count==0
assert asyncio.run(case({**base,'stale_recoverable':1})).await_count==0
print('Payment recovery diagnostic alerts: OK')

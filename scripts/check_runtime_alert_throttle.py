"""Operational alerts: debounce external errors, throttle and respect shutdown."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from shop_bot.utils import runtime_alerts as a
from shop_bot.data_manager import database as db

async def case(failed,warning):
    now=[1000.0];count=[0]
    snapshot={'failed':failed,'warnings':warning,'closing':False}
    bot=SimpleNamespace(send_message=AsyncMock())
    controller=SimpleNamespace(runtime_health=SimpleNamespace(snapshot=lambda:snapshot),get_bot_instance=lambda:bot)
    async def tick(seconds):
        count[0]+=1;now[0]+=15
        if count[0]==3:raise asyncio.CancelledError()
    with patch.object(a.time,'monotonic',lambda:now[0]),patch.object(a.asyncio,'sleep',tick),patch.object(db,'get_setting',return_value='101'):
        try:await a.runtime_watchdog(controller)
        except asyncio.CancelledError:pass
    return bot.send_message.await_count
assert asyncio.run(case(['payments_stopped'],[]))==1
assert asyncio.run(case([],['payments_cycle_error']))==0
assert asyncio.run(case([],[]))==0
print('Runtime alert debounce/throttle: OK')

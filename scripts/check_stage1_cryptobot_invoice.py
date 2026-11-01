"""Compare RUB invoice creation before/after removing the unused exchange rate."""
import ast
import asyncio
from decimal import Decimal
import json
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import uuid

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "src/shop_bot/bot/handlers.py").read_text()
tree = ast.parse(source)
node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_cryptobot_invoice_handler")
node.decorator_list = []
# Keep the real handler; replace only external dependencies.
module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
assert not any(isinstance(n, ast.Name) and n.id == "get_usdt_rub_rate" for n in ast.walk(node))

async def run(discount, failure=False):
    invoice_calls = []
    stored = []
    failed = []
    state = NS(get_data=AsyncMock(return_value={"plan_id":7,"user_id":101,"host_name":"ALL","action":"new","key_id":0,"customer_email":"fixture@example.invalid"}), clear=AsyncMock())
    callback = NS(answer=AsyncMock(), from_user=NS(id=101), message=NS(edit_text=AsyncMock()))
    price = Decimal("90") if discount else Decimal("100")
    class Crypto:
        def __init__(self, token):
            assert token == "synthetic-token"
        async def create_invoice(self, **kwargs):
            invoice_calls.append(kwargs)
            if failure:
                raise RuntimeError("synthetic creation failure")
            return NS(pay_url="https://fixture.invalid/pay")
    def store(pid, uid, amount, metadata):
        stored.append((pid, uid, amount, metadata.copy()))
        return 1
    ns = dict(logger=logging.getLogger("fixture"), get_user=lambda uid:{"telegram_id":uid}, get_setting=lambda key: "synthetic-token" if key=="cryptobot_token" else (_ for _ in ()).throw(AssertionError("Unused setting requested")), get_plan_by_id=lambda pid:{"months":1}, _validate_payment_state_plan=lambda *a:(True,""), _calculate_plan_quote=lambda *a:(price,discount), _plan_snapshot=lambda p:{"months":1,"service_type":"xui","plan_name":"fixture"}, CryptoPay=Crypto, uuid=uuid, create_pending_transaction=store, _cryptobot_build_payload=lambda pid:json.dumps({"tx_id":pid}), keyboards=NS(create_payment_keyboard=lambda url:url), mark_pending_transaction_status=lambda *a,**kw:failed.append((a,kw)))
    exec(compile(ast.fix_missing_locations(module), "invoice-handler", "exec"), ns)
    await ns[node.name](callback, state)
    assert len(invoice_calls) == len(stored) == 1
    pid, uid, amount, meta = stored[0]
    call = invoice_calls[0]
    assert call == dict(currency_type="fiat", fiat="RUB", amount=float(price), description="Подписка на 1 мес.", payload=json.dumps({"tx_id":pid}), expires_in=3600)
    assert amount == float(price) and uid == 101
    assert meta["price"] == float(price) and meta["referral_discount_applied"] is discount
    assert meta["payment_method"] == "CryptoBot" and meta["host_name"] == "ALL"
    assert meta["action"] == "new" and meta["plan_id"] == 7 and meta["months"] == 1
    assert state.clear.await_count == 1
    assert bool(failed) is failure
    if not failure:
        assert callback.message.edit_text.call_args.kwargs["reply_markup"] == "https://fixture.invalid/pay"

asyncio.run(run(False))
asyncio.run(run(True))
asyncio.run(run(False, True))
print("Stage 1 CryptoBot RUB invoice: OK")

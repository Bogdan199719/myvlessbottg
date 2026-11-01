"""Verify MTG errors never log provider text, JSON or exception payloads."""
import asyncio
import logging
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from shop_bot.modules import mtg_api as mtg

SECRET = "SYNTHETIC_SECRET_IN_RESPONSE"
records = []
class Capture(logging.Handler):
    def emit(self, record):
        records.append(record)

class Response:
    status = 403
    data = {"secret": SECRET, "password": SECRET, "name": "fixture"}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def text(self):
        raise AssertionError("Untrusted response body must not be read for logging")
    async def json(self):
        return self.data

class Session:
    exception = False
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def request(self, *args, **kwargs):
        if self.exception:
            raise RuntimeError(SECRET)
        return Response()
    post = get = delete = request

async def run():
    async def token(*args):
        return "synthetic-token"
    async def node(*args):
        return 1
    with patch.object(mtg, "get_mtg_host", return_value={"host_url":"https://fixture.invalid", "username":"test", "password":"synthetic"}), patch.object(mtg.aiohttp, "ClientSession", Session):
        for exceptional in [False, True]:
            Session.exception = exceptional
            mtg._token_cache.clear()
            assert await mtg._get_token("test") is None
            with patch.object(mtg, "_get_token", token), patch.object(mtg, "_get_best_node_id", node):
                assert await mtg.create_proxy_for_user("test", "fixture", 30) is None
                assert await mtg.renew_proxy_for_user("test", "fixture", 1, 30) is None
                assert await mtg.delete_proxy_for_user("test", "fixture", 1) is False
                assert await mtg.enable_proxy_for_user("test", "fixture", 1) is False
                assert await mtg.get_proxy_details("test", "fixture") is None
            assert await mtg._get_best_node_id("https://fixture.invalid", "test") is None
        Session.exception = False
        Response.status = 200
        mtg._token_cache.clear()
        assert await mtg._get_token("test") is None  # valid JSON, missing token
        with patch.object(mtg, "_get_token", token), patch.object(mtg, "_get_best_node_id", node):
            assert await mtg.create_proxy_for_user("test", "fixture", 30) is None  # missing link

capture = Capture()
mtg.logger.addHandler(capture)
try:
    asyncio.run(run())
finally:
    mtg.logger.removeHandler(capture)
assert records
text = "\n".join(logging.Formatter().format(r) for r in records)
assert SECRET not in text
assert not any(r.exc_info for r in records)
assert "403" in text and "RuntimeError" in text and "missing token" in text
print("Stage 1 MTG log safety: OK")

"""Exercise health diagnostics with disposable data and synthetic tasks only."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from shop_bot.utils.runtime_health import RuntimeHealth


async def exercise_runtime():
    now = [0.0]
    monitor = RuntimeHealth(clock=lambda: now[0])
    heartbeat = monitor.create_task("heartbeat", monitor.heartbeat())
    blocked = monitor.create_task("payments", asyncio.Event().wait())
    await asyncio.sleep(0)
    monitor.cycle_started("payments")
    monitor.progress("payments", "external_panel")
    monitor.expect_bot("ShopBot", True)
    monitor.bot_start_failed("ShopBot")
    assert "ShopBot_start_failed" in monitor.snapshot()["warnings"]
    assert not monitor.snapshot()["failed"]
    monitor.expect_bot("ShopBot", True)
    monitor.track_bot("ShopBot", blocked)
    middleware = monitor.api_middleware("ShopBot")
    sentinel = RuntimeError("SYNTHETIC_SECRET")

    async def fail(bot, method):
        raise sentinel

    try:
        await middleware(fail, None, None)
    except RuntimeError as exc:
        assert exc is sentinel
    else:
        raise AssertionError("API exception swallowed")
    assert "ShopBot_api_error" in monitor.snapshot()["warnings"]
    assert not monitor.snapshot()["failed"], "External error must not fail liveness"
    now[0] = 1000
    # Model a live loop while the panel request makes no progress.
    monitor._heartbeat = now[0]
    assert "payments_progress_delayed" in monitor.snapshot()["warnings"]
    assert not monitor.snapshot()["failed"]

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(bot, method):
        entered.set()
        await release.wait()
        return "same result"

    request = asyncio.create_task(middleware(slow, None, None))
    await entered.wait()
    now[0] += 181
    monitor._heartbeat = now[0]
    assert "ShopBot_api_delayed" in monitor.snapshot()["warnings"]
    release.set()
    assert await request == "same result"
    assert "ShopBot_api_error" not in monitor.snapshot()["warnings"]
    # A late request of a stopped instance must not alter a replacement's state.
    entered.clear()
    release.clear()
    old_request = asyncio.create_task(middleware(slow, None, None))
    await entered.wait()
    monitor.expect_bot("ShopBot", False)
    monitor.expect_bot("ShopBot", True)
    monitor.track_bot("ShopBot", blocked)
    release.set()
    await old_request
    assert monitor.snapshot()["bots"]["ShopBot"]["last_api_success_age_seconds"] is None
    assert "SYNTHETIC_SECRET" not in str(monitor.snapshot())
    monitor.cycle_finished("payments", error=True)
    assert monitor.snapshot()["cycles"]["payments"]["last_cycle_error"]
    monitor.cycle_started("payments")
    monitor.cycle_finished("payments")
    assert monitor.snapshot()["cycles"]["payments"]["success_age_seconds"] == 0
    now[0] += 31
    assert "event_loop_stalled" in monitor.snapshot()["failed"]
    monitor._heartbeat = now[0]
    blocked.cancel()
    await asyncio.gather(blocked, return_exceptions=True)
    assert "payments_stopped" in monitor.snapshot()["failed"]
    assert "ShopBot_stopped" in monitor.snapshot()["failed"]
    monitor.expect_bot("ShopBot", False)
    assert "ShopBot_stopped" not in monitor.snapshot()["failed"]
    monitor.close()
    heartbeat.cancel()
    await asyncio.gather(heartbeat, return_exceptions=True)
    assert not monitor.snapshot()["failed"]


asyncio.run(exercise_runtime())
with tempfile.TemporaryDirectory(prefix="health-check-") as temporary:
    os.environ["DB_PATH"] = str(Path(temporary) / "check.db")
    from shop_bot.data_manager import database as db
    from shop_bot.webhook_server.app import create_webhook_app
    from shop_bot.bot_controller import BotController

    from unittest.mock import AsyncMock
    from aiogram import Bot
    from aiogram.methods import GetMe
    from shop_bot import bot_controller as lifecycle

    async def check_real_session_middleware():
        monitor = RuntimeHealth()
        monitor.expect_bot("ShopBot", True)
        bot = Bot(token="123456:" + "x" * 35)
        bot.session.middleware(monitor.api_middleware("ShopBot"))
        sentinel = object()
        try:
            with patch.object(bot.session, "make_request", new=AsyncMock(return_value=sentinel)):
                assert await bot.session(bot, GetMe()) is sentinel
            assert monitor.snapshot()["bots"]["ShopBot"]["last_api_success_age_seconds"] is not None
        finally:
            await bot.session.close()
    asyncio.run(check_real_session_middleware())

    failed_controller = BotController()
    failed_controller._loop = SimpleNamespace(is_running=lambda: True)
    settings = {"telegram_bot_token":"synthetic", "telegram_bot_username":"fixture", "admin_telegram_id":"101"}
    with patch.object(lifecycle.database, "get_setting", side_effect=settings.get), patch.object(lifecycle, "Bot", side_effect=RuntimeError("synthetic startup failure")):
        assert failed_controller.start_shop_bot()["status"] == "error"
    assert "ShopBot_start_failed" in failed_controller.runtime_health.snapshot()["warnings"]
    assert not failed_controller.runtime_health.snapshot()["failed"]

    db.initialize_db()
    controller = BotController()
    app = create_webhook_app(controller)
    app.config["EVENT_LOOP"] = SimpleNamespace(is_running=lambda: True)
    client = app.test_client()
    assert client.get("/healthz").status_code == 200
    controller.runtime_health.expect_bot("ShopBot", False)
    assert client.get("/healthz").status_code == 200
    with patch("shop_bot.webhook_server.app.DB_FILE", Path(temporary) / "absent.db"):
        assert client.get("/healthz").status_code == 503
        assert not (Path(temporary) / "absent.db").exists(), "Health must never create DB"
    empty = Path(temporary) / "empty.db"
    empty.touch()
    with patch("shop_bot.webhook_server.app.DB_FILE", empty):
        assert client.get("/healthz").status_code == 503
    controller.runtime_health._heartbeat = controller.runtime_health._clock() - 31
    assert client.get("/healthz").status_code == 503
    assert not any(key in client.get("/healthz").text for key in ["SYNTHETIC_SECRET", temporary])
print("Stage 1 runtime health: OK")

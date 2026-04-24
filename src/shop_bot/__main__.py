import logging
import threading
import asyncio
import signal
import time
from dotenv import load_dotenv

load_dotenv()

from shop_bot.webhook_server.app import create_webhook_app
from shop_bot.data_manager.scheduler import periodic_subscription_check
from shop_bot.data_manager import database
from shop_bot.modules import xui_api
from shop_bot.bot_controller import BotController

APP_HOST = "0.0.0.0"
APP_PORT = 1488
TRUTHY_VALUES = {"1", "true", "yes", "on"}


class _RateLimitedDispatcherNoiseFilter(logging.Filter):
    def __init__(self, interval_seconds: int = 60):
        super().__init__()
        self.interval_seconds = interval_seconds
        self._last_seen: dict[str, float] = {}
        self._patterns = (
            "Failed to fetch updates - TelegramNetworkError",
            "Failed to fetch updates - TelegramConflictError",
            "Sleep for ",
        )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        matched_pattern = next((p for p in self._patterns if p in message), None)
        if not matched_pattern:
            return True

        now = time.monotonic()
        last = self._last_seen.get(matched_pattern, 0.0)
        if now - last < self.interval_seconds:
            return False
        self._last_seen[matched_pattern] = now

        if record.levelno >= logging.ERROR:
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - [%(levelname)s] - %(name)s - (%(filename)s).%(funcName)s(%(lineno)d) - %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.dispatcher").addFilter(
        _RateLimitedDispatcherNoiseFilter(interval_seconds=60)
    )


def _is_truthy_setting(value) -> bool:
    return str(value).strip().lower() in TRUTHY_VALUES


async def _cancel_pending_tasks(loop: asyncio.AbstractEventLoop) -> None:
    tasks = [
        task
        for task in asyncio.all_tasks(loop)
        if task is not asyncio.current_task(loop)
    ]
    if not tasks:
        return

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _register_signal_handlers(
    loop: asyncio.AbstractEventLoop, shutdown_callback, logger: logging.Logger
) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda sig=sig: asyncio.create_task(shutdown_callback(sig, loop))
            )
        except NotImplementedError:
            logger.warning(
                "Signal handlers are not supported on this platform/event loop."
            )
            break


def _run_flask_server(flask_app, logger: logging.Logger) -> None:
    try:
        from waitress import serve

        logger.info(
            "Starting production server (waitress) on http://%s:%s", APP_HOST, APP_PORT
        )
        serve(flask_app, host=APP_HOST, port=APP_PORT, _quiet=True)
    except Exception as e:
        logger.warning(
            "Waitress failed to start (%s). Falling back to Flask built-in server.", e
        )
        flask_app.run(host=APP_HOST, port=APP_PORT, threaded=True, use_reloader=False)


def _log_bot_autostart_result(
    logger: logging.Logger, bot_name: str, result: dict[str, str]
) -> None:
    if result["status"] == "success":
        logger.info("%s auto-started: %s", bot_name, result["message"])
        return
    logger.warning("%s auto-start skipped: %s", bot_name, result["message"])


def _summarize_xtls_result(
    logger: logging.Logger, host_name: str, result: dict, total_fixed: int
) -> int:
    fixed = result.get("fixed", 0)
    status = result.get("status", "unknown")
    total_fixed += fixed
    if fixed > 0:
        logger.info(
            "Startup XTLS sync for '%s': %s clients fixed. Status: %s",
            host_name,
            fixed,
            status,
        )
    elif status == "success":
        logger.debug("Startup XTLS sync for '%s': no fixes needed.", host_name)
    else:
        logger.warning("Startup XTLS sync for '%s': status=%s", host_name, status)
    return total_fixed


def main():
    _configure_logging()
    logger = logging.getLogger(__name__)

    database.initialize_db()
    logger.info("Database initialization check complete.")

    bot_controller = BotController()
    flask_app = create_webhook_app(bot_controller)

    async def shutdown(sig: signal.Signals, loop: asyncio.AbstractEventLoop):
        logger.info("Received signal: %s. Shutting down...", sig.name)
        if bot_controller.get_status()["is_running"]:
            bot_controller.stop()
            await asyncio.sleep(2)
        await _cancel_pending_tasks(loop)
        loop.stop()

    async def start_services():
        loop = asyncio.get_running_loop()
        bot_controller.set_loop(loop)
        flask_app.config["EVENT_LOOP"] = loop

        _register_signal_handlers(loop, shutdown, logger)

        flask_thread = threading.Thread(
            target=_run_flask_server, args=(flask_app, logger), daemon=True
        )
        flask_thread.start()

        logger.info(
            "Flask server started in a background thread on http://%s:%s",
            APP_HOST,
            APP_PORT,
        )

        # Auto-start Telegram Bot and Support Bot on application startup
        try:
            logger.info("Attempting to auto-start Telegram Bot and Support Bot...")
            _log_bot_autostart_result(
                logger, "Telegram Bot", bot_controller.start_shop_bot()
            )
            _log_bot_autostart_result(
                logger, "Support Bot", bot_controller.start_support_bot()
            )
        except Exception as e:
            logger.error("Error during bot auto-start: %s", e, exc_info=True)

        # Perform initial XTLS sync at startup only if enabled
        try:
            xtls_enabled = database.get_setting("xtls_sync_enabled")
            if _is_truthy_setting(xtls_enabled):
                logger.info("Performing initial XTLS synchronization at startup...")
                try:
                    sync_results = await xui_api.sync_inbounds_xtls_from_all_hosts()
                    total_fixed = 0
                    if sync_results and isinstance(sync_results, dict):
                        for host_name, result in sync_results.items():
                            if isinstance(result, dict):
                                total_fixed = _summarize_xtls_result(
                                    logger, host_name, result, total_fixed
                                )
                    if total_fixed > 0:
                        logger.info(
                            "Startup XTLS synchronization completed: %s total clients fixed",
                            total_fixed,
                        )
                    else:
                        logger.info(
                            "Startup XTLS synchronization completed: all clients have correct settings"
                        )
                except Exception as e:
                    logger.error(
                        "Error during startup XTLS synchronization: %s",
                        e,
                        exc_info=True,
                    )
            else:
                logger.debug("Startup XTLS sync disabled (xtls_sync_enabled=false).")
        except Exception as e:
            logger.error(
                "Failed to read xtls_sync_enabled setting: %s", e, exc_info=True
            )

        logger.info("Application is running. Bots are auto-started if configured.")

        asyncio.create_task(periodic_subscription_check(bot_controller))

        await asyncio.Future()

    try:
        asyncio.run(start_services())
    finally:
        logger.info("Application is shutting down.")


if __name__ == "__main__":
    main()

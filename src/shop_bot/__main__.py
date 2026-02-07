import logging
import threading
import asyncio
import signal
import os
from dotenv import load_dotenv

load_dotenv()

from shop_bot.webhook_server.app import create_webhook_app
from shop_bot.data_manager.scheduler import periodic_subscription_check
from shop_bot.data_manager import database
from shop_bot.modules import xui_api
from shop_bot.bot_controller import BotController

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - [%(levelname)s] - %(name)s - (%(filename)s).%(funcName)s(%(lineno)d) - %(message)s"
    )
    # Reduce noise from HTTP libraries (SSL errors, connection traces)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    logger = logging.getLogger(__name__)

    database.initialize_db()
    logger.info("Database initialization check complete.")

    bot_controller = BotController()
    flask_app = create_webhook_app(bot_controller)
    
    async def shutdown(sig: signal.Signals, loop: asyncio.AbstractEventLoop):
        logger.info(f"Received signal: {sig.name}. Shutting down...")
        if bot_controller.get_status()["is_running"]:
            bot_controller.stop()
            await asyncio.sleep(2)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            [task.cancel() for task in tasks]
            await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()

    async def start_services():
        loop = asyncio.get_running_loop()
        bot_controller.set_loop(loop)
        flask_app.config['EVENT_LOOP'] = loop
        
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda sig=sig: asyncio.create_task(shutdown(sig, loop)))
            except NotImplementedError:
                # Windows ProactorEventLoop does not support add_signal_handler
                logger.warning("Signal handlers are not supported on this platform/event loop.")
                break
        
        def run_flask():
            try:
                from waitress import serve
                logger.info("Starting production server (waitress) on http://0.0.0.0:1488")
                serve(flask_app, host='0.0.0.0', port=1488, _quiet=True)
            except Exception as e:
                logger.warning(
                    f"Waitress failed to стартовать ({e}). "
                    "Falling back to Flask встроенный сервер.",
                )
                flask_app.run(host='0.0.0.0', port=1488, threaded=True, use_reloader=False)

        flask_thread = threading.Thread(
            target=run_flask,
            daemon=True
        )
        flask_thread.start()
        
        logger.info("Flask server started in a background thread on http://0.0.0.0:1488")
        
        # Perform initial XTLS sync at startup only if enabled
        try:
            xtls_enabled = database.get_setting("xtls_sync_enabled")
            if str(xtls_enabled).strip().lower() in {"1", "true", "yes", "on"}:
                logger.info("Performing initial XTLS synchronization at startup...")
                try:
                    sync_results = await xui_api.sync_inbounds_xtls_from_all_hosts()
                    total_fixed = 0
                    if sync_results and isinstance(sync_results, dict):
                        for host_name, result in sync_results.items():
                            if isinstance(result, dict):
                                fixed = result.get('fixed', 0)
                                total_fixed += fixed
                                status = result.get('status', 'unknown')
                                if fixed > 0:
                                    logger.info(f"Startup XTLS sync for '{host_name}': {fixed} clients fixed. Status: {status}")
                                elif status == 'success':
                                    logger.debug(f"Startup XTLS sync for '{host_name}': no fixes needed.")
                                else:
                                    logger.warning(f"Startup XTLS sync for '{host_name}': status={status}")
                    if total_fixed > 0:
                        logger.info(f"Startup XTLS synchronization completed: {total_fixed} total clients fixed")
                    else:
                        logger.info("Startup XTLS synchronization completed: all clients have correct settings")
                except Exception as e:
                    logger.error(f"Error during startup XTLS synchronization: {e}", exc_info=True)
            else:
                logger.debug("Startup XTLS sync disabled (xtls_sync_enabled=false).")
        except Exception as e:
            logger.error(f"Failed to read xtls_sync_enabled setting: {e}", exc_info=True)
            
        logger.info("Application is running. Bot can be started from the web panel.")
        
        asyncio.create_task(periodic_subscription_check(bot_controller))

        await asyncio.Future()

    try:
        asyncio.run(start_services())
    finally:
        logger.info("Application is shutting down.")

if __name__ == "__main__":
    main()

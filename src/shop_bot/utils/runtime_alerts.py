"""Rate-limited operational alerts; no financial or panel operations."""
import asyncio
import logging
import time

from shop_bot.utils.admin_ids import parse_admin_telegram_ids

logger = logging.getLogger(__name__)


async def runtime_watchdog(controller):
    from shop_bot.data_manager.database import get_setting

    monitor = controller.runtime_health
    first_seen = {}
    last_sent = {}
    while True:
        try:
            snapshot = monitor.snapshot()
            if snapshot['closing']:
                return
            now = time.monotonic()
            failures = set(snapshot['failed'])
            # Short external outages and the normal startup window are advisory.
            candidates = failures | {
                code for code in snapshot['warnings']
                if code.endswith(('_cycle_error', '_progress_delayed', '_start_failed'))
            }
            first_seen = {code: stamp for code, stamp in first_seen.items() if code in candidates}
            for code in candidates:
                first_seen.setdefault(code, now)
            serious = sorted(code for code in candidates
                             if (code in failures or now - first_seen[code] >= 900)
                             and now - last_sent.get(code, -1800) >= 1800)
            if serious:
                for code in serious:
                    last_sent[code] = now
                logger.critical('Runtime fault detected: %s', ', '.join(serious))
                bot = controller.get_bot_instance() or getattr(controller, 'support_bot', None)
                if bot:
                    admins = parse_admin_telegram_ids(await asyncio.to_thread(get_setting, 'admin_telegram_id'))
                    text = ('⚠️ Требуется проверка VPN-бота: ' + ', '.join(serious)
                            + '. Автоматический перезапуск и повторная выдача не выполнялись.')
                    for admin_id in admins:
                        try:
                            await asyncio.wait_for(bot.send_message(admin_id, text), timeout=10)
                        except Exception as error:
                            logger.warning('Runtime alert delivery failed: %s', type(error).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error('Runtime watchdog check failed: %s', type(error).__name__)
        await asyncio.sleep(15)

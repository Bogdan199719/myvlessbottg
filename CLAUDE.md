# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MyVlessBot is a Telegram bot for automated VLESS VPN key sales, integrating with **3x-ui** panels. It runs as a single Docker container exposing a Flask web admin panel on port 1488.

## Commands

### Run (Docker - primary method)
```bash
docker-compose up -d --build   # Build and start
docker-compose logs -f         # Follow logs
docker-compose down            # Stop
```

### Run locally (development)
```bash
pip install -e ".[dev]"
python -m shop_bot
```

### Lint / Format
```bash
pylint src/
black src/
```

### Utilities
```bash
python scripts/check_callbacks.py         # Check callback handler coverage
python scripts/check_fsm_transitions.py  # Check FSM state transitions
```

## Architecture

The application starts a single asyncio event loop (`__main__.py`) that coordinates:

1. **Flask web server** (waitress in production) — runs in a background daemon thread on port 1488. Hosts the admin panel and payment webhooks.
2. **Telegram Shop Bot** — aiogram 3.x polling, managed by `BotController`.
3. **Telegram Support Bot** — separate aiogram bot for customer support, also managed by `BotController`.
4. **Subscription scheduler** — `asyncio.Task` running `periodic_subscription_check()` every 60 seconds, handles expiry notifications and key cleanup.

### Key Modules

| Path | Responsibility |
|---|---|
| `src/shop_bot/__main__.py` | Entry point; wires all services together |
| `src/shop_bot/bot_controller.py` | Manages start/stop of Shop Bot and Support Bot; bridges the asyncio loop with Flask's sync context via `run_coroutine_threadsafe` |
| `src/shop_bot/bot/handlers.py` | All user-facing Telegram command and callback handlers |
| `src/shop_bot/bot/support_handlers.py` | Support bot handlers (relay messages between users and support group) |
| `src/shop_bot/bot/keyboards.py` | Inline keyboard builders |
| `src/shop_bot/bot/middlewares.py` | `BanMiddleware` and `SafeCallbackMiddleware` |
| `src/shop_bot/webhook_server/app.py` | Flask app: admin panel routes, payment webhooks (YooKassa, CryptoBot, Heleket, TON) |
| `src/shop_bot/webhook_server/subscription_api.py` | Public API Blueprint for subscription status checks |
| `src/shop_bot/modules/xui_api.py` | 3x-ui panel integration via `py3xui`: create/update/delete VLESS clients across multiple hosts |
| `src/shop_bot/data_manager/database.py` | All SQLite operations; `DB_FILE` defaults to `./users.db` or `$DB_PATH` env var |
| `src/shop_bot/data_manager/database_helpers.py` | Additional query helpers (paid vs. trial key separation) |
| `src/shop_bot/data_manager/scheduler.py` | Async subscription expiry checker and auto-renewal logic |
| `src/shop_bot/config.py` | User-facing message templates (Russian locale) |

### Database (SQLite, WAL mode)

Tables: `users`, `vpn_keys`, `vpn_keys_missing`, `transactions`, `bot_settings`, `support_threads`.

All runtime configuration (bot tokens, payment credentials, plans, hosts) is stored in `bot_settings` and read via `database.get_setting(key)`. There is no static config file — settings are managed through the admin panel UI and persisted to the DB.

### Payment Methods

Supported: YooKassa, CryptoBot, Heleket, TonConnect, Telegram Stars. Each is enabled/disabled via `bot_settings`. Payment webhooks hit Flask routes; on confirmation the bot issues/extends VLESS keys via `xui_api`.

### Multi-host VPN

Multiple 3x-ui panel hosts can be configured. Keys are provisioned per-host. Global subscription tokens allow a single link to work across hosts. XTLS sync runs at startup if `xtls_sync_enabled=true`.

## Async in Flask routes

Flask runs in a Waitress daemon thread that has **no event loop**. Never call `asyncio.run()` inside route handlers — it creates a new loop per request and hangs indefinitely if the 3x-ui panel is unreachable.

Use the `_run_async(coro, timeout=45)` helper defined inside `create_webhook_app` (`app.py`), which dispatches to the main asyncio loop via `run_coroutine_threadsafe` and enforces a timeout:

```python
result = _run_async(xui_api.create_or_update_key_on_host(...))
```

For fire-and-forget background tasks (no result needed), use `asyncio.run_coroutine_threadsafe(coro, loop)` without `.result()`.

## Environment

Configuration is via `.env` file (loaded by `python-dotenv`). Minimum required at startup for bots to auto-start: `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `DOMAIN`. All other settings (payment keys, host credentials) are configured through the admin panel at `http://<domain>/admin`.

The Docker volume mounts the project root to `/app/project`, so `users.db` and `.env` persist across container rebuilds.

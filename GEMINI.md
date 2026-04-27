# GEMINI.md

## Project Overview

**MyVlessBot** is a comprehensive Telegram bot ecosystem for automated VPN (VLESS) and Telegram Proxy (MTG) sales. It features a user-facing bot, a support bot, and a Flask-based web administration panel.

- **Primary Technologies:** Python 3.10+, aiogram 3.26+, Flask, Waitress, SQLite (WAL mode).
- **Core Integrations:** 
  - **3x-ui:** Multi-host VLESS VPN management via `py3xui`.
  - **MTG Proxy:** MTProto Proxy management via REST API.
  - **Payments:** YooKassa, Telegram Stars, CryptoBot, and manual P2P.
- **Architecture:** 
  - The entry point (`src/shop_bot/__main__.py`) initializes the SQLite database and runs both the Flask app and the aiogram bot(s) concurrently within an `asyncio` event loop.
  - Background tasks (expiry notifications, service synchronization) are handled by an internal async scheduler.

## Building and Running

### Environment Setup
1.  Copy `.env.example` to `.env`.
2.  Configure required variables: `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `DOMAIN`, `PANEL_LOGIN`, `PANEL_PASSWORD`.

### Deployment via Docker (Recommended)
```bash
docker compose up -d --build
docker compose logs -f
```

### Local Development
1.  Create and activate a virtual environment:
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```
2.  Install the project in editable mode with development dependencies:
    ```bash
    pip install -e .[dev]
    ```
3.  Run the application:
    ```bash
    python3 -m shop_bot
    ```
    *The admin panel will be available at `http://localhost:1488`.*

### Validation & Quality Tools
Run these scripts before committing changes:
- **Syntax Check:** `python3 -m compileall -q src scripts`
- **FSM & Callback Validation:** 
  - `python3 scripts/check_callbacks.py`
  - `python3 scripts/check_fsm_transitions.py`
- **Logic Validation:** `python3 scripts/check_host_cleanup.py`
- **Formatting:** `black src scripts`

## Development Conventions

### Code Style
- **Indentation:** 4 spaces for Python; 2 spaces for YAML, TOML, HTML, CSS, and JS.
- **Naming:** `snake_case` for modules/functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- **Timezone:** All timestamps MUST use the Moscow timezone (`Europe/Moscow`). Use `shop_bot.utils.time_utils` for consistency.

### aiogram (Bot) Patterns
- **Bot API 9.4+ Styles:** Use the `style` parameter in `InlineKeyboardButton`:
  - `primary` (blue): Actions like "Buy", "Select", "Copy".
  - `success` (green): "Extend", "Confirm", "Connect".
  - `danger` (red): "Cancel", "Delete".
  - Default (gray): Navigation (Back, Menu).
- **Thin Handlers:** Keep logic in `handlers.py` minimal; move complex operations to `data_manager/` or `modules/`.

### Database (SQLite)
- **No ORM:** Use raw SQL queries within `src/shop_bot/data_manager/database.py`.
- **WAL Mode:** Enabled by default for better concurrency.
- **Naming Consistency:** Use the `host_slug()` helper for generating consistent host identifiers.

### Module Organization
- `src/shop_bot/bot/`: aiogram handlers, keyboards, and middleware.
- `src/shop_bot/webhook_server/`: Flask routes, subscription API, and Jinja2 templates.
- `src/shop_bot/data_manager/`: Database schema, CRUD, and scheduler.
- `src/shop_bot/modules/`: External API clients (3x-ui, MTG).
- `scripts/`: Maintenance and validation utilities.

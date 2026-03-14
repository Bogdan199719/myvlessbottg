# VLESS Shop Bot - Project Context

## Project Overview

**VLESS Shop Bot** (also known as MyVlessBot) is a Telegram bot for automated VLESS VPN key sales. It integrates with **3x-ui** panel and provides a web admin panel for managing hosts, plans, users, and payments.

### Key Features
- Automatic VLESS key issuance after payment
- Web admin panel for management
- Multi-host support with global subscriptions
- Flexible subscription plans (monthly, yearly, trial, referral-based)
- Multiple payment gateways: YooKassa, CryptoBot, Heleket, TonConnect, Telegram Stars
- Built-in support bot workflow
- XTLS synchronization with 3x-ui hosts

### Architecture

```
src/shop_bot/
├── __main__.py           # Application entry point
├── bot_controller.py     # Main bot orchestration
├── config.py             # Configuration management
├── version.py            # Version info (v2.4.19)
├── bot/                  # Telegram bot handlers & keyboards
│   ├── handlers.py       # Main bot message handlers
│   ├── keyboards.py      # Inline/reply keyboards
│   ├── middlewares.py    # Aiogram middlewares
│   └── support_handlers.py # Support bot handlers
├── data_manager/         # Database & scheduling
├── modules/              # Core modules
│   └── xui_api.py        # 3x-ui panel API integration
├── utils/                # Utility functions
└── webhook_server/       # Flask web admin panel
```

## Technology Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.10 |
| Bot Framework | aiogram 3.x (async) |
| Web Panel | Flask + Waitress |
| Database | SQLite (`users.db`) |
| VPN Integration | py3xui (3x-ui API) |
| Payments | YooKassa, CryptoBot, TonConnect |
| Containerization | Docker + Docker Compose |

## Building and Running

### Prerequisites
- Docker & Docker Compose
- Python 3.10+ (for local development)
- Telegram Bot Token
- 3x-ui panel credentials
- Domain for web panel (optional, for SSL)

### Quick Start (Docker)

```bash
# 1. Clone and setup
git clone https://github.com/Bogdan199719/myvlessbottg.git
cd vless-shopbot

# 2. Create .env file (required variables)
cp .env.example .env  # or create manually
# Set: TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_ID, DOMAIN

# 3. Run with Docker Compose
docker-compose up -d --build

# 4. View logs
docker-compose logs -f
```

### One-Command Install (Production)

```bash
curl -sSL https://raw.githubusercontent.com/Bogdan199719/myvlessbottg/main/install.sh | sudo bash
```

### Local Development

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e .

# Run the bot
python -m shop_bot
```

### Environment Variables (.env)

| Variable | Description | Required |
|----------|-------------|:--------:|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather | Yes |
| `ADMIN_TELEGRAM_ID` | Admin's Telegram user ID | Yes |
| `DOMAIN` | Domain for web admin panel | Yes |
| `PANEL_LOGIN` | 3x-ui panel login | No |
| `PANEL_PASSWORD` | 3x-ui panel password | No |
| `YOOKASSA_ENABLED` | Enable YooKassa payments | No |
| `CRYPTOBOT_ENABLED` | Enable CryptoBot payments | No |
| `TONCONNECT_ENABLED` | Enable TonConnect payments | No |

## Development Conventions

### Code Style
- **Formatter**: Black (configured in dev dependencies)
- **Linter**: Pylint (configured in dev dependencies)
- **Package Management**: pip-tools for dependency locking

### Project Structure Conventions
- All source code under `src/shop_bot/`
- Bot logic separated into `bot/` module
- Database operations in `data_manager/`
- External API integrations in `modules/`
- Web assets (HTML/CSS/JS) included in package data

### Testing Practices
- Scripts available in `scripts/` for diagnostics:
  - `check_callbacks.py` - Callback query validation
  - `check_fsm_transitions.py` - State machine validation
- Manual testing via Telegram bot interaction

### Git & Deployment
- `.env` and `*.db` files are gitignored
- Updates use `git fetch/reset` preserving `.env` and `users.db`
- Docker volumes mount the project directory for hot-reload

## Key Commands

```bash
# Docker operations
docker-compose up -d           # Start services
docker-compose down            # Stop services
docker-compose logs -f         # Follow logs
docker-compose restart         # Restart services

# Bot operations (via Telegram)
# - Admin panel: /admin
# - Support: /support
# - User commands handled via inline keyboards

# Development
pip install -e .               # Install in editable mode
python -m shop_bot             # Run locally
```

## Important Files

| File | Purpose |
|------|---------|
| `pyproject.toml` | Project metadata & dependencies |
| `docker-compose.yml` | Docker service configuration |
| `install.sh` | Automated production installer |
| `src/shop_bot/__main__.py` | Application entry point |
| `src/shop_bot/bot_controller.py` | Bot lifecycle management |
| `src/shop_bot/modules/xui_api.py` | 3x-ui integration logic |

## Security Notes

- Never commit `.env` or `users.db` to version control
- Store all tokens and API keys in `.env` or admin panel settings
- SSL certificates managed via Certbot (auto-configured in installer)
- Database backups created automatically (`users.db.bak_*`)

## Support & Maintenance

- Logs: `docker-compose logs -f` or `logs.txt`
- Database backups: `users.db.bak_YYYYMMDD_HHMM`
- Admin panel: `https://<DOMAIN>` (default: admin/admin)

# MyVlessBot - VPN Sales Bot

MyVlessBot is a ready-to-run solution for automated VLESS key sales via Telegram. It integrates with **3x-ui** and provides a web admin panel to manage hosts, plans, users, and payments.

## Features

- Automatic key issuance after payment.
- Web admin panel.
- Multi-host support.
- Flexible plans (month, year, trial, referrals).
- Payments: YooKassa, CryptoBot, Heleket, TonConnect, Telegram Stars.
- Built-in support workflow.

## One-command install

```bash
curl -sSL https://raw.githubusercontent.com/Bogdan199719/myvlessbottg/main/install.sh | sudo bash
```

The script installs dependencies (Docker, Nginx, Certbot), asks for settings, and creates `.env`.

## Environment

Project uses `.env` for configuration. Key variables:

| Variable | Description | Required |
| --- | --- | :---: |
| `TELEGRAM_BOT_TOKEN` | Bot token | yes |
| `ADMIN_TELEGRAM_ID` | Admin Telegram ID | yes |
| `DOMAIN` | Domain for web panel | yes |
| `PANEL_LOGIN` | Panel login | no |
| `PANEL_PASSWORD` | Panel password | no |

Full list: see `install.sh` and the admin panel settings.

## Manual install (Docker)

```bash
# 1. Clone
git clone https://github.com/Bogdan199719/myvlessbottg.git
cd myvlessbottg

# 2. Create .env
nano .env

# 3. Run
docker-compose up -d --build
```

## Updates

- Admin panel update uses `git fetch/reset` and `pip install`.
- `.env` and `users.db` are not touched.

## Documentation

- [DOCUMENTATION_INDEX.md](DOCUMENTATION_INDEX.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)

## License

MyVlessBot ? 2024 Bogdan199719. All rights reserved.

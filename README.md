# MyVlessBot - VPN Sales Bot

MyVlessBot is a ready-to-run solution for automated VLESS key sales via Telegram. It integrates with **3x-ui** and provides a web admin panel to manage hosts, plans, users, and payments.

## Features

- Automatic key issuance after payment.
- Web admin panel.
- Multi-host support with global subscription support.
- Flexible plans (month, year, trial, referrals).
- Payments: YooKassa, CryptoBot, Heleket, TonConnect, Telegram Stars.
- Built-in support workflow.

## One-command install (recommended)

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

- Admin panel update uses `git fetch/reset` and dependency install.
- `.env` and `users.db` are not touched.

## Security & privacy

- Do NOT commit `.env` or `users.db` to Git.
- Keep all tokens and API keys only in `.env` or in the admin panel settings.
- This repository does not include any personal data or secrets.

## Support

If you have questions or issues, open an issue or contact the support bot configured in your panel.

## License

MyVlessBot © 2024 Bogdan199719. All rights reserved.

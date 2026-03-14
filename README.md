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

## Environment & security

Project uses `.env` for configuration. Key variables:

| Variable | Description | Required |
| --- | --- | :---: |
| `TELEGRAM_BOT_TOKEN` | Bot token | yes |
| `ADMIN_TELEGRAM_ID` | Admin Telegram ID | yes |
| `DOMAIN` | Domain for web panel | yes |
| `PANEL_LOGIN` | Panel login | no |
| `PANEL_PASSWORD` | Panel password | no |

Full list: see `install.sh` and the admin panel settings.
Do not commit `.env`, `users.db`, backups, or logs. They are local runtime files and are ignored by Git.

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

After first start, verify panel credentials and change any temporary/default values in the admin panel.

## Development checks

- `python3 -m compileall -q src scripts`
- `python3 scripts/check_callbacks.py`
- `python3 scripts/check_fsm_transitions.py`

## Updates

- Application updates should not overwrite `.env` or `users.db`.
- Review installer and admin settings when adding new payment providers or tokens.

## Recent Stability Updates (No Version Bump)

- Safer subscription handling for invalid/legacy `plan_id` values.
- Fallback recreate flow for `record not found` errors in 3x-ui client updates.
- Reduced noisy Telegram callback errors (`message is not modified`) in runtime logs.
- Refactoring of host auto-provision trigger paths in web admin routes.
- Improved global subscription key handling and safer token logging (prefix only).

## Security & privacy

- Do NOT commit `.env` or `users.db` to Git.
- Keep all tokens and API keys only in `.env` or in the admin panel settings.
- No live API keys, database files, or personal data should be stored in tracked files.

## Support

If you have questions or issues, open an issue or contact the support bot configured in your panel.

## License

MyVlessBot © 2024 Bogdan199719. All rights reserved.

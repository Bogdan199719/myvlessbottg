# Deployment

## Требования

- Docker Engine с Docker Compose;
- домен с A/AAAA-записью на сервер;
- открытые снаружи порты `80` и `443`;
- токен Telegram-бота, Telegram ID администратора и учётные данные 3x-ui.

Контейнер намеренно слушает только `127.0.0.1:1488`. Не открывайте этот порт во внешний интернет: используйте HTTPS reverse proxy.

## Запуск

```bash
git clone https://github.com/Bogdan199719/myvlessbottg.git
cd myvlessbottg
cp .env.example .env
chmod 600 .env
docker compose up -d --build
docker compose logs -f bot
```

До запуска заполните в `.env` минимум `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `DOMAIN`, `FLASK_SECRET_KEY`, `PANEL_LOGIN` и `PANEL_PASSWORD`. Для `FLASK_SECRET_KEY` используйте случайное значение длиной не менее 32 символов, например `openssl rand -hex 32`.

Проверка локального сервиса:

```bash
curl -fsS http://127.0.0.1:1488/healthz
```

На первом старте приложение создаёт или мигрирует `users.db` и переносит стартовые настройки из `.env` в SQLite. После этого рабочие параметры хранятся в `bot_settings` и меняются через админку.

## Reverse proxy и HTTPS

Установите Nginx и Certbot, создайте `/etc/nginx/sites-available/myvlessbot`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name vpn.example.com;

    location / {
        proxy_pass http://127.0.0.1:1488;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Замените `vpn.example.com` на значение `DOMAIN`, включите конфигурацию и получите сертификат:

```bash
sudo ln -s /etc/nginx/sites-available/myvlessbot /etc/nginx/sites-enabled/myvlessbot
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d vpn.example.com --redirect
```

Проверьте `https://vpn.example.com/healthz`, затем войдите в админку по `https://vpn.example.com/login` с `PANEL_LOGIN` и `PANEL_PASSWORD`. Если перед Nginx есть CDN или другой proxy, добавьте его непосредственный CIDR в `TRUSTED_PROXY_CIDRS`; не доверяйте произвольному `X-Forwarded-For`.

Альтернатива: `bash install.sh` автоматизирует установку Docker, Nginx и Let's Encrypt на Ubuntu/Debian. Запускайте его только на сервере, где домен уже резолвится на этот хост.

## Состояние, backup и restore

- `.env`, `users.db` и backup-файлы — runtime-данные, а не исходный код;
- храните `.env` с правами `600` и делайте резервные копии вне сервера;
- backup/import из админки включает `.env` только при явном выборе в форме;
- restore сначала сохраняется как pending, а перед стартом Flask, ботов и scheduler применяется с rollback-копиями БД и `.env`.

## Обновление

Перед обновлением сделайте backup и убедитесь, что дерево Git чистое:

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
```

Встроенный update-manager по умолчанию выключен (`ENABLE_WEB_UPDATES=false`). При включении он использует `git reset --hard origin/main`; для production предпочтительно ручное обновление выше.

## Проверки перед деплоем

Если зависимости установлены только в контейнере, запускайте проверки так:

```bash
docker compose exec -T bot python3 -m compileall -q src scripts
docker compose exec -T bot python3 scripts/check_callbacks.py
docker compose exec -T bot python3 scripts/check_fsm_transitions.py
docker compose exec -T bot python3 scripts/check_host_cleanup.py
docker compose exec -T bot python3 scripts/check_payment_safety.py
docker compose exec -T bot python3 scripts/check_profit_accounting.py
docker compose exec -T bot python3 scripts/check_auto_selector.py
docker compose exec -T bot python3 scripts/check_happ_subscription_metadata.py
docker compose exec -T bot python3 scripts/check_ip_limit_rules.py
docker compose exec -T bot python3 scripts/check_proxy_keyboard.py
docker compose exec -T bot python3 scripts/check_scheduler_integrations.py
docker compose exec -T bot python3 scripts/check_subscription_business_rules.py
docker compose exec -T bot python3 scripts/check_subscription_consistency.py
docker compose exec -T bot python3 scripts/check_xui_connection_equivalence.py
docker compose exec -T bot python3 scripts/check_settings_defaults.py
bash -n install.sh
docker compose config --quiet
git diff --check
```

`check_subscription_consistency.py` читает live-БД и проверяет, что каждый active trial и active paid global пользователь имеет ключ на каждом включённом XUI-хосте. По умолчанию скрипт маскирует идентификаторы; `--show-identities` предназначен только для доверенной локальной диагностики.

`check_subscription_business_rules.py` и `check_profit_accounting.py` используют временную SQLite и не изменяют production-данные.

# Deployment

## Локальный запуск через Docker

```bash
cp .env.example .env
# обязательно задайте TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_ID, DOMAIN
# и сильный FLASK_SECRET_KEY, если .env создаётся вручную
docker compose up -d --build
docker compose logs -f
```

Сервис:

- собирает образ из [Dockerfile](/root/vless-shopbot/Dockerfile);
- монтирует проект в контейнер как `/app/project`;
- запускает `python3 -m shop_bot`;
- слушает `1488`.

## Что происходит на старте

- загружается `.env`;
- инициализируется и мигрируется SQLite;
- создаётся Flask app;
- поднимается Waitress;
- запускается основной бот, если в `bot_settings` есть `telegram_bot_token` и `admin_telegram_id`; username бота может быть получен автоматически через Bot API;
- запускается support-бот, если в `bot_settings` есть `support_bot_token` и `support_group_id`;
- стартует `periodic_subscription_check`.

## Переменные и состояние

- `.env` используется как первичная конфигурация;
- фактические рабочие настройки дальше живут в таблице `bot_settings`;
- БД по умолчанию: `users.db` в корне проекта;
- backup-файлы и `.env` считаются runtime-артефактами, не исходниками.
- `FLASK_SECRET_KEY` должен быть случайным секретом длиной не меньше 32 символов. `install.sh` генерирует его автоматически; placeholder-значения из шаблона приложение не использует.

## Backup и restore

В админке есть:

- создание zip-бэкапа с `users.db`, `metadata.json` и, при выборе в форме, `.env`;
- импорт такого архива с проверкой checksum;
- опциональное применение `.env` при restore через отдельный checkbox;
- перед заменой БД бот пытается остановиться, после чего выполняется `run_migration()`.

## Обновление

Есть два варианта:

### Ручное

```bash
git pull
docker compose up -d --build
```

Если образ уже собран отдельно через `docker compose build`, изменения всё равно не попадут в работающий контейнер до `docker compose up -d`.

### Из админки

Встроенный update-manager:

- проверяет версию по GitHub Raw;
- по умолчанию отключён в production и требует явного `ENABLE_WEB_UPDATES=true`;
- делает `git fetch origin main`, если web-update включён;
- проверяет, что рабочее дерево чистое;
- только после этого делает `git reset --hard origin/main`;
- выполняет `pip install -e .`;
- завершает процесс, чтобы Docker его перезапустил.

Это всё ещё агрессивная схема обновления, но теперь она не запускается при локальных незакоммиченных изменениях.

Для Docker production предпочтителен ручной rebuild/redeploy через `docker compose up -d --build`, потому что зависимости устанавливаются при сборке образа.

## Healthcheck

`/healthz` используется Docker healthcheck-ом. Endpoint публично не требует авторизации, поэтому он должен возвращать только минимальный статус `ok/degraded`, без деталей о БД, event loop и ботах.

После изменения кода `/healthz` старый подробный ответ будет сохраняться до пересоздания контейнера.

## Проверки перед деплоем

```bash
python3 -m compileall -q src scripts
python3 scripts/check_callbacks.py
python3 scripts/check_fsm_transitions.py
python3 scripts/check_host_cleanup.py
python3 scripts/check_settings_defaults.py
bash -n install.sh
docker compose config --quiet
git diff --check
docker compose build
```

Для очистки локальных cache-артефактов есть:

```bash
./scripts/cleanup.sh
```

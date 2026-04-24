# Deployment

## Локальный запуск через Docker

```bash
cp .env.example .env
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

### Из админки

Встроенный update-manager:

- проверяет версию по GitHub Raw;
- делает `git fetch origin main`;
- проверяет, что рабочее дерево чистое;
- только после этого делает `git reset --hard origin/main`;
- выполняет `pip install -e .`;
- завершает процесс, чтобы Docker его перезапустил.

Это всё ещё агрессивная схема обновления, но теперь она не запускается при локальных незакоммиченных изменениях.

## Проверки перед деплоем

```bash
python3 -m compileall -q src scripts
python3 scripts/check_callbacks.py
python3 scripts/check_fsm_transitions.py
python3 scripts/check_host_cleanup.py
python3 scripts/check_settings_defaults.py
```

Для очистки локальных cache-артефактов есть:

```bash
./scripts/cleanup.sh
```

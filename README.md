# MyVlessBot

Telegram-бот для продажи VPN и Telegram Proxy с веб-админкой, SQLite и автоматической выдачей доступов через 3x-ui и MTG AdminPanel.

## Что есть в проекте

- основной Telegram-бот на `aiogram 3`;
- отдельный support-бот с forum topics в группе поддержки;
- Flask-админка с авторизацией, статистикой, управлением пользователями, хостами, тарифами и платежами;
- подписки VPN по отдельным хостам и глобальная подписка `ALL`;
- trial-период для VPN в логике глобальной подписки с общей `/sub/...` ссылкой;
- Telegram Proxy через MTG-хосты;
- оплаты через YooKassa, Telegram Stars, CryptoBot и ручной P2P;
- фоновые проверки: уведомления об истечении, синхронизация с панелями, обслуживание ключей, авто-провижининг глобальных подписок.

## Стек

- Python `3.10+`
- `aiogram`, `Flask`, `Waitress`
- `SQLite`
- `py3xui`
- `aiohttp`
- `docker compose`

## Быстрый старт

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

После первого запуска приложение:
- создаёт/обновляет `users.db`;
- переносит базовые значения из `.env` в таблицу `bot_settings`;
- поднимает Flask на порту `1488`;
- запускает основной бот, если заполнены `TELEGRAM_BOT_TOKEN` и `ADMIN_TELEGRAM_ID`; `TELEGRAM_BOT_USERNAME` можно указать вручную или получить автоматически через Bot API;
- запускает support-бот, если заполнены `SUPPORT_BOT_TOKEN` и `support_group_id` в настройках.

## Основные пути

- `src/shop_bot/__main__.py` — общий entrypoint
- `src/shop_bot/bot/` — пользовательский бот и support-бот
- `src/shop_bot/data_manager/` — SQLite, миграции, scheduler
- `src/shop_bot/modules/` — интеграции с 3x-ui и MTG
- `src/shop_bot/webhook_server/` — веб-админка и subscription API
- `scripts/` — служебные проверки и очистка cache-артефактов

## Support tickets

- support-бот создаёт и поддерживает `forum topic` на пользователя в группе поддержки;
- состояние тикета и история переписки сохраняются в SQLite, а не только в Telegram;
- если topic удалён или сломан, следующий входящий message пытается восстановить тикет в новом topic без потери истории;
- веб-админка больше не показывает отдельный раздел тикетов; support flow работает через Telegram и хранение истории в БД.

## Проверки перед выкладкой

```bash
python3 -m compileall -q src scripts
python3 scripts/check_callbacks.py
python3 scripts/check_fsm_transitions.py
python3 scripts/check_host_cleanup.py
python3 scripts/check_settings_defaults.py
```

## Документация

- `docs/architecture.md` — карта модулей и связей
- `docs/bot-flow.md` — пользовательские сценарии бота
- `docs/admin-panel.md` — разделы и действия в админке
- `docs/deployment.md` — запуск, backup/import, обновление
- `docs/env.md` — переменные окружения и настройки
- `docs/codebase-audit.md` — итог аудита, явный мусор и спорные места

## Важные замечания

- В проекте нет отдельной очереди и внешнего брокера: фоновые задачи работают в одном процессе через `asyncio`.
- `users.db`, `.env` и локальные backup-файлы считаются runtime-данными и не должны удаляться автоматически.
- Backup/import из админки работает с `.env` только по явному выбору в форме, а не автоматически.
- Встроенное обновление из админки теперь отказывается работать при dirty git worktree, чтобы не потерять локальные изменения.

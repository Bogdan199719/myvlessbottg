# Codebase Audit

## Что проверено

Во время аудита были просмотрены:

- структура репозитория;
- entrypoints и runtime;
- зависимости и deploy-файлы;
- bot handlers, keyboards, support flow;
- Flask routes и шаблоны админки;
- БД, миграции и scheduler;
- интеграции с 3x-ui, MTG, YooKassa, CryptoBot;
- служебные скрипты и текущая документация.

## Удалён только явный мусор

Удалены только нетрекаемые runtime-артефакты:

- `__pycache__/`
- `src/vless_shopbot.egg-info/`

Почему это безопасно:

- не являются исходным кодом;
- не трекаются git;
- генерируются при запуске и `pip install -e .`;
- не участвуют в бизнес-логике и деплое как репозиторные файлы.

## Под вопросом, оставлено без удаления

- [src/shop_bot/webhook_server/templates/debug_settings.html](/root/vless-shopbot/src/shop_bot/webhook_server/templates/debug_settings.html)
  Шаблон выглядит устаревшим и не подключён к текущим route, но файл tracked, поэтому автоматически не удалялся.
- `.env`
- `users.db`
- `users.db.bak_20260302_1314`
- старые callback aliases и compatibility-код в bot handlers

## Обнаруженные риски и особенности

- Обновление из админки использует `git reset --hard origin/main`.
- Веб-админка и боты живут в одном процессе; проблемы с loop/runtime могут затронуть обе части сразу.
- Настройки частично приходят из `.env`, но фактическая эксплуатационная конфигурация хранится в SQLite.
- Есть функции с сильным прод-эффектом: удаление хостов, revoke ключей, restore backup, force-update.

## Что обновлено в документации

- `README.md`
- `docs/architecture.md`
- `docs/bot-flow.md`
- `docs/admin-panel.md`
- `docs/deployment.md`
- `docs/env.md`
- `docs/codebase-audit.md`

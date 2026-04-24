# Architecture

## Общая схема

Проект запускается одним процессом из [src/shop_bot/__main__.py](/root/vless-shopbot/src/shop_bot/__main__.py). Внутри него поднимаются:

- основной Telegram-бот продаж;
- опциональный support-бот;
- Flask-веб-админка;
- периодический scheduler.

`Waitress` обслуживает HTTP, а основной `asyncio` event loop используется для ботов, scheduler и вызовов async-кода из Flask-роутов.

## Основные слои

### Bot layer

- [src/shop_bot/bot/handlers.py](/root/vless-shopbot/src/shop_bot/bot/handlers.py) — пользовательские сценарии, FSM оплаты, выдача ключей, триал, профиль, рефералка, P2P.
- [src/shop_bot/bot/keyboards.py](/root/vless-shopbot/src/shop_bot/bot/keyboards.py) — inline/reply-кнопки.
- [src/shop_bot/bot/support_handlers.py](/root/vless-shopbot/src/shop_bot/bot/support_handlers.py) — support-бот, восстановление тикетов и связка с forum topics.
- [src/shop_bot/bot/middlewares.py](/root/vless-shopbot/src/shop_bot/bot/middlewares.py) — защита от callback race и блокировка banned users.

### Service/runtime layer

- [src/shop_bot/bot_controller.py](/root/vless-shopbot/src/shop_bot/bot_controller.py) — запуск, остановка и статус обоих ботов.
- [src/shop_bot/data_manager/scheduler.py](/root/vless-shopbot/src/shop_bot/data_manager/scheduler.py) — уведомления, sync с панелями, авто-провижининг, контроль активных/просроченных доступов.

### Data layer

- [src/shop_bot/data_manager/database.py](/root/vless-shopbot/src/shop_bot/data_manager/database.py) — SQLite-схема, миграции и CRUD.
- Базовые таблицы: `users`, `vpn_keys`, `transactions`, `bot_settings`, `xui_hosts`, `mtg_hosts`, `plans`, `payment_method_rules`, `p2p_requests`, `support_threads`, `support_tickets`, `support_messages`, `vpn_keys_missing`, `sent_notifications`.

### Integration layer

- [src/shop_bot/modules/xui_api.py](/root/vless-shopbot/src/shop_bot/modules/xui_api.py) — 3x-ui login, create/update/delete client, connection strings, panel sync.
- [src/shop_bot/modules/mtg_api.py](/root/vless-shopbot/src/shop_bot/modules/mtg_api.py) — MTG token cache, create/renew/delete/toggle proxy.
- [src/shop_bot/webhook_server/subscription_api.py](/root/vless-shopbot/src/shop_bot/webhook_server/subscription_api.py) — `/sub/<token>` для глобальной подписки и active global trial.

### Admin web layer

- [src/shop_bot/webhook_server/app.py](/root/vless-shopbot/src/shop_bot/webhook_server/app.py) — login, dashboard, users, keys, settings, host management, payment webhooks, update routes.
- [src/shop_bot/webhook_server/templates/](/root/vless-shopbot/src/shop_bot/webhook_server/templates) — HTML-шаблоны.
- [src/shop_bot/webhook_server/static/](/root/vless-shopbot/src/shop_bot/webhook_server/static) — CSS/JS/изображения.

## Потоки данных

1. Пользователь выбирает тариф в боте.
2. Бот создаёт pending transaction и переводит пользователя к оплате.
3. После webhook или Telegram payment вызывается `process_successful_payment`.
4. Выдача происходит через 3x-ui или MTG.
5. Результат пишется в `vpn_keys` и `transactions`.
6. Scheduler дальше поддерживает срок, статус и уведомления.

## Особенности

- Бизнес-данные и настройки живут в SQLite; `.env` используется как первичный источник при первом старте.
- Админка и бот используют одну базу и один event loop.
- Support flow не полагается только на Telegram topic: история сообщений и статус доставки сохраняются в БД даже без отдельного раздела тикетов в админке.
- В админке есть ручные операции с реальными прод-эффектами: удаление клиентов на панелях, backup/import БД, force-update приложения.
- Restore и export backup умеют работать с `.env`, но только по явному выбору пользователя в форме.

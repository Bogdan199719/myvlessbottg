# Architecture

## Общая схема

Проект запускается одним процессом из `src/shop_bot/__main__.py`. Внутри него поднимаются:

- основной Telegram-бот продаж;
- опциональный support-бот;
- Flask-веб-админка;
- периодический maintenance/notification scheduler и отдельный цикл проверки
  и восстановления подтверждённых платежей.

`Waitress` обслуживает HTTP, а основной `asyncio` event loop используется для ботов, scheduler и вызовов async-кода из Flask-роутов.

## Основные слои

### Bot layer

- `src/shop_bot/bot/handlers.py` — пользовательские сценарии, FSM оплаты, выдача ключей, триал, профиль, рефералка, P2P.
- `src/shop_bot/bot/keyboards.py` — inline/reply-кнопки.
- `src/shop_bot/bot/support_handlers.py` — support-бот, восстановление тикетов и связка с forum topics.
- `src/shop_bot/bot/middlewares.py` — защита от callback race и блокировка banned users.

### Service/runtime layer

- `src/shop_bot/bot_controller.py` — запуск, остановка и статус обоих ботов.
- `src/shop_bot/data_manager/scheduler.py` — уведомления, sync с панелями, авто-провижининг, контроль активных/просроченных доступов.

### Data layer

- `src/shop_bot/data_manager/database.py` — SQLite-схема, миграции и CRUD.
- Основные таблицы: `users`, `vpn_keys`, `transactions`, `bot_settings`, `xui_hosts`, `mtg_hosts`, `plans`, `payment_method_rules`, `processed_webhooks`, `p2p_requests`, `promo_codes`, `promo_code_redemptions`, `profit_distributions`, `support_threads`, `support_tickets`, `support_messages`, `vpn_keys_missing`, `sent_notifications`.

### Integration layer

- `src/shop_bot/modules/xui_api.py` — 3x-ui login/API-token auth, create/update/delete client, connection strings, panel sync.
- `src/shop_bot/modules/mtg_api.py` — MTG token cache, create/renew/delete/toggle proxy.
- `src/shop_bot/webhook_server/subscription_api.py` — `/sub/<token>` для глобальной подписки и active global trial; `/happ/<token>` и `/incy/<token>` для HTTPS-редиректов в deeplink соответствующего приложения.

3x-ui API compatibility:

- `py3xui==0.7.0` provides native Bearer-token and CSRF-aware session authentication;
- legacy panels use `/panel/api/inbounds/addClient`, `/updateClient`, `/:id/delClient` and traffic endpoints through the py3xui client;
- current 3x-ui panels also expose `/panel/api/clients/add`, `/update/:email`, `/del/:email`, `/:email/attach`, `/resetTraffic/:email` and `/traffic/:email`;
- integration code prefers the current clients API and falls back to legacy inbound routes only when those endpoints are unavailable;
- current panels may return optional `streamSettings` as `null` and default `sniffing` as an empty object; the integration normalises these valid wire values before validation by `py3xui==0.7.0`;
- Hysteria/Hysteria2 clients still use the raw JSON API because the SDK client model does not expose the panel's `auth` field. Payloads are normalised so numeric fields such as `tgId`, `expiryTime`, `totalGB` and `limitIp` are sent as numbers for newer Go validators.

### Admin web layer

- `src/shop_bot/webhook_server/app.py` — login, dashboard, users, keys, settings, host management, payment webhooks, update routes.
- `src/shop_bot/webhook_server/templates/` — HTML-шаблоны.
- `src/shop_bot/webhook_server/static/` — CSS/JS/изображения.

## Потоки данных

1. Пользователь выбирает тариф в боте.
2. Бот создаёт pending transaction и переводит пользователя к оплате.
3. После webhook или Telegram payment вызывается `process_successful_payment`.
4. Выдача происходит через 3x-ui или MTG.
5. Результат пишется в `vpn_keys` и `transactions`.
6. Для VPN пользователь получает постоянную `/sub/<token>` ссылку и расположенные в одном ряду кнопки `🔌 Happ` и `📲 Incy`; отдельные XUI-ключи по хостам остаются техническими записями для 3x-ui, но не продаются и не показываются пользователю как отдельный товар.
7. Кнопки ведут на HTTPS endpoints `/happ/<token>` и `/incy/<token>`. После проверки token сервер отвечает соответственно `302 Location: happ://add/https://<domain>/sub/<token>` или `302 Location: incy://import/https://<domain>/sub/<token>`.
8. Scheduler дальше поддерживает срок, статус, уведомления и довыдачу недостающих включённых XUI-хостов для active global paid и active global trial.

Для provider-confirmed платежа локальная metadata является источником durable checkpoint-полей `fulfillment_*`. Webhook и фоновая проверка YooKassa сохраняют эти поля, `accounting_applied` и отметку восстановления прерванной выдачи из актуальной строки БД. Чтение, объединение с данными провайдера и резервирование заказа выполняются в одной короткой SQLite-транзакции; внешних запросов внутри неё нет. Повтор не меняет абсолютную цель выдачи, стабильный email клиента или checkpoint пользовательского уведомления. Для остальных способов оплаты правила резервирования не изменены.

Если после резервирования YooKassa основной бот или обработчик выдачи недоступен, заказ возвращается в `pending`, а webhook отвечает `503` для повторной попытки. Успешным обработанным уведомлением такой запрос не помечается.

Новые заказы получают локальный `fulfillment_protocol_version` при создании, а резервирование YooKassa сохраняет `processing_instance_id` текущего процесса. При запуске единственного экземпляра приложения, после остановки предыдущего и до создания Flask, ботов и фоновых задач, ранние заказы YooKassa предыдущего процесса могут вернуться в `pending`. Это разрешено только для новой версии протокола, без сохранённой цели, финансового учёта или признаков дальнейших этапов выдачи. Перед выдачей webhook или scheduler снова проверяет оплату у YooKassa. Исторические заказы без маркера, неизвестные состояния, другие провайдеры и заказы текущего процесса так не восстанавливаются.

Периодическое восстановление по-прежнему требует сохранённой абсолютной цели выдачи и достаточного возраста `processing`-заказа. Оба вида восстановления изменяют статус только если исходная строка metadata осталась прежней: сохранённый другим обработчиком checkpoint не перезаписывается старым снимком.

Промокод использует отдельный durable reservation в `promo_code_redemptions`. До обращения к 3x-ui в нём сохраняется абсолютная дата окончания выдачи. Если обработана только часть хостов, пользователь повторно вводит тот же код, а уже успешные хосты получают ту же дату без повторного начисления дней.

## Особенности

- Бизнес-данные и настройки живут в SQLite; `.env` используется как первичный источник при первом старте.
- Админка и бот используют одну базу и один event loop.
- Блокирующие вызовы 3x-ui выполняются вне общего event loop. Ручное обслуживание ограничивает параллелизм физическими панелями и сохраняет последовательный порядок logical hosts одной панели.
- Ручные sync/fix/maintenance задачи и массовые административные операции имеют process-local защиту от повторного параллельного запуска.
- Все записи клиентов в одну физическую 3x-ui панель сериализованы общей
  process-local блокировкой, включая preflight и XTLS. Блокировка остаётся у
  worker thread до завершения сетевой операции, даже если ожидающий coroutine
  получил timeout/cancel. Независимые панели могут обрабатываться параллельно.
- Выдача после оплаты, промокода, ручная выдача глобального тарифа и установка
  срока через админку используют `modules/panel_jobs.py`: одновременно работают
  максимум пять физических панелей в рамках одной операции, а подключения одной
  панели обрабатываются по очереди. Результат каждого ключа сразу сохраняется
  в SQLite; частичная выдача не считается завершённой оплатой. Отмена операции
  отменяет ожидающие задания, сохраняя блокировку у уже работающего HTTP worker.
- При выдаче ключа используется inbound, только что прочитанный под блокировкой
  панели: повторная загрузка того же inbound перед изменением не выполняется.
  Остальные сценарии сохраняют чтение актуального inbound. Настройки и inbound
  между отдельными операциями не кешируются. Журнал `XUI subscription batch`
  показывает количество подключений, панелей и длительность, без идентификаторов
  пользователей и платёжных данных.
- Платёжные проверки выполняются в отдельном периодическом цикле и не ждут
  завершения полного maintenance прохода. Выдача и уведомления сохраняют свои
  существующие правила и durable checkpoints.
- Support flow не полагается только на Telegram topic: история сообщений и статус доставки сохраняются в БД даже без отдельного раздела тикетов в админке.
- В админке есть ручные операции с реальными прод-эффектами: удаление клиентов на панелях, backup/import БД, force-update приложения.
- Trial, платёж, промокод и ручная выдача считают хост успешным только после подтверждённой записи результата в SQLite.
- Restore и export backup умеют работать с `.env`, но только по явному выбору пользователя в форме.

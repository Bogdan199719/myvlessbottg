# Environment And Settings

## Что реально читается из `.env`

При первом запуске проект переносит базовые значения из `.env` в `bot_settings`.

Основные переменные:

- `TELEGRAM_BOT_TOKEN`
- `ADMIN_TELEGRAM_ID`
- `DOMAIN`
- `PANEL_LOGIN`
- `PANEL_PASSWORD`
- `SUPPORT_BOT_TOKEN`
- `TELEGRAM_BOT_USERNAME`
- `YOOKASSA_SHOP_ID`
- `YOOKASSA_SECRET_KEY`
- `YOOKASSA_ENABLED`
- `CRYPTOBOT_ENABLED`
- `CRYPTOBOT_TOKEN`
- `CRYPTOBOT_WEBHOOK_SECRET`
- `DB_PATH`
- `FLASK_SECRET_KEY`
- `ENABLE_WEB_UPDATES`

`DB_PATH` влияет напрямую на путь к SQLite. Остальные значения в основном копируются в `bot_settings` и дальше редактируются уже через админку.

`ENABLE_WEB_UPDATES=false` оставляет встроенный update-manager выключенным. При
включении он использует агрессивное обновление через `git reset --hard
origin/main`, поэтому для Docker production предпочтителен ручной rebuild.

`FLASK_SECRET_KEY` используется для подписи Flask-сессий админки. Он должен быть случайным и длинным: минимум 32 символа, лучше 64 hex-символа. Известные placeholder-значения из шаблона считаются небезопасными; при их обнаружении приложение использует сохранённый сильный ключ из БД или генерирует новый.

## Важные настройки в `bot_settings`

### Базовые

- `panel_login`
- `panel_password`
- `telegram_bot_token`
- `support_bot_token`
- `telegram_bot_username`
- `admin_telegram_id`
- `support_group_id`
- `domain`
- `flask_secret_key`

### Контент и ссылки

- `about_text`
- `support_text`
- `support_user`
- `channel_url`
- `terms_url`
- `privacy_url`
- `subscription_name`

### Trial и рефералка

- `trial_enabled`
- `trial_duration_days`
- `trial_duration_value`
- `trial_duration_unit`
- `enable_referrals`
- `referral_percentage`
- `referral_discount`
- `minimum_withdrawal`

### Оплаты

- `yookassa_enabled`
- `yookassa_shop_id`
- `yookassa_secret_key`
- `sbp_enabled`
- `receipt_email`
- `cryptobot_enabled`
- `cryptobot_token`
- `cryptobot_webhook_secret`
- `p2p_enabled`
- `p2p_card_number`
- `stars_enabled`
- `stars_rub_per_star`
- `email_prompt_enabled`

### Подписки и sync

- `subscription_live_sync`
- `subscription_live_stats`
- `subscription_allow_fallback_host_fetch`
- `subscription_auto_provision` (зарезервирована; текущие пути provisioning её не учитывают)
- `happ_routing_enabled`
- `happ_routing_rules`
- `provision_timeout_seconds`
- `panel_sync_enabled`
- `xtls_sync_enabled`
- `enable_global_plans`

## Практический смысл

- `.env` нужен для первого старта и аварийного восстановления.
- `.env` содержит токены и пароли, поэтому файл должен иметь права `600`. `install.sh` скрывает ввод Telegram bot token и выставляет `chmod 600` при генерации.
- Для запуска основного бота нужны `telegram_bot_token` и `admin_telegram_id`; `telegram_bot_username` можно заполнить вручную или дать приложению получить его через Bot API.
- Для запуска support-бота нужен не только `support_bot_token`, но и `support_group_id` в `bot_settings`.
- `email_prompt_enabled` относится только к YooKassa и влияет на запрос email перед созданием YooKassa-платежа.
- Если `email_prompt_enabled=false`, для YooKassa должен быть указан реальный `receipt_email`. Placeholder-адреса вроде `example@example.com` игнорируются, и метод оплаты YooKassa не показывается пользователю как доступный.
- Backup/import из админки не включает и не применяет `.env` автоматически: это отдельные опции в форме.
- После запуска основная операционная конфигурация редактируется через `/settings`.
- Если меняется только `.env`, это не гарантирует обновление значений в БД без ручной синхронизации.
- Если вручную меняется `FLASK_SECRET_KEY`, все существующие Flask-сессии админки станут недействительными, и администратору нужно будет войти снова.

## Платёжные webhook-и

- YooKassa webhook повторно проверяет платёж через YooKassa API, сверяет статус, сумму и валюту `RUB`.
- CryptoBot webhook при настроенном `cryptobot_token` требует HMAC-подпись Crypto Pay. Path-secret endpoint оставлен только для совместимости маршрута, но не заменяет HMAC при наличии token.
- Telegram Stars сверяет валюту `XTR` и ожидаемое количество Stars, сохранённое при создании invoice.
- P2P-заявки подтверждаются вручную администратором; пользователь может отправить на проверку только свою заявку.
- Если оплаченный CryptoBot webhook приходит без локальной pending-транзакции, backend возвращает `503`, чтобы провайдер повторил доставку; `200` отдаётся только для уже оплаченной или уже обрабатываемой локальной транзакции.

## Примечание по trial

- В текущей логике trial для VPN ведёт себя как глобальная подписка и выдаёт общую `/sub/...` ссылку.
- Trial доступен только до первой платной VPN-подписки. Если пользователь сначала купил VPN, кнопка trial больше не показывается и старые callback-кнопки не выдают пробный доступ.
- Основная рабочая настройка длительности сейчас берётся из `trial_duration_days`.
- `trial_duration_value` и `trial_duration_unit` сохраняются в БД, но не являются основным источником длительности в текущем коде.
- Активный trial покрывает все включённые XUI-хосты так же, как paid global.
- Платёж за `ALL` финализируется как `paid` только после успешной записи всех включённых XUI-хостов. Если часть хостов временно не выдалась, транзакция остаётся в retry, а повторная выдача использует сохранённую абсолютную дату окончания подписки.
- Scheduler обрабатывает active trial как глобальную подписку и отправляет одно уведомление на пользователя/окно окончания, а не по одному уведомлению на каждый технический XUI-хост.
- XUI-тарифы должны быть только `host_name=ALL`; per-host XUI-тарифы в админке не создаются и не выдаются вручную.

## Примечание по 3x-ui API

- Для старых панелей используется legacy API `/panel/api/inbounds/*`.
- Для актуальных панелей поддержан fallback на `/panel/api/clients/*`.
- При добавлении хоста админка проверяет не только чтение inbound, но и запись: создаёт и удаляет тестового клиента. Если preflight не проходит, такой хост нельзя считать рабочим для продаж и trial.

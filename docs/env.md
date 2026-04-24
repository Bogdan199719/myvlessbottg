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

`DB_PATH` влияет напрямую на путь к SQLite. Остальные значения в основном копируются в `bot_settings` и дальше редактируются уже через админку.

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
- `subscription_auto_provision`
- `panel_sync_enabled`
- `xtls_sync_enabled`
- `enable_global_plans`

## Практический смысл

- `.env` нужен для первого старта и аварийного восстановления.
- Для запуска основного бота нужны `telegram_bot_token` и `admin_telegram_id`; `telegram_bot_username` можно заполнить вручную или дать приложению получить его через Bot API.
- Для запуска support-бота нужен не только `support_bot_token`, но и `support_group_id` в `bot_settings`.
- `email_prompt_enabled` относится только к YooKassa и влияет на запрос email перед созданием YooKassa-платежа.
- Backup/import из админки не включает и не применяет `.env` автоматически: это отдельные опции в форме.
- После запуска основная операционная конфигурация редактируется через `/settings`.
- Если меняется только `.env`, это не гарантирует обновление значений в БД без ручной синхронизации.

## Примечание по trial

- В текущей логике trial для VPN ведёт себя как глобальная подписка и выдаёт общую `/sub/...` ссылку.
- Основная рабочая настройка длительности сейчас берётся из `trial_duration_days`.
- `trial_duration_value` и `trial_duration_unit` сохраняются в БД, но не являются основным источником длительности в текущем коде.

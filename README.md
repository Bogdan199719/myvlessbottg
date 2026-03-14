# MyVlessBot

MyVlessBot - Telegram-бот для продажи VLESS/VPN-ключей с веб-панелью управления, интеграцией с 3x-ui и поддержкой нескольких способов оплаты.

## Что умеет проект

- автоматически выдавать и продлевать ключи после оплаты;
- управлять тарифами, хостами, пользователями и платежами через веб-панель;
- работать с несколькими 3x-ui хостами и глобальными подписками;
- поддерживать trial, рефералы и встроенную поддержку пользователей;
- принимать платежи через YooKassa, CryptoBot, Heleket, TON и Telegram Stars.

## Быстрый запуск

Рекомендуемый способ установки:

```bash
curl -sSL https://raw.githubusercontent.com/Bogdan199719/myvlessbottg/main/install.sh | sudo bash
```

Скрипт установит зависимости, создаст `.env`, настроит Nginx и поднимет контейнеры.

Ручной запуск через Docker:

```bash
git clone https://github.com/Bogdan199719/myvlessbottg.git
cd myvlessbottg
docker-compose up -d --build
```

## Основные файлы

- `src/shop_bot/` - основная логика бота и приложения;
- `src/shop_bot/bot/` - Telegram-обработчики и клавиатуры;
- `src/shop_bot/webhook_server/` - Flask-панель, webhook-маршруты и шаблоны;
- `src/shop_bot/data_manager/` - работа с SQLite и планировщик;
- `src/shop_bot/modules/xui_api.py` - интеграция с 3x-ui;
- `scripts/` - служебные проверки проекта.

## Конфигурация

Минимально нужны:

- `TELEGRAM_BOT_TOKEN`
- `ADMIN_TELEGRAM_ID`
- `DOMAIN`

Дополнительные токены, платежные ключи и параметры панели задаются через `.env` и админ-панель.

## Проверки перед обновлением

```bash
python3 -m compileall -q src scripts
python3 scripts/check_callbacks.py
python3 scripts/check_fsm_transitions.py
```

## Безопасность

- не коммитьте `.env`, `users.db`, резервные копии баз и логи;
- не храните реальные API-ключи и токены в tracked-файлах;
- после первой установки задайте собственный логин и пароль панели;
- перед обновлением сохраняйте `.env` и рабочую базу отдельно.

## Обновление

Обновление кода не должно затирать `.env` и `users.db`. После изменения платежных провайдеров, webhook-логики или настроек 3x-ui проверяйте админ-панель и сценарии оплаты вручную.

## Лицензия

MyVlessBot © 2024 Bogdan199719. All rights reserved.

<div align="center">

# MyVlessBot

**Telegram-бот для автоматической продажи VLESS/VPN-ключей**

[![Version](https://img.shields.io/badge/version-2.4.20-blue?style=flat-square)](https://github.com/Bogdan199719/myvlessbottg/releases)
[![Python](https://img.shields.io/badge/python-3.10+-green?style=flat-square&logo=python)](https://python.org)
[![License](https://img.shields.io/badge/license-Proprietary-red?style=flat-square)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?style=flat-square&logo=docker)](docker-compose.yml)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2AABEE?style=flat-square&logo=telegram)](https://aiogram.dev)

*Полностью автоматизированная торговля VPN-подписками с 4 способами оплаты, веб-панелью управления и поддержкой нескольких 3x-ui серверов*

</div>

---

## Что умеет бот

| Возможность | Описание |
|---|---|
| **Автоматическая выдача ключей** | После оплаты пользователь мгновенно получает VLESS/VMess/Trojan-ключ |
| **Продление подписок** | Автоматическое и ручное продление, уведомления за 3/7 дней до истечения |
| **Несколько серверов** | Поддержка множества 3x-ui хостов, глобальные подписки |
| **Реферальная система** | Вознаграждение за приглашённых пользователей, баланс в рублях |
| **Триал-доступ** | Настраиваемый пробный период для новых пользователей |
| **Встроенная поддержка** | Тикет-система через отдельного support-бота |
| **Веб-панель администратора** | Flask-интерфейс для управления тарифами, хостами, пользователями и платежами |
| **4 способа оплаты** | YooKassa, Telegram Stars, CryptoBot, P2P-перевод |

---

## Способы оплаты

| Платёжная система | Валюта | Тип |
|---|---|---|
| **YooKassa** | RUB | Банковские карты, СБП |
| **Telegram Stars** | Stars | Нативные платежи в Telegram |
| **CryptoBot** | USDT, TON, BTC и др. | Криптовалюта |
| **P2P (карта)** | RUB | Ручное подтверждение администратором |

---

## Технологии

| Компонент | Технология |
|---|---|
| Telegram-бот | Python 3.10+, [aiogram 3.x](https://aiogram.dev) |
| Веб-панель | [Flask](https://flask.palletsprojects.com) + Jinja2 |
| База данных | SQLite (9 таблиц) |
| Планировщик | Async-планировщик (проверка подписок, уведомления) |
| VPN-интеграция | [3x-ui](https://github.com/MHSanaei/3x-ui) REST API |
| Протоколы | VLESS, VMess, Trojan (Reality, TLS, WebSocket, gRPC, TCP) |
| Деплой | Docker + Nginx + Let's Encrypt |

---

## Быстрая установка

### Автоматически (рекомендуется)

```bash
curl -sSL https://raw.githubusercontent.com/Bogdan199719/myvlessbottg/main/install.sh | sudo bash
```

Скрипт сам установит зависимости, запросит необходимые данные, настроит Nginx и SSL-сертификат, поднимет Docker-контейнеры.

### Вручную (Docker)

```bash
# 1. Клонировать репозиторий
git clone https://github.com/Bogdan199719/myvlessbottg.git
cd myvlessbottg

# 2. Создать и заполнить файл конфигурации
cp .env.example .env
nano .env

# 3. Запустить
docker-compose up -d --build

# 4. Просмотр логов
docker-compose logs -f
```

---

## Конфигурация

### Обязательные переменные `.env`

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен основного бота от [@BotFather](https://t.me/BotFather) |
| `ADMIN_TELEGRAM_ID` | Telegram ID администратора |
| `DOMAIN` | Домен сервера (для вебхука и SSL) |
| `PANEL_LOGIN` | Логин 3x-ui панели |
| `PANEL_PASSWORD` | Пароль 3x-ui панели |

### Дополнительные переменные `.env`

| Переменная | Описание |
|---|---|
| `DB_PATH` | Путь к SQLite-базе (по умолчанию `./users.db`) |
| `SUPPORT_BOT_TOKEN` | Токен отдельного бота для поддержки |
| `TELEGRAM_BOT_USERNAME` | Username бота (без @) |
| `YOOKASSA_SHOP_ID` | ID магазина YooKassa |
| `YOOKASSA_SECRET_KEY` | Секретный ключ YooKassa |
| `CRYPTOBOT_TOKEN` | API-токен CryptoBot |
| `CRYPTOBOT_WEBHOOK_SECRET` | Webhook-секрет CryptoBot |

> **Примечание:** Большинство настроек (тарифы, тексты, реферальная система, включение/отключение способов оплаты) настраиваются через веб-панель администратора, а не через `.env`.

---

## Архитектура

```
myvlessbottg/
├── src/shop_bot/
│   ├── __main__.py          # Точка входа: asyncio + Flask + aiogram
│   ├── bot_controller.py    # Запуск и остановка бота
│   ├── config.py            # Загрузка конфигурации
│   ├── version.py           # Версия приложения
│   │
│   ├── bot/                 # Telegram-обработчики
│   │   ├── handlers.py      # Основная логика: покупка, продление, платежи
│   │   ├── keyboards.py     # Inline и Reply клавиатуры
│   │   ├── middlewares.py   # Проверка бана, антиспам
│   │   └── support_handlers.py  # Тикет-система поддержки
│   │
│   ├── data_manager/        # Работа с данными
│   │   ├── database.py      # SQLite: схема из 9 таблиц + CRUD
│   │   ├── database_helpers.py  # Вспомогательные функции
│   │   └── scheduler.py     # Фоновые задачи: уведомления, продления
│   │
│   ├── modules/
│   │   └── xui_api.py       # Интеграция с 3x-ui REST API
│   │
│   ├── utils/
│   │   ├── time_utils.py    # Работа с датами и временем
│   │   └── update_manager.py  # Проверка и применение обновлений
│   │
│   └── webhook_server/      # Flask веб-панель
│       ├── app.py           # Маршруты панели администратора
│       ├── subscription_api.py  # API подписок
│       ├── templates/       # HTML-шаблоны (Jinja2)
│       └── static/          # CSS, JS, изображения
│
├── scripts/                 # Утилиты для проверки кода
├── docker-compose.yml       # Docker-конфигурация
├── Dockerfile               # Образ приложения
└── install.sh               # Автоматический установщик
```

### Схема базы данных

| Таблица | Назначение |
|---|---|
| `users` | Пользователи: баланс, рефералы, статус |
| `vpn_keys` | Выданные VPN-ключи с датой истечения |
| `transactions` | Все платежи и их статусы |
| `bot_settings` | Настройки бота (key-value) |
| `xui_hosts` | Список 3x-ui серверов |
| `plans` | Тарифы: цена, срок, привязка к хосту |
| `vpn_keys_missing` | Ключи, отсутствующие на панели |
| `support_threads` | Тикеты поддержки |
| `sent_notifications` | Журнал отправленных уведомлений |

---

## Интеграция с 3x-ui

Бот подключается к [3x-ui](https://github.com/MHSanaei/3x-ui) по REST API и умеет:

- Создавать VPN-аккаунты с UUID, лимитами трафика и сроком действия
- Продлевать и удалять клиентов
- Генерировать строки подключения для VLESS / VMess / Trojan
- Поддерживать транспорты: **Reality**, **TLS**, **WebSocket**, **gRPC**, **TCP**
- Синхронизировать базу данных с состоянием панели
- Сбрасывать счётчик трафика для «исчерпавших» клиентов
- Работать с несколькими хостами одновременно

---

## Веб-панель администратора

Flask-панель доступна по адресу `https://ваш-домен/` и позволяет:

- Управлять пользователями (просмотр, бан, начисление баланса)
- Создавать и редактировать тарифы
- Добавлять и отключать 3x-ui хосты
- Просматривать транзакции и статистику
- Настраивать все параметры бота (тексты, реферальная программа, оплата)
- Управлять VPN-ключами вручную

---

## Управление и обновление

```bash
# Просмотр логов
docker-compose logs -f

# Перезапуск
docker-compose restart

# Обновление
git pull
docker-compose up -d --build

# Проверка кода перед обновлением
python3 -m compileall -q src scripts
python3 scripts/check_callbacks.py
python3 scripts/check_fsm_transitions.py
```

> **Важно:** При обновлении сохраняйте резервную копию `.env` и `users.db`.

---

## Безопасность

- Не коммитьте `.env`, `users.db` и резервные копии базы — они в `.gitignore`
- После установки смените логин и пароль панели 3x-ui
- Используйте сложный пароль для `PANEL_PASSWORD` (скрипт может сгенерировать автоматически)
- Ограничьте доступ к порту `1488` (Flask) через Nginx — он уже настраивается скриптом
- Регулярно обновляйте зависимости: `docker-compose build --no-cache`

---

## Лицензия

Copyright © 2024–2026 **Bogdan199719**. All rights reserved.

Данный программный продукт является проприетарным. Распространение, модификация и коммерческое использование без явного письменного разрешения автора запрещены.

---

<div align="center">

Если проект оказался полезным — поставьте ⭐ на GitHub

</div>

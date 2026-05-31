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

- `.env`
- `users.db`
- локальные `users.db.backup_*` / `users.*backup*.db`
- старые callback aliases и compatibility-код в bot handlers

## Обнаруженные риски и особенности

- Обновление из админки использует `git reset --hard origin/main`.
- Веб-админка и боты живут в одном процессе; проблемы с loop/runtime могут затронуть обе части сразу.
- Настройки частично приходят из `.env`, но фактическая эксплуатационная конфигурация хранится в SQLite.
- Есть функции с сильным прод-эффектом: удаление хостов, revoke ключей, restore backup, force-update.

## Аудит 2026-05-21

### Исправлено

- `install.sh` теперь корректно устанавливает отсутствующие системные зависимости и останавливается при ошибке установки.
- При генерации `.env` через `install.sh` создаётся случайный `FLASK_SECRET_KEY`.
- Приложение больше не использует известные placeholder-значения `FLASK_SECRET_KEY`.
- `/healthz` в обновлённом коде возвращает только минимальный статус, без раскрытия состояния БД, event loop и ботов.
- YooKassa webhook проверяет валюту `RUB`.
- CryptoBot webhook при настроенном token требует HMAC-подпись, отклоняет legacy payload без pending transaction id и проверяет fiat/RUB.
- Telegram Stars проверяет валюту `XTR` и ожидаемое количество Stars.
- P2P callback проверяет владельца заявки.
- P2P command approve удаляет заявку до выдачи доступа и восстанавливает её только при сбое, чтобы избежать двойной выдачи.
- Forced channel subscription больше не проходит fail-open при неверном `channel_url`.

### Требует решения владельца

- `ALL`-подписка сейчас может быть частично выдана по нескольким хостам. Простое повторение webhook-а опасно двойным продлением уже успешных хостов; нужен host-level ledger выдачи или отдельная retry-модель.
- `GET /sub/<token>` может выполнять auto-provision глобальной подписки. Это удобно как self-healing, но публичный bearer-token endpoint получает side effects. Нужно решить, оставить это поведение или перенести provisioning в scheduler/admin flow.
- Restore SQLite при WAL-режиме лучше переводить в maintenance/restart flow либо дополнить checkpoint-логикой.
- Production Docker лучше перевести на отдельный volume для БД и immutable image для кода вместо writable bind mount всего репозитория.
- Legacy naive timestamps требуют единой политики интерпретации и, возможно, миграции данных.

### Production-наблюдение

На момент проверки контейнер был `healthy`, scheduler выполнял циклы, shop/support bot были активны. В логах замечен не связанный с этим аудитом runtime error `Unsupported protocol: hysteria` при обработке одного из XUI-хостов; позже интеграция 3x-ui была доработана для Hysteria/Hysteria2 через raw JSON API.

## Что обновлено в документации

- `README.md`
- `docs/architecture.md`
- `docs/bot-flow.md`
- `docs/admin-panel.md`
- `docs/deployment.md`
- `docs/env.md`
- `docs/codebase-audit.md`

## Аудит 2026-05-23

### Исправлено

- Scheduler больше не отправляет уведомления об истечении старых XUI-ключей, если у пользователя есть активная глобальная XUI-подписка. MTG proxy остаётся отдельным продуктом и уведомляется отдельно.
- Support-сводка по тикету различает глобальные и обычные ключи, показывает последнюю реальную оплату отдельно от промокодов/служебных операций и указывает неполную выдачу как `3/4`, если включённый хост недоступен.
- Fallback-проверка pending-платежей теперь обрабатывает свежие записи первыми, чтобы старые неоплаченные счета не блокировали новые зависшие платежи при лимите выборки.
- Scheduler получил backoff для временно сломанных XUI-хостов, чтобы один недоступный сервер не создавал постоянный поток ошибок каждую минуту.
- Интеграция 3x-ui приведена к текущему API: Bearer token для v3, официальный `/panel/api/clients/links/:email` для ссылок, fallback на legacy route для старых панелей и protocol-aware `clientId` (`id`, `password`, `email`, `auth`).
- Hysteria/Hysteria2 поддерживаются через raw JSON API 3x-ui с полем `auth`, потому что установленная версия `py3xui` не моделирует это поле в `Client`.

### Runtime-решение

- Хост `Сервер Niderland 🇳🇱` сначала был временно отключён из-за `404` на 3x-ui API. Корневая причина оказалась в устаревшем `api_token` только у этой строки хоста; один URL панели с разными inbound корректен.
- После очистки токена хост снова включён. `Сервер Niderland 🇳🇱` использует Hysteria inbound, `Niderland2` использует VLESS inbound на той же панели.
- Для пользователей с уже выданной глобальной подпиской обновлены/довыданы недостающие Hysteria-ключи и connection string.

### Перед добавлением или повторным включением XUI-хоста

- Проверить `host_url`, `host_inbound_id`, логин/пароль или `api_token` в админке.
- Убедиться, что API 3x-ui доступен по настроенному пути и inbound существует.
- После включения проверить логи scheduler и `/sub/<token>` на успешную довыдачу ключей.

## Аудит 2026-05-25

### Проверено

- `python3 -m compileall -q src scripts`
- `scripts/check_callbacks.py`
- `scripts/check_fsm_transitions.py`
- `scripts/check_host_cleanup.py`
- `scripts/check_settings_defaults.py`
- `bash -n install.sh`
- `docker compose config --quiet`
- `git diff --check`
- Flask route map и `url_for(...)` в шаблонах внутри контейнера
- `PRAGMA integrity_check` для `users.db`
- Docker healthcheck `/healthz`
- последние runtime-логи контейнера на `ERROR`, `WARNING`, `Traceback`

### Исправлено

- Убраны устаревшие абсолютные ссылки из документации. Документация теперь использует относительные пути и не зависит от имени директории на сервере.

### Итог

- На момент аудита контейнер `myvlessbottg-bot-1` был `healthy`.
- `/healthz` возвращал `{"status": "ok"}`.
- База прошла `PRAGMA integrity_check`.
- В последних проверенных логах не было `ERROR`, `WARNING` или traceback.
- Явных runtime-багов, требующих правки кода и перезапуска, не найдено.

## Аудит 2026-05-29

### Инвентаризация production

- Проект находится в `/root/myvlessbottg`.
- Запущен один контейнер `myvlessbottg-bot-1`, статус `healthy`, порт опубликован только на `127.0.0.1:1488`.
- Наружу открыты `22/tcp`, `80/tcp`, `443/tcp`; nginx проксирует `panel.stopurban.ru` на локальный Flask/Waitress.
- SSL Let's Encrypt для `panel.stopurban.ru` действителен до `2026-07-23`.
- Основная БД: `users.db`, права `600`; `.env` не tracked в git и также имеет права `600`.
- В БД на момент аудита: 309 пользователей, 555 VPN-ключей, 3 XUI-хоста, 4 тарифа, 128 paid transaction и 43 pending transaction.
- Активные global/trial подписки покрывают все 3 включённых XUI-хоста.
- SSH по требованию владельца должен оставаться открытым с парольным входом. Это осознанный эксплуатационный риск; автоматические изменения SSH, firewall, паролей и ключей не выполнялись.

### Исправлено

- `process_successful_payment()` теперь сбрасывает `users.pending_payment`, если отправка processing-сообщения в Telegram падает до основного блока fulfillment.
- Удаление processing-сообщения после успешной выдачи VPN стало best-effort и больше не переводит уже выполненную выдачу в ошибку webhook/retry.
- Ошибка редактирования processing-сообщения при failed fulfillment больше не маскирует исходную ошибку и не мешает сбросу `pending_payment`.
- `_execute_payment_for_hosts()` добавляет host в успешные результаты только после записи или проверки записи в SQLite. Если 3x-ui создал/обновил клиента, но локальная БД не подтвердила запись, host считается failed.
- Slash-команды P2P теперь используют обычный формат `/approve_p2p <request_id>` и `/decline_p2p <request_id>`.
- Промокод после успешного `applied` больше не освобождается из-за нефатальной ошибки удаления processing-сообщения, отправки success-сообщения или admin-уведомления.
- Добавлен `scripts/check_payment_safety.py` — статическая проверка критичных инвариантов fulfillment-логики, `pending_payment`, P2P-команд и промокодов.

### Найденные риски без автоматического исправления

- SSH открыт в интернет и разрешает password/root login. Fail2Ban активен и уже банит brute-force, но риск остаётся по бизнес-требованию владельца.
- `/login` в nginx использует общую зону rate-limit, хотя отдельная зона `login` уже объявлена. Безопасное исправление требует reload nginx.
- `/sub/<token>` является публичной bearer-ссылкой и при активной global/trial подписке может довыдавать missing hosts. Это удобно для self-healing, но GET endpoint имеет side effects.
- Секреты XUI/YooKassa/Telegram хранятся в SQLite в открытом виде. Права файла строгие, но бэкапы и доступ к серверу надо защищать как доступ к секретам.
- Часть старых DB-бэкапов в `/root/myvlessbottg-db-backups` имеет права `0644`. Каталог находится под `/root`, но права лучше привести к `600`.
- Админка `/users` и `/keys` загружает данные целиком и фильтрует на клиенте; при росте базы нужна серверная пагинация.
- CSP админки всё ещё допускает inline handlers для совместимости с текущими шаблонами.

### Проверено после исправлений

```bash
python3 -m compileall -q src scripts
python3 scripts/check_payment_safety.py
python3 scripts/check_callbacks.py
python3 scripts/check_fsm_transitions.py
python3 scripts/check_settings_defaults.py
python3 scripts/check_host_cleanup.py --db users.db
python3 scripts/check_subscription_consistency.py --db users.db
docker exec myvlessbottg-bot-1 python3 scripts/check_xui_connection_equivalence.py
```

`check_xui_connection_equivalence.py` на host Python не запускается без установленного `py3xui`, поэтому проверка выполнена внутри уже запущенного контейнера.

### Бэкап перед исправлениями

Перед правками создан локальный бэкап runtime-файлов:

- `/root/myvlessbottg-audit-backups/20260529T084843Z/users.db`
- `/root/myvlessbottg-audit-backups/20260529T084843Z/.env`
- `/root/myvlessbottg-audit-backups/20260529T084843Z/docker-compose.yml`
- `/root/myvlessbottg-audit-backups/20260529T084843Z/panel.stopurban.ru.conf`
- `/root/myvlessbottg-audit-backups/20260529T084843Z/rate-limits.conf`

## Аудит 2026-05-31

### Проверено

- Рендер админки через Flask test client: `/dashboard`, `/dashboard?period=1`, `/dashboard?period=7`, `/dashboard?period=30`, `/dashboard?period=all`, `/users`, `/keys`, `/settings`, `/updates`, `/users/diagnostics/<user_id>`.
- Браузерный визуальный проход Playwright в desktop `1440x1000` и mobile `390x844`: dashboard, users, keys, settings, updates.
- Проверка переполнений/выездов элементов за viewport вне таблиц со штатным horizontal scroll.
- Проверка console errors в браузере.
- `python3 -m compileall -q src scripts`.
- `scripts/check_callbacks.py`.
- `scripts/check_fsm_transitions.py`.
- `scripts/check_host_cleanup.py`.
- `scripts/check_subscription_consistency.py`.
- `scripts/check_xui_connection_equivalence.py`.
- `scripts/check_settings_defaults.py`.
- `scripts/check_payment_safety.py`.

### Исправлено

- Dashboard больше не считает истекшие платные подписки отдельной формулой по paid-транзакциям. Метрики активных/истекших платных и trial-подписок теперь берутся из того же классификатора, что и `/users`.
- Блок оплаченных покупок на Dashboard показывает все paid-транзакции за всю историю, с полной датой и временем, последние платежи сверху.
- Блок оплаченных покупок оформлен как полноширинная таблица, чтобы сумма, метод, тариф и пользователь читались без случайного обрезания.
- Блок промокодов в Settings переразмечен в отдельную адаптивную карточку: статус функции, форма добавления, карточки промокодов, счётчик применений и действия сохранения/паузы/запуска/удаления.
- Длинные URL хостов в Settings теперь переносятся и не ломают mobile layout.
- CSP разрешает `fonts.googleapis.com` и `fonts.gstatic.com`, поэтому подключённые Google Fonts больше не блокируются политикой самой админки.

### Итог

- На момент проверки `/healthz` возвращал `{"status": "ok"}`.
- Браузерный проход не показал console errors и layout-overflow проблем на проверенных desktop/mobile viewport.
- По текущей БД истекших платных подписок нет: нет пользователей с paid XUI-ключами `plan_id > 0`, у которых отсутствует активный paid-ключ.

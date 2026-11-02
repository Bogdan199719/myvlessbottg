# Продолжение после обновления лимитов

Состояние на 2026-11-02. Работа остановлена по просьбе владельца для сохранения бюджета. Непроверенные изменения в production не внедрялись.

## Завершено и внедрено

- Первый этап: `24f2077` — диагностика, MTG-журналы, рублёвый CryptoBot без Binance, ограничение памяти авторизации.
- Следующий законченный этап: `5fe6cf0bb639532bd7487e3815ec5c7692f2fa34` — изоляция ошибок независимых шагов scheduler, отдельный цикл уведомлений, ограниченные по частоте операционные уведомления, OS-блокировки проекта и БД до восстановления/старта ботов.
- Оба коммита отправлены в `origin/agent/improve-repository-documentation` обычным push.
- Этап стабильности прошёл 9 целевых скриптов в новом образе: CryptoBot confirmation, YooKassa retry, attempt ownership, scheduler integrations/isolation, notification rules, process lock, runtime alerts и health.
- Проверки после выкладки: оба бота отвечают getMe, API/login/существующая подписка доступны, некорректные webhook отклоняются; 18 подключений 3x-ui доступны. Второй процесс блокируется. Heartbeat и три scheduler работают, проходы завершены. Финальная проверка: healthy, без рестартов; SQLite quick_check проходит, пользователи, ключи и транзакции из предрелизного снимка сохранены. Реальных тестовых платежей/продлений не создавали.

Production image: `myvlessbottg-bot:stability-20261102t091029z`.
Резервные копии, предыдущий Compose override, последний SQLite snapshot и `rollback.py`: `/root/deployments/stability-20261102T091029Z/`. Откат меняет приложение, но НЕ восстанавливает старую базу поверх новых платежей.

## Незавершённая работа сохранена отдельно

Изолированный Git-репозиторий: `/root/workspaces/myvlessbottg-remaining`.
Ветка: `wip/remaining-hardening-20261102`; чистое рабочее дерево.

- `6388713` — подготовленная привязка CryptoBot: ID до публикации ссылки, атомарный claim старого ID, провайдер/сумма/дубли, canonical ID и сохранность binding в checkpoints. НЕ внедрено.
- `e68a37c25fef1f94faaebaf2798d89a7d3d50530` — WIP резервирования, безопасных обновлений и защиты recovery от живого owner. НЕ внедрено.
- Резервная копия всего этого Git: `/root/workspaces/remaining-hardening-checkpoint.bundle`.
- Агентские patch и журналы: `/root/workspaces/remaining-agent-artifacts.tar.gz` (права 600).

Дополнения субагентов:

1. `/root/workspaces/crypto-review.patch` уже объединён в WIP. Исправлены нормализация ID, durable binding и повреждённые marker. Последний узкий binding-тест PASS; предыдущие confirmation/retry/ownership проходили, но общий окончательный прогон нужен. Один прогон confirmation превысил 90 секунд при параллельной нагрузке: НЕ считать PASS. Старый YooKassa fixture без метода давал 400; fixture исправлен в WIP, повторить тест.
2. `/root/workspaces/payment-recovery.patch` уже объединён в WIP. RLock owner -> SQLite + прежний CAS; активный пользователь, включая legacy None, не теряет владельца. Некорректные targets не возобновляются; read-only counters и отложенные alerts без identities. 8 targeted тестов и отдельный alert-тест PASS; последняя правка тихого timestamp parser требует общего финального прогона.
3. `/root/workspaces/remaining-backup-review.patch` ещё НЕ объединён. Проверенная копия: `/root/workspaces/remaining-review-backup`; журнал `/root/workspaces/backup-agent-final-tests2.log`. Три финальных теста PASS: пути/updates, backup, full synthetic backup -> restore -> реальный запуск. Patch добавляет ротацию только проверенных архивов с поддержкой пробелов, fsync и fault injection. Его нужно просмотреть и применить к WIP перед общими тестами.

**Важно про Git WIP:** исходный snapshot специально включает прежние незакоммиченные правки владельца и имеет отдельную синтетическую историю. Не push всего WIP-репозитория в production-ветку. Переносить только новые diff-hunks на реальную историю основного репозитория. `.env.example` был скопирован, но игнорируется синтетическим индексом; дополнение backup-agent к нему оформлено как modify-hunk. Не превращать этот файл в новое добавление в релизный patch — он уже отслеживается основным репозиторием.

## С чего продолжить

1. Проверить production health, Git и неизменность чужих правок. Не менять код работающего контейнера.
2. В изоляции просмотреть и объединить backup-agent patch; проверить совместимость с объединёнными recovery/watchdog изменениями.
3. Последовательно, без конкурирующих тяжёлых контейнеров, прогнать все безопасные `check_*.py` на синтетической базе. Особенно CryptoBot binding/confirmation, YooKassa retry, ownership/checkpoints, recovery races, trial/subscriptions/notifications, XUI serialization/canonical preservation, backup/full restore, runtime alerts. Runner: `/root/workspaces/remaining-test-runner.py`. Поднимать тестовую 3x-ui только при наличии её бинарника; не использовать production-панель как fixture.
4. Только при всех критических PASS сформировать небольшие отдельные коммиты на реальной истории и релиз. Текущие локальные ops helpers: `/root/workspaces/remaining_release.py`, `/root/workspaces/check_release_production.py`, `/root/workspaces/remaining_commit_release.py`. Перед использованием проверить аргументы/state: они относятся к последнему stability-релизу, не запускать deploy вслепую.
5. До новой выкладки — проверенная резервная копия, предыдущий immutable image/override, обратный patch; stop-before-start. После — боты, health, все циклы, read-only подписка, webhook rejection, 3x-ui и сохранность данных. При откате не заменять live-БД старой копией.

Внешнее хранилище НЕ настроено: владелец пока не указал destination/доступ и ключ шифрования. Ничего наружу не выгружали. Неоднозначные старые платежи/P2P и новое раннее восстановление не разрешены для автоматической обработки — оставить ручную сверку и согласовать отдельно.

В основном репозитории сохранены прежние изменения: `docs/bot-flow.md`, `bot/support_handlers.py`, `data_manager/database.py` (строка Happ), `templates/settings.html`; также локальные AUDIT_REPORT/FIX_PLAN, Compose override и `check_support_delivery.py`. Не включать их в следующие коммиты без отдельного основания.

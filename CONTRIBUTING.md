# Contributing

Спасибо за улучшения проекта. Перед pull request:

1. Не добавляйте `.env`, `users.db`, backups, токены, пароли и персональные данные.
2. Держите изменение сфокусированным и используйте понятное сообщение коммита, например `fix: harden subscription validation`.
3. Запустите как минимум:

   ```bash
   python3 -m compileall -q src scripts
   python3 scripts/check_callbacks.py
   python3 scripts/check_subscription_business_rules.py
   ```

4. Для изменений в платежах, подписках, FSM, очистке хостов или scheduler добавьте либо обновите соответствующий `scripts/check_*.py`.
5. В PR опишите пользовательский эффект, выполненные проверки и влияние на конфигурацию или миграции.

Подробности по запуску и production-проверкам — в [документации по деплою](docs/deployment.md).

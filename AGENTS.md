# Repository Guidelines

## Project Structure & Module Organization
- Core application code lives in `src/shop_bot/`.
- Telegram bot logic is in `src/shop_bot/bot/` (`handlers.py`, `support_handlers.py`, `keyboards.py`, `middlewares.py`).
- Web admin panel and payment webhooks are in `src/shop_bot/webhook_server/` with templates in `templates/` and static assets in `static/`.
- Data access and subscription jobs are in `src/shop_bot/data_manager/`.
- 3x-ui integration is in `src/shop_bot/modules/xui_api.py`.
- Maintenance checks are in `scripts/`.

## Build, Test, and Development Commands
- `docker-compose up -d --build`: build and run the full stack (primary workflow).
- `docker-compose logs -f`: stream runtime logs for bot/webhook troubleshooting.
- `docker-compose down`: stop running containers.
- `pip install -e ".[dev]"`: install local editable package with dev tools.
- `python -m shop_bot`: run the app locally without Docker.
- `pylint src/`: static lint checks.
- `black src/`: format Python code.
- `python scripts/check_callbacks.py` and `python scripts/check_fsm_transitions.py`: regression guards for callback/FSM flow coverage.

## Coding Style & Naming Conventions
- Use Python with 4-space indentation and PEP 8-friendly formatting.
- Use `snake_case` for functions/variables/files, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants.
- Keep handlers and callbacks explicit; avoid hidden side effects in route or bot handler code.
- Run `black` and `pylint` before opening a PR.

## Testing Guidelines
- There is no formal `tests/` suite yet; required checks are lint plus script-based guards.
- Treat `scripts/check_callbacks.py` and `scripts/check_fsm_transitions.py` as mandatory pre-PR checks.
- If adding complex logic, include focused unit tests in a new `tests/` package using `pytest` naming (`test_*.py`).

## Commit & Pull Request Guidelines
- Match existing history style: short, imperative subjects (e.g., `Fix callback timeout handling`, `Bump version to 2.4.19`).
- Keep commits scoped to one change area.
- PRs should include: purpose, user-visible impact, verification steps/commands, and screenshots for UI/admin template changes.
- Link related issues and mention config or migration implications (`.env`, `users.db`, host settings).

## Security & Configuration Tips
- Never commit secrets or runtime data (`.env`, `users.db`, backups, tokens).
- Validate payment/webhook changes in a safe environment before production rollout.

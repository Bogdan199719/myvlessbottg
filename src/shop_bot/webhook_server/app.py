import os
import logging
import asyncio
import concurrent.futures
import json
import hashlib
import hmac
import base64
import sqlite3
import tempfile
import zipfile
import shutil
import sys
import threading
import csv
import io
import re
import time as _time
from collections import defaultdict
from hmac import compare_digest
from datetime import datetime, timedelta
from shop_bot.utils import time_utils, update_manager
from shop_bot.version import APP_VERSION
from functools import wraps
from math import ceil
from pathlib import Path
from urllib.parse import urlparse
from flask import (
    Flask,
    request,
    render_template,
    redirect,
    url_for,
    flash,
    session,
    current_app,
    g,
    send_file,
    after_this_request,
    Response,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from yookassa import Configuration
from yookassa import Payment

from shop_bot.modules import mtg_api, xui_api
from shop_bot.bot import handlers
from shop_bot.webhook_server.subscription_api import subscription_bp
from shop_bot.data_manager import scheduler
from shop_bot.data_manager.database import (
    get_all_settings,
    update_setting,
    get_all_hosts,
    get_plans_for_host,
    create_host,
    delete_host,
    create_plan,
    delete_plan,
    get_user_count,
    get_total_keys_count,
    get_total_spent_sum,
    get_daily_stats_for_charts,
    get_recent_transactions,
    get_paginated_transactions,
    get_all_users,
    get_user_keys,
    ban_user,
    unban_user,
    delete_user_everywhere,
    get_setting,
    DB_FILE,
    register_user_if_not_exists,
    get_next_key_number,
    get_key_by_id,
    update_key_info,
    set_terms_agreed,
    get_plan_by_id,
    log_transaction,
    get_referral_count,
    add_to_referral_balance,
    create_pending_transaction,
    reserve_pending_transaction,
    finalize_reserved_transaction,
    run_migration,
    set_referral_balance,
    set_referral_balance_all,
    get_all_keys_with_usernames,
    update_key_connection_string,
    get_host,
    update_host,
    toggle_host_status,
    get_keys_for_host,
    add_new_key,
    get_user,
    update_user_stats,
    get_missing_keys,
    get_key_by_email,
    update_key_plan_id,
    create_mtg_host,
    get_mtg_host,
    get_all_mtg_hosts,
    update_mtg_host,
    toggle_mtg_host_status,
    delete_mtg_host,
    get_all_payment_rules,
    set_payment_rule,
    delete_payment_rules_for_context,
    delete_keys_by_ids,
    ALL_PAYMENT_METHODS,
    get_global_plan_ids,
    is_global_xui_key,
    create_promo_code,
    get_all_promo_codes,
    update_promo_code,
    set_promo_code_active,
    delete_promo_code,
    get_paid_revenue_between,
    create_profit_distribution,
    get_profit_distributions,
    get_last_active_profit_distribution,
    get_first_paid_transaction_date,
    has_profit_distribution_overlap,
    update_profit_distribution,
    void_profit_distribution,
    mark_profit_distribution_paid,
)

_bot_controller = None
APP_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = APP_ROOT / ".env"
MAX_BACKUP_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_BACKUP_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_BACKUP_FILES = 3
ALLOWED_BACKUP_FILES = {"users.db", "metadata.json", ".env"}


def _build_subscription_link(domain: str | None, token: str | None) -> str | None:
    domain_value = (domain or "").strip()
    token_value = (token or "").strip()
    if not domain_value or not token_value:
        return None
    if not domain_value.startswith(("http://", "https://")):
        domain_value = f"https://{domain_value}"
    return f"{domain_value.rstrip('/')}/sub/{token_value}"


def _key_plan_id(key: dict) -> int:
    try:
        return int(key.get("plan_id") or 0)
    except (TypeError, ValueError):
        return 0


def _is_xui_key(key: dict) -> bool:
    return key.get("service_type", "xui") != "mtg"


def _is_trial_key(key: dict) -> bool:
    return _is_xui_key(key) and _key_plan_id(key) <= 0


def _configured_trial_duration_days() -> float:
    try:
        return max(1.0, float(get_setting("trial_duration_days") or 1))
    except (TypeError, ValueError):
        return 1.0


def _user_has_paid_subscription(user: dict) -> bool:
    try:
        total_spent = float(user.get("total_spent") or 0)
    except (TypeError, ValueError):
        total_spent = 0
    return total_spent > 0 or _user_int_field(user, "paid_transaction_count") > 0


def _user_int_field(user: dict, key: str) -> int:
    try:
        return int(user.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _key_is_trial_for_user(
    key: dict,
    user: dict | None = None,
    trial_duration_days: float | None = None,
) -> bool:
    if not _is_trial_key(key):
        return False

    created = time_utils.parse_iso_to_msk(key.get("created_date"))
    expiry = time_utils.parse_iso_to_msk(key.get("expiry_date"))
    if not created or not expiry:
        return True

    configured_days = (
        trial_duration_days
        if trial_duration_days is not None
        else _configured_trial_duration_days()
    )
    grace_seconds = 6 * 3600
    return (expiry - created).total_seconds() <= (
        configured_days * 86400 + grace_seconds
    )


def _key_is_trial_for_owner(
    key: dict, trial_duration_days: float | None = None
) -> bool:
    return _key_is_trial_for_user(key, key, trial_duration_days)


def _build_user_metrics(users: list[dict]) -> dict:
    metrics = {
        "total_users": len(users),
        "paid_users": 0,
        "paid_expired_users": 0,
        "free_users": 0,
        "trial_users": 0,
        "trial_expired_users": 0,
        "payment_pending_users": 0,
        "free_expired_users": 0,
        "support_only_users": 0,
        "no_subscription_users": 0,
        "banned_users": 0,
    }
    for user in users:
        summary = user.get("subscription_summary") or {}
        status = summary.get("status")
        if status == "paid":
            metrics["paid_users"] += 1
        elif status == "paid_expired":
            metrics["paid_expired_users"] += 1
        elif status == "free":
            metrics["free_users"] += 1
        elif status == "trial":
            metrics["trial_users"] += 1
        elif status == "trial_expired":
            metrics["trial_expired_users"] += 1
        elif status == "payment_pending":
            metrics["payment_pending_users"] += 1
        elif status == "free_expired":
            metrics["free_expired_users"] += 1
        elif status == "support_only":
            metrics["support_only_users"] += 1
        elif status == "banned":
            metrics["banned_users"] += 1
        else:
            metrics["no_subscription_users"] += 1
    return metrics


def _summarize_user_subscription(
    user: dict, keys: list[dict], now: datetime | None = None
) -> dict:
    now = now or time_utils.get_msk_now()
    trial_duration_days = _configured_trial_duration_days()
    paid_active = 0
    trial_active = 0
    active_total = 0
    paid_keys_total = 0
    latest_paid_expiry = None

    for key in keys:
        if not _is_xui_key(key):
            continue

        expiry = time_utils.parse_iso_to_msk(key.get("expiry_date"))
        is_trial_key = _key_is_trial_for_user(key, user, trial_duration_days)
        if not is_trial_key:
            paid_keys_total += 1
            if expiry and (latest_paid_expiry is None or expiry > latest_paid_expiry):
                latest_paid_expiry = expiry

        if not expiry or expiry <= now:
            continue

        active_total += 1

        if is_trial_key:
            trial_active += 1
        else:
            paid_active += 1

    has_paid_purchase = _user_has_paid_subscription(user)
    has_pending_payment = _user_int_field(user, "pending_transaction_count") > 0
    has_free_access_history = _user_int_field(user, "free_transaction_count") > 0
    has_support_history = (
        _user_int_field(user, "support_ticket_count") > 0
        or _user_int_field(user, "support_message_count") > 0
    )

    if user.get("is_banned"):
        status = "banned"
        label = "Забанен"
        css_class = "status-banned"
    elif paid_active > 0 and has_paid_purchase:
        status = "paid"
        label = "Платная подписка"
        css_class = "status-active"
    elif paid_active > 0:
        status = "free"
        label = "Бесплатный доступ"
        css_class = "status-info"
    elif trial_active > 0:
        status = "trial"
        label = "Пробная подписка"
        css_class = "status-trial"
    elif has_paid_purchase:
        status = "paid_expired"
        label = "Платная истекла"
        css_class = "status-warning"
    elif user.get("trial_used"):
        status = "trial_expired"
        label = "Пробник истек"
        css_class = "status-warning"
    elif has_pending_payment:
        status = "payment_pending"
        label = "Счёт не оплачен"
        css_class = "status-pending"
    elif has_free_access_history or paid_keys_total > 0:
        status = "free_expired"
        label = "Бесплатный доступ истёк"
        css_class = "status-info"
    elif has_support_history:
        status = "support_only"
        label = "Обращался без подписки"
        css_class = "status-info"
    else:
        status = "no_subscription"
        label = "Без подписки"
        css_class = "status-stopped"

    return {
        "status": status,
        "label": label,
        "css_class": css_class,
        "active_total": active_total,
        "paid_active": paid_active,
        "trial_active": trial_active,
        "paid_keys_total": paid_keys_total,
        "latest_paid_expiry": latest_paid_expiry,
        "pending_transaction_count": _user_int_field(
            user, "pending_transaction_count"
        ),
        "free_transaction_count": _user_int_field(user, "free_transaction_count"),
        "support_ticket_count": _user_int_field(user, "support_ticket_count"),
        "support_message_count": _user_int_field(user, "support_message_count"),
        "is_active": active_total > 0,
    }


def _format_remaining_time(seconds_left: float) -> str:
    total_minutes = max(1, int(ceil(seconds_left / 60)))
    hours, minutes = divmod(total_minutes, 60)
    if hours <= 0:
        return f"{minutes} мин."

    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes and not days:
        parts.append(f"{minutes} мин.")
    return " ".join(parts)


def _build_key_expiry_status(
    key: dict, *, is_trial: bool = False, now: datetime | None = None
) -> dict:
    now = now or time_utils.get_msk_now()
    expiry = time_utils.parse_iso_to_msk(key.get("expiry_date"))

    if not expiry:
        return {
            "filter": "active",
            "css_class": "status-active",
            "label": "Бессрочно",
            "expires_at": "∞",
        }

    expires_at = expiry.strftime("%d.%m.%Y %H:%M")
    seconds_left = (expiry - now).total_seconds()

    if seconds_left <= 0:
        return {
            "filter": "expired",
            "css_class": "status-banned",
            "label": "Триал истек" if is_trial else "Просрочено",
            "expires_at": expires_at,
        }

    remaining = _format_remaining_time(seconds_left)
    if seconds_left < 24 * 3600:
        return {
            "filter": "expiring",
            "css_class": "status-warning",
            "label": (
                f"Триал: {remaining}" if is_trial else f"Осталось {remaining}"
            ),
            "expires_at": expires_at,
        }

    if seconds_left < 3 * 24 * 3600:
        return {
            "filter": "expiring",
            "css_class": "status-warning",
            "label": (
                f"Триал: {remaining}" if is_trial else f"Осталось {remaining}"
            ),
            "expires_at": expires_at,
        }

    return {
        "filter": "active",
        "css_class": "status-active",
        "label": f"Осталось {remaining}",
        "expires_at": expires_at,
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_schema_version(db_path: Path) -> int:
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version")
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.error(f"Failed to read schema version from {db_path}: {e}")
        return 0


def _validate_restore_db(db_path: Path) -> None:
    """Validate uploaded SQLite DB before it can replace the live database."""
    required_tables = {"users", "vpn_keys", "transactions", "bot_settings"}
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            integrity_row = cursor.fetchone()
            integrity_result = str(integrity_row[0] if integrity_row else "").lower()
            if integrity_result != "ok":
                raise ValueError("Проверка целостности БД не пройдена.")

            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            tables = {str(row[0]) for row in cursor.fetchall()}
    except sqlite3.DatabaseError as e:
        raise ValueError("Файл users.db в архиве не является корректной SQLite БД.") from e

    missing_tables = sorted(required_tables - tables)
    if missing_tables:
        raise ValueError(
            "В backup-базе отсутствуют обязательные таблицы: "
            + ", ".join(missing_tables)
        )


def _create_backup_zip(include_env: bool = False) -> tuple[Path, Path]:
    """
    Returns (zip_path, temp_dir) so caller can clean up temp_dir afterwards.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="backup_"))
    try:
        db_copy = temp_dir / "users.db"
        with sqlite3.connect(DB_FILE) as src_conn, sqlite3.connect(db_copy) as dst_conn:
            src_conn.backup(dst_conn)

        checksum = _sha256_file(db_copy)
        metadata = {
            "timestamp_utc": time_utils.get_msk_now().isoformat(),
            "schema_version": _get_schema_version(db_copy),
            "checksum": checksum,
            "include_env": include_env,
        }
        metadata_path = temp_dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if include_env and ENV_FILE.exists():
            shutil.copy(ENV_FILE, temp_dir / ".env")

        zip_path = (
            temp_dir
            / f"backup-{time_utils.get_msk_now().strftime('%Y%m%d-%H%M%S')}.zip"
        )
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
            for file_path in temp_dir.iterdir():
                if file_path == zip_path:
                    continue
                zipf.write(file_path, arcname=file_path.name)

        return zip_path, temp_dir
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _safe_extract_zip(zip_ref: zipfile.ZipFile, extract_dir: Path) -> None:
    extract_root = extract_dir.resolve()
    members = zip_ref.infolist()
    if len(members) > MAX_BACKUP_FILES:
        raise ValueError("В архиве слишком много файлов.")

    total_unpacked = 0
    for member in members:
        member_name = member.filename
        if member.is_dir():
            raise ValueError("Архив не должен содержать директории.")
        if member_name not in ALLOWED_BACKUP_FILES:
            raise ValueError("Архив содержит недопустимый файл.")
        total_unpacked += int(member.file_size or 0)
        if total_unpacked > MAX_BACKUP_UNPACKED_BYTES:
            raise ValueError("Архив слишком большой после распаковки.")
        if Path(member_name).is_absolute():
            raise ValueError("Недопустимый путь в архиве.")
        member_path = (extract_dir / member_name).resolve()
        if os.path.commonpath([str(extract_root), str(member_path)]) != str(
            extract_root
        ):
            raise ValueError("Недопустимый путь в архиве.")
    zip_ref.extractall(extract_dir)


def _restore_from_backup(zip_file, apply_env: bool = False) -> dict:
    temp_dir = Path(tempfile.mkdtemp(prefix="restore_"))
    restart_results: dict[str, dict] = {}
    previous_status = {
        "shop_bot_running": False,
        "support_bot_running": False,
        "is_running": False,
    }
    try:
        upload_path = temp_dir / "upload.zip"
        zip_file.save(upload_path)

        extract_dir = temp_dir / "extracted"
        with zipfile.ZipFile(upload_path, "r") as zip_ref:
            _safe_extract_zip(zip_ref, extract_dir)

        db_src = extract_dir / "users.db"
        metadata_path = extract_dir / "metadata.json"

        if not db_src.exists():
            raise ValueError("В архиве нет файла users.db")

        _validate_restore_db(db_src)

        if metadata_path.exists():
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
            expected_checksum = meta.get("checksum")
            if expected_checksum:
                actual_checksum = _sha256_file(db_src)
                if actual_checksum != expected_checksum:
                    raise ValueError(
                        "Контрольная сумма БД не совпадает, архив повреждён."
                    )

        # Остановить ботов перед заменой БД
        try:
            if _bot_controller:
                previous_status = dict(_bot_controller.get_status())
                if previous_status.get("is_running"):
                    _bot_controller.stop()
        except Exception as e:
            logger.error(f"Failed to stop bots before restore: {e}", exc_info=True)

        # Резервная копия текущей базы
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        if DB_FILE.exists():
            backup_path = DB_FILE.with_suffix(
                f".bak.{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
            )
            shutil.copyfile(DB_FILE, backup_path)

        # Замена базы
        shutil.copyfile(db_src, DB_FILE)
        run_migration()

        if apply_env:
            env_src = extract_dir / ".env"
            if env_src.exists():
                shutil.copyfile(env_src, ENV_FILE)

        if _bot_controller:
            if previous_status.get("shop_bot_running"):
                restart_results["shop"] = _bot_controller.start_shop_bot()
            if previous_status.get("support_bot_running"):
                restart_results["support"] = _bot_controller.start_support_bot()

        return {
            "restart_results": restart_results,
            "restart_errors": [
                result.get("message", "unknown error")
                for result in restart_results.values()
                if result.get("status") != "success"
            ],
        }

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _ensure_processed_webhooks_table():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_webhooks (
                    provider TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (provider, external_id)
                )
                """)
            conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to ensure processed_webhooks table: {e}")


def _is_webhook_processed(provider: str, external_id: str) -> bool:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM processed_webhooks WHERE provider = ? AND external_id = ?",
                (provider, external_id),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        logger.error(
            f"Failed to check webhook processed for {provider}:{external_id}: {e}"
        )
        return False


def _set_webhook_processed(provider: str, external_id: str) -> None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO processed_webhooks (provider, external_id) VALUES (?, ?)",
                (provider, external_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logger.error(
            f"Failed to set webhook processed for {provider}:{external_id}: {e}"
        )


def _get_transaction_status(payment_id: str) -> str | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM transactions WHERE payment_id = ?",
                (payment_id,),
            )
            row = cursor.fetchone()
            return str(row[0]) if row else None
    except sqlite3.Error as e:
        logger.error(f"Failed to get transaction status for {payment_id}: {e}")
        return None


def _sanitize_csv_cell(value) -> str:
    text = str(value or "")
    if text.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
        return f"'{text}"
    return text


def _reserve_pending_transaction_for_cryptobot(
    payment_id: str,
    *,
    amount_currency=None,
    currency_name: str | None = None,
) -> dict | None:
    return reserve_pending_transaction(
        payment_id,
        payment_method="CryptoBot",
        amount_currency=amount_currency,
        currency_name=currency_name,
    )


def _extract_cryptobot_secret_from_request() -> str | None:
    header_secret = (request.headers.get("X-CryptoBot-Secret") or "").strip()
    if header_secret:
        return header_secret

    authorization = (request.headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    return None


def _is_valid_cryptobot_signature() -> bool:
    signature = (request.headers.get("crypto-pay-api-signature") or "").strip()
    if not signature:
        return False

    cryptobot_token = get_setting("cryptobot_token")
    if not cryptobot_token:
        logger.error(
            "CryptoBot Webhook: cryptobot_token is not configured, cannot verify signature."
        )
        return False

    body = request.get_data(cache=True)
    signing_secret = hashlib.sha256(str(cryptobot_token).encode("utf-8")).digest()
    calculated_signature = hmac.new(signing_secret, body, hashlib.sha256).hexdigest()
    return compare_digest(calculated_signature, signature)


ALL_SETTINGS_KEYS = [
    "panel_login",
    "panel_password",
    "show_about_menu_item",
    "about_text",
    "terms_url",
    "privacy_url",
    "support_user",
    "support_text",
    "channel_url",
    "telegram_bot_token",
    "telegram_bot_username",
    "admin_telegram_id",
    "yookassa_shop_id",
    "yookassa_secret_key",
    "sbp_enabled",
    "receipt_email",
    "cryptobot_token",
    "cryptobot_webhook_secret",
    "domain",
    "referral_percentage",
    "referral_discount",
    "force_subscription",
    "trial_enabled",
    "trial_duration_days",
    "enable_referrals",
    "minimum_withdrawal",
    "support_group_id",
    "support_bot_token",
    "p2p_enabled",
    "p2p_card_number",
    "stars_enabled",
    "stars_rub_per_star",
    "enable_admin_payment_notifications",
    "enable_admin_trial_notifications",
    "subscription_name",
    "subscription_live_sync",
    "subscription_live_stats",
    "subscription_allow_fallback_host_fetch",
    "subscription_auto_provision",
    "panel_sync_enabled",
    "xtls_sync_enabled",
    "enable_promo_codes",
    "profit_vlad_tax_percent",
    "profit_server_cost_rub",
]

SECRET_SETTINGS_KEYS = {
    "telegram_bot_token",
    "support_bot_token",
    "yookassa_secret_key",
    "cryptobot_token",
    "cryptobot_webhook_secret",
}


def _validate_panel_url(raw_url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse((raw_url or "").strip())
    except ValueError:
        return False, "некорректный URL"

    if parsed.scheme not in {"http", "https"}:
        return False, "URL должен начинаться с http:// или https://"
    if not parsed.hostname:
        return False, "в URL должен быть указан host"
    if parsed.username or parsed.password:
        return False, "логин и пароль нельзя передавать внутри URL"
    if parsed.fragment:
        return False, "URL не должен содержать fragment"
    return True, ""


def _is_usable_flask_secret(value: str | None) -> bool:
    normalized = (value or "").strip()
    if len(normalized) < 32:
        return False
    return normalized.lower() not in {
        "change_me_to_a_long_random_secret",
        "generate_a_long_random_secret_here",
        "changeme",
        "change_me",
    }


def create_webhook_app(bot_controller_instance):
    global _bot_controller
    _bot_controller = bot_controller_instance

    _ensure_processed_webhooks_table()

    # Ensure template and static folder relative to this file's location
    base_dir = os.path.dirname(os.path.abspath(__file__))

    flask_app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static"),
    )
    flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_proto=1, x_host=1)
    flask_app.config["MAX_CONTENT_LENGTH"] = MAX_BACKUP_UPLOAD_BYTES

    flask_app.register_blueprint(subscription_bp)

    env_secret_key = os.getenv("FLASK_SECRET_KEY")
    db_secret_key = get_setting("flask_secret_key")
    if _is_usable_flask_secret(env_secret_key):
        secret_key = env_secret_key.strip()
    elif _is_usable_flask_secret(db_secret_key):
        secret_key = db_secret_key.strip()
        if env_secret_key:
            logger.warning("Ignoring weak FLASK_SECRET_KEY from environment.")
    else:
        secret_key = os.urandom(32).hex()
        update_setting("flask_secret_key", secret_key)
        logger.warning("Generated a new Flask secret key because no strong key was configured.")
    flask_app.config["SECRET_KEY"] = secret_key

    # Security Hardening
    flask_app.config["SESSION_COOKIE_SECURE"] = True
    flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
    flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    flask_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)

    # Login brute-force protection
    _login_attempts = defaultdict(list)
    _LOGIN_MAX_ATTEMPTS = 5
    _LOGIN_WINDOW_SECONDS = 300

    @flask_app.route("/favicon.ico")
    def favicon():
        return ("", 204)

    @flask_app.route("/healthz", methods=["GET"])
    def healthz():
        db_ok = False
        try:
            with sqlite3.connect(DB_FILE, timeout=2) as conn:
                conn.execute("SELECT 1")
            db_ok = True
        except sqlite3.Error:
            logger.exception("Health check failed: database is unavailable")

        loop = current_app.config.get("EVENT_LOOP")
        loop_ok = bool(loop and loop.is_running())
        status_code = 200 if db_ok and loop_ok else 503
        payload = {"status": "ok" if status_code == 200 else "degraded"}
        return Response(
            json.dumps(payload, ensure_ascii=False),
            status=status_code,
            mimetype="application/json",
        )

    # CSRF Protection
    @flask_app.before_request
    def csrf_protect():
        if request.method == "POST":
            # Skip CSRF for webhooks
            if request.path in ["/yookassa-webhook", "/cryptobot-webhook"]:
                return
            if request.path.startswith("/cryptobot-webhook/"):
                return

            target_token = request.form.get("csrf_token") or request.headers.get(
                "X-CSRFToken"
            )
            token = session.get("_csrf_token")
            if not token or token != target_token:
                return "CSRF Token missing or invalid!", 403

    def generate_csrf_token():
        if "_csrf_token" not in session:
            session["_csrf_token"] = os.urandom(24).hex()
        return session["_csrf_token"]

    def csp_nonce():
        if not hasattr(g, "_csp_nonce"):
            g._csp_nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        return g._csp_nonce

    flask_app.jinja_env.globals["csrf_token"] = generate_csrf_token
    flask_app.jinja_env.globals["csp_nonce"] = csp_nonce

    @flask_app.after_request
    def add_secure_flag_to_session_cookie(response):
        nonce = csp_nonce()
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
            "script-src-attr 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src 'self' data:; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'",
        )

        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        is_https = request.is_secure or forwarded_proto.lower() == "https"
        if not is_https:
            return response

        session_cookie_name = flask_app.config.get("SESSION_COOKIE_NAME", "session")
        set_cookie_headers = response.headers.getlist("Set-Cookie")
        if not set_cookie_headers:
            return response

        response.headers.pop("Set-Cookie", None)
        cookie_prefix = f"{session_cookie_name}="
        for header_value in set_cookie_headers:
            if header_value.startswith(cookie_prefix) and "Secure" not in header_value:
                header_value = f"{header_value}; Secure"
            response.headers.add("Set-Cookie", header_value)
        return response

    task_status_lock = threading.Lock()
    task_statuses = {
        "sync_configs": {"status": "idle", "message": "Не запускалась"},
        "fix_parameters": {"status": "idle", "message": "Не запускалась"},
        "maintenance": {"status": "idle", "message": "Не запускалась"},
    }

    def _task_status_snapshot() -> dict:
        with task_status_lock:
            return json.loads(json.dumps(task_statuses, ensure_ascii=False))

    def _set_task_status(
        task_name: str, status: str, message: str, details: dict | None = None
    ) -> None:
        payload = {
            "status": status,
            "message": message,
            "updated_at": time_utils.get_msk_now().isoformat(),
        }
        if details:
            payload["details"] = details
        with task_status_lock:
            task_statuses[task_name] = payload

    @flask_app.context_processor
    def inject_current_year():
        return {"current_year": time_utils.get_msk_now().year}

    def login_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not session.get("logged_in", False):
                next_path = request.full_path if request.query_string else request.path
                return redirect(url_for("login_page", next=next_path))
            return f(*args, **kwargs)

        return decorated_function

    def _verify_and_upgrade_panel_password(
        plain_password: str, stored_password: str | None
    ) -> bool:
        if not stored_password:
            return False

        # werkzeug hashes usually start with something like: pbkdf2:sha256:...
        is_hashed = stored_password.startswith("pbkdf2:") or stored_password.startswith(
            "scrypt:"
        )
        if is_hashed:
            return check_password_hash(stored_password, plain_password)

        # Legacy plaintext password support (auto-upgrade on successful login)
        if plain_password == stored_password:
            try:
                update_setting("panel_password", generate_password_hash(plain_password))
            except Exception:
                logger.exception(
                    "Failed to upgrade legacy panel_password to hashed format"
                )
            return True

        return False

    @flask_app.route("/login", methods=["GET", "POST"])
    def login_page():
        settings = get_all_settings()
        if request.method == "POST":
            # Brute-force protection
            client_ip = request.remote_addr or "unknown"
            now = _time.time()
            _login_attempts[client_ip] = [
                t for t in _login_attempts[client_ip] if now - t < _LOGIN_WINDOW_SECONDS
            ]
            if len(_login_attempts[client_ip]) >= _LOGIN_MAX_ATTEMPTS:
                flash(
                    "Слишком много попыток входа. Попробуйте через 5 минут.", "danger"
                )
                return render_template("login.html"), 429

            username_ok = request.form.get("username") == settings.get("panel_login")
            password_ok = _verify_and_upgrade_panel_password(
                request.form.get("password", ""),
                settings.get("panel_password"),
            )
            if username_ok and password_ok:
                _login_attempts.pop(client_ip, None)
                session["logged_in"] = True
                session.permanent = True
                session.pop("_csrf_token", None)  # Rotate CSRF token on login
                next_url = (request.args.get("next") or "").strip()
                if next_url.startswith("/") and not next_url.startswith("//"):
                    return redirect(next_url)
                return redirect(url_for("dashboard_page"))
            else:
                _login_attempts[client_ip].append(now)
                flash("Неверный логин или пароль", "danger")
        return render_template("login.html")

    @flask_app.route("/logout", methods=["POST"])
    @login_required
    def logout_page():
        session.pop("logged_in", None)
        flash("Вы успешно вышли.", "success")
        return redirect(url_for("login_page"))

    def get_common_template_data():
        bot_status = _bot_controller.get_status()
        settings = get_all_settings()
        required_for_shop_start = [
            "telegram_bot_token",
            "admin_telegram_id",
        ]
        required_for_support_start = [
            "support_bot_token",
            "support_group_id",
        ]
        all_settings_ok = all(settings.get(key) for key in required_for_shop_start)
        support_settings_ok = all(
            settings.get(key) for key in required_for_support_start
        )
        return {
            "bot_status": bot_status,
            "all_settings_ok": all_settings_ok,
            "support_settings_ok": support_settings_ok,
        }

    def _parse_user_id_from_key_email(email: str | None) -> int | None:
        if not email:
            return None
        m = re.search(r"user(\d+)-", str(email), re.IGNORECASE)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    def _build_problem_users(limit: int = 10) -> list[dict]:
        users = get_all_users()
        user_by_id = {
            int(u["telegram_id"]): u for u in users if u.get("telegram_id") is not None
        }

        reasons_by_user: dict[int, set[str]] = {}
        now = time_utils.get_msk_now()

        # Problem: missing keys found by sync mechanisms.
        for missing in get_missing_keys():
            email = missing.get("key_email")
            host_name = missing.get("host_name")
            first_seen_raw = missing.get("first_seen")
            uid = _parse_user_id_from_key_email(email)
            if uid is None:
                continue

            # Ignore legacy/dirty entries without host mapping.
            if not host_name:
                continue

            # Ignore stale records older than 48h to reduce false positives.
            first_seen_dt = time_utils.parse_iso_to_msk(first_seen_raw)
            if first_seen_dt and first_seen_dt < now - timedelta(hours=48):
                continue

            # Ignore records that no longer exist in DB or already expired.
            key = get_key_by_email(email)
            if not key:
                continue
            expiry = time_utils.parse_iso_to_msk(key.get("expiry_date"))
            if expiry and expiry <= now:
                continue

            reasons_by_user.setdefault(uid, set()).add("Ключ отсутствует на панели")

        result = []
        for uid, reasons in reasons_by_user.items():
            user = user_by_id.get(uid)
            result.append(
                {
                    "user_id": uid,
                    "username": (user or {}).get("username") or "N/A",
                    "reasons": sorted(reasons),
                    "issues_count": len(reasons),
                }
            )

        result.sort(key=lambda x: (-x["issues_count"], x["user_id"]))
        return result[:limit]

    def _csv_response(
        rows: list[dict], filename: str, fieldnames: list[str]
    ) -> Response:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _sanitize_csv_cell(row.get(k, "")) for k in fieldnames})

        data = output.getvalue()
        output.close()
        return Response(
            data,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    def _date_range(days: int | None, start_day: str | None = None) -> list[str]:
        today = time_utils.get_msk_now().date()
        if days is None:
            try:
                start = datetime.fromisoformat(start_day).date() if start_day else today
            except (TypeError, ValueError):
                start = today
            if start > today:
                start = today
            span = (today - start).days + 1
            return [
                (start + timedelta(days=offset)).isoformat()
                for offset in range(span)
            ]
        return [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(days - 1, -1, -1)
        ]

    def _safe_metadata(raw: str | None) -> dict:
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    def _format_payment_method(method: str | None) -> str:
        normalized = str(method or "").strip().lower()
        labels = {
            "yookassa": "ЮKassa / СБП",
            "yoo_kassa": "ЮKassa / СБП",
            "telegram_stars": "Telegram Stars",
            "stars": "Telegram Stars",
            "cryptobot": "CryptoBot",
            "crypto_bot": "CryptoBot",
        }
        return labels.get(normalized, str(method or "Не указан"))

    def _format_transaction_status(status: str | None) -> str:
        normalized = str(status or "unknown").strip().lower()
        labels = {
            "paid": "Оплачен",
            "pending": "Ожидает оплаты",
            "processing": "Обрабатывается",
            "canceled": "Отменён",
            "failed": "Ошибка",
            "expired": "Истёк без оплаты",
        }
        return labels.get(normalized, str(status or "Неизвестно"))

    def _format_chart_day(day: str) -> str:
        try:
            return datetime.fromisoformat(day).strftime("%d.%m")
        except ValueError:
            return day

    def _format_transaction_dt(value: str | None) -> str:
        if not value:
            return "—"
        parsed = time_utils.parse_iso_to_msk(str(value))
        if parsed:
            return parsed.strftime("%d.%m.%Y %H:%M:%S")
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
                "%d.%m.%Y %H:%M:%S"
            )
        except (TypeError, ValueError):
            return str(value)

    def _dashboard_period_options(selected_period: str) -> list[dict]:
        return [
            {
                "period": period,
                "label": label,
                "is_active": selected_period == period,
            }
            for period, label in (
                ("1", "1 день"),
                ("7", "7 дней"),
                ("30", "30 дней"),
                ("all", "Всё время"),
            )
        ]

    def _format_period_label(period: str) -> str:
        labels = {
            "1": "за 1 день",
            "7": "за 7 дней",
            "30": "за 30 дней",
            "all": "за всё время",
        }
        return labels.get(period, "за 30 дней")

    def _parse_money_value(value, default: float = 0.0) -> float:
        try:
            normalized = str(value if value is not None else "").replace(",", ".")
            return max(0.0, float(normalized))
        except (TypeError, ValueError):
            return default

    def _parse_percent_value(value, default: float = 0.0) -> float:
        try:
            normalized = str(value if value is not None else "").replace(",", ".")
            return min(100.0, max(0.0, float(normalized)))
        except (TypeError, ValueError):
            return default

    def _profit_settings(settings: dict | None = None) -> dict:
        settings = settings or get_all_settings()
        bogdan_share = _parse_percent_value(
            settings.get("profit_bogdan_share_percent"), 40.0
        )
        vlad_share = _parse_percent_value(
            settings.get("profit_vlad_share_percent"), 60.0
        )
        return {
            "bogdan_share_percent": bogdan_share,
            "vlad_share_percent": vlad_share,
            "vlad_tax_percent": _parse_percent_value(
                settings.get("profit_vlad_tax_percent"), 9.0
            ),
            "server_cost_rub": _parse_money_value(
                settings.get("profit_server_cost_rub"), 0.0
            ),
        }

    def _calculate_partner_profit(revenue_rub: float, settings: dict) -> dict:
        revenue = max(0.0, float(revenue_rub or 0.0))
        bogdan_share = float(settings["bogdan_share_percent"])
        vlad_share = float(settings["vlad_share_percent"])
        tax_percent = float(settings["vlad_tax_percent"])
        server_cost = float(settings["server_cost_rub"])

        total_tax = revenue * tax_percent / 100
        revenue_after_tax = revenue - total_tax
        profit_pool = revenue_after_tax - server_cost
        bogdan_profit = profit_pool * bogdan_share / 100
        vlad_net = profit_pool * vlad_share / 100

        return {
            "revenue_rub": round(revenue, 2),
            "revenue_after_tax_rub": round(revenue_after_tax, 2),
            "profit_pool_rub": round(profit_pool, 2),
            "bogdan_share_percent": bogdan_share,
            "vlad_share_percent": vlad_share,
            "vlad_tax_percent": tax_percent,
            "server_cost_rub": round(server_cost, 2),
            "server_share_rub": round(server_cost, 2),
            "bogdan_gross_rub": round(bogdan_profit, 2),
            "bogdan_profit_rub": round(bogdan_profit, 2),
            "vlad_gross_rub": round(vlad_net, 2),
            "vlad_tax_rub": round(total_tax, 2),
            "vlad_net_rub": round(vlad_net, 2),
            "total_net_rub": round(bogdan_profit + vlad_net, 2),
        }

    def _month_bounds(now: datetime, offset: int = 0) -> tuple[datetime, datetime]:
        month_index = now.month - 1 + offset
        year = now.year + month_index // 12
        month = month_index % 12 + 1
        start = now.replace(
            year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        next_index = month_index + 1
        next_year = now.year + next_index // 12
        next_month = next_index % 12 + 1
        end = now.replace(
            year=next_year,
            month=next_month,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return start, end

    def _format_profit_dt(value: str | None) -> str:
        if not value:
            return "старт проекта"
        try:
            normalized = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).strftime("%d.%m.%Y %H:%M")
        except (TypeError, ValueError):
            return str(value)

    def _profit_iso(dt: datetime) -> str:
        return dt.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")

    def _normalize_profit_dt(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return _profit_iso(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            return str(value)

    def _parse_profit_dt_input(value: str | None) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return _profit_iso(datetime.fromisoformat(raw.replace("T", " ")))
        except ValueError:
            return None

    def _profit_input_value(value: str | None) -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
                "%Y-%m-%dT%H:%M"
            )
        except ValueError:
            return ""

    def _profit_display_amount(value: float | int | None) -> str:
        amount = round(float(value or 0))
        return f"{amount:,}".replace(",", " ")

    def _selected_period_bounds(
        selected_period: str, selected_period_days: int | None, now: datetime
    ) -> tuple[str | None, str]:
        if selected_period == "all" or selected_period_days is None:
            return get_first_paid_transaction_date(), _profit_iso(now)

        start = (now - timedelta(days=selected_period_days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return _profit_iso(start), _profit_iso(now)

    def _build_profit_context(
        selected_period_revenue: float,
        selected_period: str,
        selected_period_days: int | None,
    ) -> dict:
        settings = _profit_settings()
        now = time_utils.get_msk_now()
        current_month_start, current_month_end = _month_bounds(now, 0)
        previous_month_start, previous_month_end = _month_bounds(now, -1)
        last_distribution = get_last_active_profit_distribution()
        project_start = _normalize_profit_dt(get_first_paid_transaction_date())
        last_distribution_end = _normalize_profit_dt(
            last_distribution.get("period_end") if last_distribution else None
        )
        unsettled_start = last_distribution_end if last_distribution else project_start
        selected_start, selected_end = _selected_period_bounds(
            selected_period, selected_period_days, now
        )
        now_iso = _profit_iso(now)

        slices = {
            "selected": _calculate_partner_profit(selected_period_revenue, settings),
            "all_time": _calculate_partner_profit(
                get_paid_revenue_between(None, now_iso), settings
            ),
            "current_month": _calculate_partner_profit(
                get_paid_revenue_between(
                    _profit_iso(current_month_start), _profit_iso(current_month_end)
                ),
                settings,
            ),
            "previous_month": _calculate_partner_profit(
                get_paid_revenue_between(
                    _profit_iso(previous_month_start), _profit_iso(previous_month_end)
                ),
                settings,
            ),
            "unsettled": _calculate_partner_profit(
                get_paid_revenue_between(unsettled_start, now_iso), settings
            ),
        }
        distribution_periods = [
            {
                "value": "unsettled",
                "label": "К распределению",
                "start": unsettled_start,
                "end": now_iso,
                "note": f"{_format_profit_dt(unsettled_start)} → {_format_profit_dt(now_iso)}",
                "calculation": slices["unsettled"],
            },
            {
                "value": "current_month",
                "label": "Текущий месяц",
                "start": _profit_iso(current_month_start),
                "end": now_iso,
                "note": f"{_format_profit_dt(_profit_iso(current_month_start))} → {_format_profit_dt(now_iso)}",
                "calculation": slices["current_month"],
            },
            {
                "value": "previous_month",
                "label": "Прошлый месяц",
                "start": _profit_iso(previous_month_start),
                "end": _profit_iso(previous_month_end),
                "note": f"{_format_profit_dt(_profit_iso(previous_month_start))} → {_format_profit_dt(_profit_iso(previous_month_end))}",
                "calculation": slices["previous_month"],
            },
            {
                "value": "selected",
                "label": "Выбранный сверху период",
                "start": selected_start,
                "end": selected_end,
                "note": f"{_format_profit_dt(selected_start)} → {_format_profit_dt(selected_end)}",
                "calculation": slices["selected"],
            },
            {
                "value": "all_time",
                "label": "Всё время",
                "start": project_start,
                "end": now_iso,
                "note": f"{_format_profit_dt(project_start)} → {_format_profit_dt(now_iso)}",
                "calculation": slices["all_time"],
            },
            {
                "value": "custom",
                "label": "Вручную с/по",
                "start": None,
                "end": None,
                "note": "укажи даты ниже",
                "calculation": None,
            },
        ]
        for item in distribution_periods:
            calculation = item.get("calculation") or {}
            item["revenue_display"] = _profit_display_amount(calculation.get("revenue_rub"))
            item["tax_display"] = _profit_display_amount(calculation.get("vlad_tax_rub"))
            item["server_cost_display"] = _profit_display_amount(
                calculation.get("server_cost_rub")
            )
            item["bogdan_display"] = _profit_display_amount(
                calculation.get("bogdan_profit_rub")
            )
            item["vlad_display"] = _profit_display_amount(calculation.get("vlad_net_rub"))
        history = get_profit_distributions(limit=8, include_void=True)
        for row in history:
            row["period_start_label"] = _format_profit_dt(row.get("period_start"))
            row["period_end_label"] = _format_profit_dt(row.get("period_end"))
            row["status_label"] = {
                "active": "рассчитано",
                "paid": "выплачено",
                "void": "отменено",
            }.get(str(row.get("status") or ""), str(row.get("status") or ""))

        return {
            "settings": settings,
            "slices": slices,
            "last_distribution": last_distribution,
            "project_start": project_start,
            "project_start_label": _format_profit_dt(project_start),
            "unsettled_start": unsettled_start,
            "unsettled_start_label": _format_profit_dt(unsettled_start),
            "now_iso": now_iso,
            "now_label": _format_profit_dt(now_iso),
            "now_input": _profit_input_value(now_iso),
            "current_month_start_input": _profit_input_value(
                _profit_iso(current_month_start)
            ),
            "distribution_periods": distribution_periods,
            "history": history,
        }

    def _build_dashboard_analytics(days: int | None = 30) -> dict:
        period_key = "all" if days is None else str(days)
        start_day = None
        if days is None:
            try:
                with sqlite3.connect(DB_FILE) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT MIN(day)
                        FROM (
                            SELECT substr(registration_date, 1, 10) AS day
                            FROM users
                            WHERE registration_date IS NOT NULL
                            UNION ALL
                            SELECT substr(COALESCE(paid_date, created_date), 1, 10) AS day
                            FROM transactions
                            WHERE COALESCE(paid_date, created_date) IS NOT NULL
                            UNION ALL
                            SELECT substr(created_date, 1, 10) AS day
                            FROM vpn_keys
                            WHERE created_date IS NOT NULL
                        )
                        WHERE day IS NOT NULL AND day != ''
                        """
                    )
                    start_day = cursor.fetchone()[0]
            except sqlite3.Error as e:
                logger.error("Failed to detect all-time dashboard start day: %s", e)
        dates = _date_range(days, start_day=start_day)
        date_set = set(dates)
        analytics = {
            "dates": dates,
            "labels": [_format_chart_day(day) for day in dates],
            "period": period_key,
            "period_days": days,
            "period_label": _format_period_label(period_key),
            "series": {
                "users": {day: 0 for day in dates},
                "revenue": {day: 0.0 for day in dates},
                "orders": {day: 0 for day in dates},
                "keys": {day: 0 for day in dates},
            },
            "summary": {
                "today_users": 0,
                "today_revenue": 0.0,
                "today_orders": 0,
                "period_users": 0,
                "period_revenue": 0.0,
                "period_orders": 0,
                "all_time_revenue": 0.0,
                "all_time_orders": 0,
                "active_paid_keys": 0,
                "expired_paid_keys": 0,
                "active_total_keys": 0,
                "expired_total_keys": 0,
                "active_trial_keys": 0,
                "expired_trial_keys": 0,
                "total_paid_keys": 0,
                "total_trial_keys": 0,
                "total_users": 0,
                "trial_users": 0,
                "trial_used_users": 0,
                "expired_trial_users": 0,
                "paying_users": 0,
                "paid_transaction_users": 0,
                "period_paid_transaction_users": 0,
                "paid_subscription_users": 0,
                "active_subscriptions": 0,
                "active_paid_subscriptions": 0,
                "active_free_subscriptions": 0,
                "active_access_subscriptions": 0,
                "expired_paid_subscriptions": 0,
                "expired_subscriptions": 0,
                "active_trial_subscriptions": 0,
                "active_paid_users": 0,
                "active_paid_users_with_payment": 0,
                "active_paid_users_without_payment": 0,
                "active_trial_users": 0,
                "expired_paid_users": 0,
                "expired_trial_users": 0,
                "conversion_percent": 0.0,
                "avg_order": 0.0,
            },
            "payment_methods": [],
            "plan_revenue": [],
            "top_days": [],
            "paid_transactions": [],
            "transaction_statuses": [],
        }

        today_key = time_utils.get_msk_now().date().isoformat()
        now = time_utils.get_msk_now()

        try:
            with sqlite3.connect(DB_FILE) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute("SELECT COUNT(*) FROM users")
                total_users = int(cursor.fetchone()[0] or 0)
                analytics["summary"]["total_users"] = total_users

                users_for_status = get_all_users()
                user_status_counts = _build_user_metrics(
                    [
                        {
                            **user,
                            "subscription_summary": _summarize_user_subscription(
                                user,
                                get_user_keys(int(user["telegram_id"])),
                                now,
                            ),
                        }
                        for user in users_for_status
                    ]
                )

                cursor.execute(
                    """
                    SELECT substr(registration_date, 1, 10) AS day, COUNT(*) AS count
                    FROM users
                    WHERE registration_date IS NOT NULL
                    GROUP BY day
                    """
                )
                for row in cursor.fetchall():
                    day = row["day"]
                    if day in date_set:
                        analytics["series"]["users"][day] = int(row["count"] or 0)

                cursor.execute(
                    """
                    SELECT telegram_id, username, total_spent, total_months, trial_used
                    FROM users
                    """
                )
                user_states = {
                    int(row["telegram_id"]): {
                        "user": dict(row),
                        "has_xui_key": False,
                        "has_active_xui_key": False,
                        "has_paid_key": False,
                        "has_active_paid_key": False,
                        "has_trial_key": False,
                        "has_active_trial_key": False,
                    }
                    for row in cursor.fetchall()
                }

                cursor.execute(
                    """
                    SELECT key_id, user_id, expiry_date, created_date, plan_id, service_type
                    FROM vpn_keys
                    WHERE COALESCE(service_type, 'xui') != 'mtg'
                    """
                )
                trial_duration_days = _configured_trial_duration_days()
                paying_users: set[int] = set()
                paid_transaction_users: set[int] = set()
                trial_users: set[int] = set()
                trial_used_users: set[int] = set()
                paid_subscription_users: set[int] = set()
                active_paid_users: set[int] = set()
                active_trial_users: set[int] = set()
                expired_paid_users: set[int] = set()
                expired_trial_users: set[int] = set()
                for row in cursor.fetchall():
                    key_row = dict(row)
                    user_id = int(key_row["user_id"])
                    state = user_states.setdefault(
                        user_id,
                        {
                            "user": {
                                "telegram_id": user_id,
                                "total_spent": 0,
                                "total_months": 0,
                                "trial_used": 0,
                            },
                            "has_xui_key": False,
                            "has_active_xui_key": False,
                            "has_paid_key": False,
                            "has_active_paid_key": False,
                            "has_trial_key": False,
                            "has_active_trial_key": False,
                        },
                    )
                    state["has_xui_key"] = True
                    expiry_dt = time_utils.parse_iso_to_msk(key_row["expiry_date"])
                    is_active = bool(expiry_dt and expiry_dt > now)
                    is_trial_key = _key_is_trial_for_user(
                        key_row, state["user"], trial_duration_days
                    )
                    if is_active:
                        state["has_active_xui_key"] = True
                    if not is_trial_key:
                        paid_subscription_users.add(user_id)
                        state["has_paid_key"] = True
                        analytics["summary"]["total_paid_keys"] += 1
                        if is_active:
                            analytics["summary"]["active_paid_keys"] += 1
                            state["has_active_paid_key"] = True
                            active_paid_users.add(user_id)
                        else:
                            analytics["summary"]["expired_paid_keys"] += 1
                            expired_paid_users.add(user_id)
                    else:
                        state["has_trial_key"] = True
                        analytics["summary"]["total_trial_keys"] += 1
                        if is_active:
                            analytics["summary"]["active_trial_keys"] += 1
                            state["has_active_trial_key"] = True
                            active_trial_users.add(user_id)
                            trial_users.add(user_id)
                        else:
                            analytics["summary"]["expired_trial_keys"] += 1
                            expired_trial_users.add(user_id)

                for state in user_states.values():
                    user = state["user"]
                    user_id = int(user["telegram_id"])
                    if _user_has_paid_subscription(user):
                        paying_users.add(user_id)
                    if user.get("trial_used"):
                        trial_used_users.add(user_id)
                        trial_users.add(user_id)
                        if not state["has_active_trial_key"]:
                            expired_trial_users.add(user_id)

                analytics["summary"]["active_total_keys"] = (
                    analytics["summary"]["active_paid_keys"]
                    + analytics["summary"]["active_trial_keys"]
                )
                analytics["summary"]["expired_total_keys"] = (
                    analytics["summary"]["expired_paid_keys"]
                    + analytics["summary"]["expired_trial_keys"]
                )
                analytics["summary"].update(
                    {
                        "paid_subscription_users": len(paid_subscription_users),
                        "active_paid_users": len(active_paid_users),
                        "active_trial_users": len(active_trial_users),
                        "expired_paid_users": len(expired_paid_users),
                        "expired_trial_users": len(expired_trial_users),
                    }
                )

                cursor.execute(
                    """
                    SELECT substr(created_date, 1, 10) AS day, COUNT(*) AS count
                    FROM vpn_keys
                    WHERE created_date IS NOT NULL
                      AND COALESCE(service_type, 'xui') != 'mtg'
                    GROUP BY day
                    """
                )
                for row in cursor.fetchall():
                    day = row["day"]
                    if day in date_set:
                        analytics["series"]["keys"][day] = int(row["count"] or 0)

                cursor.execute(
                    "SELECT plan_id, plan_name FROM plans"
                )
                plan_names = {
                    str(row["plan_id"]): row["plan_name"] for row in cursor.fetchall()
                }

                cursor.execute(
                    """
                    SELECT payment_id, user_id, username, status, amount_rub,
                           payment_method, metadata, created_date, paid_date
                    FROM transactions
                    ORDER BY COALESCE(paid_date, created_date) DESC
                    """
                )
                method_totals: dict[str, dict] = {}
                plan_totals: dict[str, dict] = {}
                status_counts: dict[str, int] = {}
                period_paid_transaction_users: set[int] = set()
                top_days: dict[str, dict] = {
                    day: {
                        "day": _format_chart_day(day),
                        "revenue": 0.0,
                        "orders": 0,
                        "users": 0,
                    }
                    for day in dates
                }
                paid_transactions: list[dict] = []

                for row in cursor.fetchall():
                    status = str(row["status"] or "unknown")
                    status_counts[status] = status_counts.get(status, 0) + 1
                    transaction_date = row["paid_date"] or row["created_date"]
                    day = str(transaction_date or "")[:10]
                    metadata = _safe_metadata(row["metadata"])
                    method = _format_payment_method(
                        row["payment_method"] or metadata.get("payment_method")
                    )
                    amount = float(row["amount_rub"] or 0)

                    if status != "paid":
                        continue

                    paid_transaction_users.add(int(row["user_id"]))
                    analytics["summary"]["all_time_revenue"] += amount
                    analytics["summary"]["all_time_orders"] += 1

                    if day in date_set:
                        period_paid_transaction_users.add(int(row["user_id"]))
                        analytics["series"]["revenue"][day] += amount
                        analytics["series"]["orders"][day] += 1
                        top_days[day]["revenue"] += amount
                        top_days[day]["orders"] += 1
                        top_days[day]["users"] += 1

                        method_bucket = method_totals.setdefault(
                            method, {"method": method, "revenue": 0.0, "orders": 0}
                        )
                        method_bucket["revenue"] += amount
                        method_bucket["orders"] += 1

                    plan_id = str(metadata.get("plan_id") or "")
                    if method == "Promo" and metadata.get("promo_code"):
                        duration_days = metadata.get("duration_days")
                        plan_name = (
                            f"Промокод {metadata.get('promo_code')} ({duration_days} дн.)"
                            if duration_days
                            else f"Промокод {metadata.get('promo_code')}"
                        )
                    else:
                        plan_name = (
                            metadata.get("plan_name")
                            or plan_names.get(plan_id)
                            or (f"Тариф #{plan_id}" if plan_id else "Не указан")
                        )
                    if day in date_set:
                        plan_bucket = plan_totals.setdefault(
                            plan_name, {"plan": plan_name, "revenue": 0.0, "orders": 0}
                        )
                        plan_bucket["revenue"] += amount
                        plan_bucket["orders"] += 1

                    paid_transactions.append(
                        {
                            "payment_id": row["payment_id"],
                            "user_id": row["user_id"],
                            "username": row["username"] or "N/A",
                            "amount": amount,
                            "method": method,
                            "plan": plan_name,
                            "date": transaction_date,
                            "date_label": _format_transaction_dt(transaction_date),
                        }
                    )

                period_revenue = sum(analytics["series"]["revenue"].values())
                period_orders = sum(analytics["series"]["orders"].values())
                period_users = sum(analytics["series"]["users"].values())
                paying_users.update(paid_transaction_users)
                analytics["summary"].update(
                    {
                        "today_users": analytics["series"]["users"].get(today_key, 0),
                        "today_revenue": analytics["series"]["revenue"].get(
                            today_key, 0.0
                        ),
                        "today_orders": analytics["series"]["orders"].get(today_key, 0),
                        "period_users": period_users,
                        "period_revenue": period_revenue,
                        "period_orders": period_orders,
                        "paying_users": len(paid_transaction_users),
                        "paid_transaction_users": len(paid_transaction_users),
                        "period_paid_transaction_users": len(
                            period_paid_transaction_users
                        ),
                        "active_subscriptions": user_status_counts["paid_users"],
                        "active_paid_subscriptions": user_status_counts["paid_users"],
                        "active_free_subscriptions": user_status_counts["free_users"],
                        "active_access_subscriptions": (
                            user_status_counts["paid_users"]
                            + user_status_counts["free_users"]
                            + user_status_counts["trial_users"]
                        ),
                        "expired_paid_subscriptions": user_status_counts[
                            "paid_expired_users"
                        ],
                        "expired_subscriptions": user_status_counts[
                            "paid_expired_users"
                        ],
                        "active_trial_subscriptions": len(active_trial_users),
                        "active_paid_users_with_payment": len(
                            active_paid_users & paid_transaction_users
                        ),
                        "active_paid_users_without_payment": len(
                            active_paid_users - paid_transaction_users
                        ),
                        "expired_paid_users": user_status_counts[
                            "paid_expired_users"
                        ],
                        "trial_users": user_status_counts["trial_users"],
                        "trial_used_users": len(trial_used_users),
                        "expired_trial_users": user_status_counts[
                            "trial_expired_users"
                        ],
                        "conversion_percent": (
                            round(len(paid_transaction_users) / total_users * 100, 1)
                            if total_users
                            else 0.0
                        ),
                        "avg_order": (
                            round(period_revenue / period_orders, 2)
                            if period_orders
                            else 0.0
                        ),
                    }
                )

                analytics["payment_methods"] = sorted(
                    method_totals.values(),
                    key=lambda item: item["revenue"],
                    reverse=True,
                )
                analytics["plan_revenue"] = sorted(
                    plan_totals.values(),
                    key=lambda item: item["revenue"],
                    reverse=True,
                )[:6]
                analytics["top_days"] = sorted(
                    top_days.values(),
                    key=lambda item: item["revenue"],
                    reverse=True,
                )[:5]
                analytics["paid_transactions"] = sorted(
                    paid_transactions,
                    key=lambda item: str(item.get("date") or ""),
                    reverse=True,
                )
                analytics["transaction_statuses"] = [
                    {
                        "status": status,
                        "status_label": _format_transaction_status(status),
                        "count": count,
                    }
                    for status, count in sorted(status_counts.items())
                ]
        except sqlite3.Error as e:
            logger.error("Failed to build dashboard analytics: %s", e, exc_info=True)

        return analytics

    def _run_async(coro, timeout: int = 45):
        """Run an async coroutine from a Flask sync route via the shared event loop.

        Replaces bare asyncio.run() calls which create new event loops per request
        and can hang Waitress worker threads indefinitely if XUI panel is unreachable.
        """
        loop = current_app.config.get("EVENT_LOOP")
        if not loop or not loop.is_running():
            raise RuntimeError("Основной event loop недоступен или не запущен.")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"Операция с XUI-панелью превысила лимит ожидания ({timeout}с). Проверьте доступность сервера."
            )

    def _run_auto_provision_for_global_users(context_host_name: str) -> bool:
        """Run global users auto-provisioning from admin host actions."""
        try:
            from shop_bot.data_manager.scheduler import (
                auto_provision_new_hosts_for_global_users,
            )

            loop = current_app.config.get("EVENT_LOOP")
            if loop and loop.is_running():

                async def _provision_wrapper():
                    try:
                        await auto_provision_new_hosts_for_global_users()
                        logger.info(
                            f"Auto-provisioning completed for host '{context_host_name}'"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to auto-provision for host '{context_host_name}': {e}",
                            exc_info=True,
                        )

                asyncio.run_coroutine_threadsafe(_provision_wrapper(), loop)
                logger.info(
                    f"Auto-provisioning scheduled for host '{context_host_name}'"
                )
                return True

            logger.warning(
                f"Event loop не доступен для автопровижинга хоста '{context_host_name}'. Пропускаем."
            )
            return False
        except Exception as e:
            logger.error(
                f"Failed to auto-provision for host '{context_host_name}': {e}",
                exc_info=True,
            )
            return False

    def _load_settings_page_context() -> dict:
        current_settings = get_all_settings()
        hosts = get_all_hosts()
        for host in hosts:
            host["plans"] = get_plans_for_host(host["host_name"], service_type="xui")
            if host.get("api_token"):
                host["api_token_configured"] = True
                host["api_token"] = ""

        mtg_hosts = get_all_mtg_hosts()
        for host in mtg_hosts:
            host["plans"] = get_plans_for_host(host["host_name"], service_type="mtg")

        safe_settings = dict(current_settings)
        for secret_key in SECRET_SETTINGS_KEYS:
            if safe_settings.get(secret_key):
                safe_settings[f"{secret_key}_configured"] = True
                safe_settings[secret_key] = ""

        return {
            "settings": safe_settings,
            "hosts": hosts,
            "global_plans": get_plans_for_host("ALL", service_type="xui"),
            "mtg_hosts": mtg_hosts,
            "payment_rules": get_all_payment_rules(),
            "all_payment_methods": ALL_PAYMENT_METHODS,
            "promo_codes": get_all_promo_codes(),
        }

    def _delete_remote_user_key(key: dict) -> bool:
        service_type = key.get("service_type", "xui")
        host_name = key.get("host_name")
        asset_name = key.get("key_email")
        if not host_name or not asset_name:
            return False

        if service_type == "mtg":
            from shop_bot.modules import mtg_api as _mtg_api

            node_id_raw = key.get("xui_client_uuid")
            try:
                node_id = int(node_id_raw)
            except (TypeError, ValueError):
                logger.error(
                    f"Cannot delete MTG proxy '{asset_name}' on host '{host_name}': invalid node id {node_id_raw!r}"
                )
                return False
            return bool(
                _run_async(
                    _mtg_api.delete_proxy_for_user(host_name, asset_name, node_id)
                )
            )

        return bool(_run_async(xui_api.delete_client_on_host(host_name, asset_name)))

    async def _sync_keys_job() -> dict:
        all_keys = get_all_keys_with_usernames()
        keys_by_host = {}
        for key in all_keys:
            host_name = key.get("host_name")
            if not host_name:
                continue
            keys_by_host.setdefault(host_name, []).append(key)

        total_updated = 0
        total_hosts = 0
        total_errors = 0
        hosts = [h["host_name"] for h in get_all_hosts(only_enabled=True)]
        for host_name in hosts:
            if host_name not in keys_by_host:
                continue
            total_hosts += 1
            try:
                mapping = await asyncio.wait_for(
                    xui_api.get_connection_strings_for_host(host_name), timeout=15
                )
            except Exception as k_e:
                total_errors += 1
                logger.warning(
                    f"Failed to sync host '{host_name}': {k_e!r}", exc_info=True
                )
                await asyncio.sleep(0.5)
                continue

            for key in keys_by_host[host_name]:
                email = key.get("key_email")
                if not email:
                    continue
                conn = mapping.get(email)
                if conn:
                    update_key_connection_string(key["key_id"], conn)
                    total_updated += 1

            await asyncio.sleep(0.5)

        return {
            "updated": total_updated,
            "hosts_checked": total_hosts,
            "errors": total_errors,
        }

    async def _fix_clients_job() -> dict:
        total_fixed = 0
        total_hosts = 0
        total_errors = 0
        hosts = [h["host_name"] for h in get_all_hosts(only_enabled=True)]
        for host_name in hosts:
            total_hosts += 1
            try:
                fixed = await asyncio.wait_for(
                    xui_api.fix_all_client_parameters_on_host(host_name), timeout=20
                )
                total_fixed += int(fixed)
            except Exception as k_e:
                total_errors += 1
                logger.warning(
                    f"Failed to fix clients on host '{host_name}': {k_e!r}",
                    exc_info=True,
                )
            await asyncio.sleep(1)

        return {
            "fixed": total_fixed,
            "hosts_checked": total_hosts,
            "errors": total_errors,
        }

    @flask_app.route("/")
    @login_required
    def index():
        return redirect(url_for("dashboard_page"))

    @flask_app.route("/dashboard")
    @login_required
    def dashboard_page():
        requested_period = (request.args.get("period") or "30").strip().lower()
        selected_period = (
            requested_period if requested_period in {"1", "7", "30", "all"} else "30"
        )
        period_days = None if selected_period == "all" else int(selected_period)
        problem_users = _build_problem_users(limit=10)
        stats = {
            "user_count": get_user_count(),
            "total_keys": get_total_keys_count(),
            "total_spent": get_total_spent_sum(),
            "host_count": len(get_all_hosts()),
            "problem_users_count": len(problem_users),
        }

        page = request.args.get("page", 1, type=int)
        per_page = 8

        transactions, total_transactions = get_paginated_transactions(
            page=page, per_page=per_page
        )
        total_pages = ceil(total_transactions / per_page)

        analytics = _build_dashboard_analytics(days=period_days)
        profit = _build_profit_context(
            analytics["summary"]["period_revenue"], selected_period, period_days
        )
        common_data = get_common_template_data()

        return render_template(
            "dashboard.html",
            stats=stats,
            problem_users=problem_users,
            analytics=analytics,
            profit=profit,
            transactions=transactions,
            current_page=page,
            total_pages=total_pages,
            period_options=_dashboard_period_options(selected_period),
            **common_data,
        )

    @flask_app.route("/dashboard/profit-settings", methods=["POST"])
    @login_required
    def update_profit_settings_route():
        tax_percent = _parse_percent_value(request.form.get("vlad_tax_percent"), 9.0)
        server_cost = _parse_money_value(request.form.get("server_cost_rub"), 0.0)
        update_setting("profit_vlad_tax_percent", str(tax_percent))
        update_setting("profit_server_cost_rub", str(server_cost))
        flash("Настройки дележа прибыли сохранены.", "success")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/dashboard/profit-distributions/create", methods=["POST"])
    @login_required
    def create_profit_distribution_route():
        settings = _profit_settings()
        now = time_utils.get_msk_now()
        now_iso = _profit_iso(now)
        current_month_start, _ = _month_bounds(now, 0)
        previous_month_start, previous_month_end = _month_bounds(now, -1)
        last_distribution = get_last_active_profit_distribution()
        project_start = _normalize_profit_dt(get_first_paid_transaction_date())
        last_distribution_end = _normalize_profit_dt(
            last_distribution.get("period_end") if last_distribution else None
        )
        selected_period = (request.form.get("selected_period") or "30").strip().lower()
        selected_days = (
            None
            if selected_period == "all"
            else int(selected_period)
            if selected_period in {"1", "7", "30"}
            else 30
        )
        selected_start, selected_end = _selected_period_bounds(
            selected_period, selected_days, now
        )
        period_scope = (request.form.get("distribution_period") or "unsettled").strip()
        period_options = {
            "unsettled": (
                last_distribution_end if last_distribution else project_start,
                now_iso,
            ),
            "current_month": (_profit_iso(current_month_start), now_iso),
            "previous_month": (
                _profit_iso(previous_month_start),
                _profit_iso(previous_month_end),
            ),
            "selected": (selected_start, selected_end),
            "all_time": (project_start, now_iso),
        }

        if period_scope == "custom":
            period_start = _parse_profit_dt_input(request.form.get("custom_period_start"))
            period_end = _parse_profit_dt_input(request.form.get("custom_period_end"))
            if not period_end:
                period_end = now_iso
        else:
            period_start, period_end = period_options.get(
                period_scope, period_options["unsettled"]
            )

        if not period_end:
            period_end = now_iso

        if period_start and datetime.fromisoformat(period_start) >= datetime.fromisoformat(
            period_end
        ):
            flash("Дата начала периода должна быть раньше даты окончания.", "warning")
            return redirect(request.referrer or url_for("dashboard_page"))

        if has_profit_distribution_overlap(period_start, period_end):
            flash(
                "Этот период пересекается с уже зафиксированной выплатой. Выберите другой период или отмените старую фиксацию.",
                "warning",
            )
            return redirect(request.referrer or url_for("dashboard_page"))

        revenue = get_paid_revenue_between(period_start, period_end)
        calculated = _calculate_partner_profit(revenue, settings)
        distribution_id = create_profit_distribution(
            period_start=period_start,
            period_end=period_end,
            revenue_rub=calculated["revenue_rub"],
            bogdan_share_percent=calculated["bogdan_share_percent"],
            vlad_share_percent=calculated["vlad_share_percent"],
            vlad_tax_percent=calculated["vlad_tax_percent"],
            server_cost_rub=calculated["server_cost_rub"],
            bogdan_profit_rub=calculated["bogdan_profit_rub"],
            vlad_gross_rub=calculated["vlad_gross_rub"],
            vlad_tax_rub=calculated["vlad_tax_rub"],
            vlad_net_rub=calculated["vlad_net_rub"],
            note=(request.form.get("note") or "").strip() or None,
        )
        if distribution_id:
            flash("Распределение прибыли зафиксировано в истории.", "success")
        else:
            flash("Не удалось зафиксировать распределение прибыли.", "danger")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route(
        "/dashboard/profit-distributions/<int:distribution_id>/update",
        methods=["POST"],
    )
    @login_required
    def update_profit_distribution_route(distribution_id: int):
        try:
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT status FROM profit_distributions WHERE distribution_id = ?",
                    (distribution_id,),
                )
                row = cursor.fetchone()
            if not row:
                flash("Запись распределения не найдена.", "warning")
                return redirect(request.referrer or url_for("dashboard_page"))
            if row[0] != "active":
                flash(
                    "Можно редактировать только активную фиксацию. Выплаченную или отменённую запись сначала нельзя менять.",
                    "warning",
                )
                return redirect(request.referrer or url_for("dashboard_page"))
        except sqlite3.Error as e:
            logger.error(
                "Failed to read profit distribution %s before update: %s",
                distribution_id,
                e,
            )
            flash("Не удалось проверить запись распределения.", "danger")
            return redirect(request.referrer or url_for("dashboard_page"))

        settings = {
            "bogdan_share_percent": _parse_percent_value(
                request.form.get("bogdan_share_percent"), 40.0
            ),
            "vlad_share_percent": _parse_percent_value(
                request.form.get("vlad_share_percent"), 60.0
            ),
            "vlad_tax_percent": _parse_percent_value(
                request.form.get("vlad_tax_percent"), 9.0
            ),
            "server_cost_rub": _parse_money_value(
                request.form.get("server_cost_rub"), 0.0
            ),
        }
        period_start = (request.form.get("period_start") or "").strip() or None
        period_end = (
            (request.form.get("period_end") or "").strip()
            or time_utils.get_msk_now().isoformat()
        )
        try:
            parsed_period_start = (
                datetime.fromisoformat(period_start) if period_start else None
            )
            parsed_period_end = datetime.fromisoformat(period_end)
        except ValueError:
            flash("Неверный формат даты периода.", "warning")
            return redirect(request.referrer or url_for("dashboard_page"))

        if parsed_period_start and parsed_period_start >= parsed_period_end:
            flash("Дата начала периода должна быть раньше даты окончания.", "warning")
            return redirect(request.referrer or url_for("dashboard_page"))

        if has_profit_distribution_overlap(
            period_start, period_end, exclude_distribution_id=distribution_id
        ):
            flash(
                "После правки период пересекается с другой фиксацией. Изменения не сохранены.",
                "warning",
            )
            return redirect(request.referrer or url_for("dashboard_page"))

        default_revenue = get_paid_revenue_between(period_start, period_end)
        revenue = _parse_money_value(request.form.get("revenue_rub"), default_revenue)
        calculated = _calculate_partner_profit(revenue, settings)
        ok = update_profit_distribution(
            distribution_id,
            period_start=period_start,
            period_end=period_end,
            revenue_rub=calculated["revenue_rub"],
            bogdan_share_percent=calculated["bogdan_share_percent"],
            vlad_share_percent=calculated["vlad_share_percent"],
            vlad_tax_percent=calculated["vlad_tax_percent"],
            server_cost_rub=calculated["server_cost_rub"],
            bogdan_profit_rub=calculated["bogdan_profit_rub"],
            vlad_gross_rub=calculated["vlad_gross_rub"],
            vlad_tax_rub=calculated["vlad_tax_rub"],
            vlad_net_rub=calculated["vlad_net_rub"],
            note=(request.form.get("note") or "").strip() or None,
        )
        flash(
            (
                "Запись распределения обновлена."
                if ok
                else "Запись распределения не найдена."
            ),
            "success" if ok else "warning",
        )
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route(
        "/dashboard/profit-distributions/<int:distribution_id>/paid",
        methods=["POST"],
    )
    @login_required
    def mark_profit_distribution_paid_route(distribution_id: int):
        ok = mark_profit_distribution_paid(distribution_id)
        flash(
            "Фиксация отмечена как выплаченная."
            if ok
            else "Можно отметить выплаченной только активную фиксацию.",
            "success" if ok else "warning",
        )
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route(
        "/dashboard/profit-distributions/<int:distribution_id>/void",
        methods=["POST"],
    )
    @login_required
    def void_profit_distribution_route(distribution_id: int):
        ok = void_profit_distribution(distribution_id)
        flash(
            "Фиксация отменена. Следующий расчёт снова учтёт этот период."
            if ok
            else "Активная фиксация не найдена.",
            "success" if ok else "warning",
        )
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/users")
    @login_required
    def users_page():
        users = get_all_users()
        now = time_utils.get_msk_now()
        for user in users:
            user["user_keys"] = get_user_keys(user["telegram_id"])
            user["subscription_summary"] = _summarize_user_subscription(
                user, user["user_keys"], now
            )
        user_metrics = _build_user_metrics(users)

        # Prepare plans for manual issuance
        all_hosts = get_all_hosts()
        # Structure: {'global': [plans], 'hosts': {hostname: [plans]}}
        issuance_data = {
            "global_plans": get_plans_for_host("ALL", service_type="xui"),
            "host_plans": {},
        }
        for host in all_hosts:
            plans = get_plans_for_host(host["host_name"], service_type="xui")
            if plans:
                issuance_data["host_plans"][host["host_name"]] = plans

        common_data = get_common_template_data()
        return render_template(
            "users.html",
            users=users,
            user_metrics=user_metrics,
            issuance_data=issuance_data,
            **common_data,
        )

    @flask_app.route("/export/users.csv")
    @login_required
    def export_users_csv():
        rows = []
        now = time_utils.get_msk_now()
        for user in get_all_users():
            keys = get_user_keys(int(user["telegram_id"]))
            summary = _summarize_user_subscription(user, keys, now)
            rows.append(
                {
                    "telegram_id": user.get("telegram_id"),
                    "username": user.get("username") or "",
                    "receipt_email": user.get("receipt_email") or "",
                    "is_banned": int(bool(user.get("is_banned"))),
                    "trial_used": int(bool(user.get("trial_used"))),
                    "subscription_status": summary["status"],
                    "registration_date": user.get("registration_date") or "",
                    "total_spent": user.get("total_spent") or 0,
                    "total_months": user.get("total_months") or 0,
                    "keys_total": len(keys),
                    "keys_active": summary["active_total"],
                    "paid_keys_active": summary["paid_active"],
                    "paid_keys_total": summary["paid_keys_total"],
                    "latest_paid_expiry": (
                        time_utils.format_msk(summary["latest_paid_expiry"])
                        if summary["latest_paid_expiry"]
                        else ""
                    ),
                    "trial_keys_active": summary["trial_active"],
                    "pending_transactions": summary["pending_transaction_count"],
                    "free_transactions": summary["free_transaction_count"],
                    "support_tickets": summary["support_ticket_count"],
                    "support_messages": summary["support_message_count"],
                }
            )

        return _csv_response(
            rows,
            filename=f"users-{time_utils.get_msk_now().strftime('%Y%m%d-%H%M%S')}.csv",
            fieldnames=[
                "telegram_id",
                "username",
                "receipt_email",
                "is_banned",
                "trial_used",
                "subscription_status",
                "registration_date",
                "total_spent",
                "total_months",
                "keys_total",
                "keys_active",
                "paid_keys_active",
                "paid_keys_total",
                "latest_paid_expiry",
                "trial_keys_active",
            ],
        )

    @flask_app.route("/export/keys.csv")
    @login_required
    def export_keys_csv():
        rows = []
        all_keys = get_all_keys_with_usernames()
        for key in all_keys:
            rows.append(
                {
                    "key_id": key.get("key_id"),
                    "user_id": key.get("user_id"),
                    "username": key.get("username") or "",
                    "host_name": key.get("host_name") or "",
                    "key_email": key.get("key_email") or "",
                    "plan_id": key.get("plan_id"),
                    "days_left": key.get("days_left"),
                    "expiry_date": key.get("expiry_date") or "",
                    "created_date": key.get("created_date") or "",
                }
            )

        return _csv_response(
            rows,
            filename=f"keys-{time_utils.get_msk_now().strftime('%Y%m%d-%H%M%S')}.csv",
            fieldnames=[
                "key_id",
                "user_id",
                "username",
                "host_name",
                "key_email",
                "plan_id",
                "days_left",
                "expiry_date",
                "created_date",
            ],
        )

    @flask_app.route("/export/transactions.csv")
    @login_required
    def export_transactions_csv():
        rows = []
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT transaction_id, payment_id, user_id, username, status, amount_rub,
                       amount_currency, currency_name, payment_method, metadata, created_date, paid_date
                FROM transactions
                ORDER BY COALESCE(paid_date, created_date) DESC
                """)
            for row in cursor.fetchall():
                rows.append(dict(row))

        return _csv_response(
            rows,
            filename=f"transactions-{time_utils.get_msk_now().strftime('%Y%m%d-%H%M%S')}.csv",
            fieldnames=[
                "transaction_id",
                "payment_id",
                "user_id",
                "username",
                "status",
                "amount_rub",
                "amount_currency",
                "currency_name",
                "payment_method",
                "metadata",
                "created_date",
                "paid_date",
            ],
        )

    @flask_app.route("/users/diagnostics/<int:user_id>")
    @login_required
    def user_diagnostics_page(user_id: int):
        user = get_user(user_id)
        if not user:
            flash(f"Пользователь {user_id} не найден.", "danger")
            return redirect(url_for("users_page"))

        keys = get_user_keys(user_id)
        now = time_utils.get_msk_now()
        rows = []
        issues_total = 0

        for key in keys:
            host_name = key.get("host_name")
            key_email = key.get("key_email")
            db_expiry = time_utils.parse_iso_to_msk(key.get("expiry_date"))
            db_active = bool(db_expiry and db_expiry > now)
            issue_list: list[str] = []

            panel_found = False
            panel_enabled = None
            panel_expiry = None
            panel_total = None
            panel_up = None
            panel_down = None

            host_data = get_host(host_name) if host_name else None
            if not host_data:
                issue_list.append("Хост отсутствует или удален из настроек")
            else:
                try:
                    api, inbound = xui_api.login_to_host(
                        host_url=host_data["host_url"],
                        username=host_data["host_username"],
                        password=host_data["host_pass"],
                        inbound_id=host_data["host_inbound_id"],
                        api_token=host_data.get("api_token"),
                    )
                    if not api or not inbound:
                        issue_list.append("Не удалось подключиться к панели XUI")
                    else:
                        inbound_fresh = api.inbound.get_by_id(inbound.id)
                        clients = (
                            (inbound_fresh.settings.clients or [])
                            if inbound_fresh
                            else []
                        )
                        client = next(
                            (
                                c
                                for c in clients
                                if getattr(c, "email", None) == key_email
                            ),
                            None,
                        )
                        if not client:
                            issue_list.append("Клиент отсутствует на панели XUI")
                        else:
                            panel_found = True
                            panel_enabled = bool(getattr(client, "enable", True))
                            panel_total = getattr(client, "total", None)
                            panel_up = getattr(client, "up", None)
                            panel_down = getattr(client, "down", None)
                            expiry_ms = int(getattr(client, "expiry_time", 0) or 0)
                            panel_expiry = (
                                time_utils.from_timestamp_ms(expiry_ms)
                                if expiry_ms > 0
                                else None
                            )

                            if db_active and not panel_enabled:
                                issue_list.append(
                                    "В БД ключ активен, но на панели выключен"
                                )
                            if (not db_active) and panel_enabled:
                                issue_list.append(
                                    "В БД ключ просрочен, но на панели включен"
                                )

                            if db_expiry and panel_expiry:
                                diff_seconds = abs(
                                    (db_expiry - panel_expiry).total_seconds()
                                )
                                if diff_seconds > 90:
                                    issue_list.append(
                                        f"Расхождение срока БД/панель: {int(diff_seconds // 60)} мин."
                                    )
                except Exception as e:
                    logger.error(
                        f"Diagnostics failed for user={user_id}, key={key_email}: {e}",
                        exc_info=True,
                    )
                    issue_list.append(f"Ошибка диагностики: {e}")

            if issue_list:
                issues_total += 1

            rows.append(
                {
                    "key_id": key.get("key_id"),
                    "host_name": host_name,
                    "key_email": key_email,
                    "plan_id": key.get("plan_id"),
                    "db_expiry": db_expiry,
                    "db_active": db_active,
                    "panel_found": panel_found,
                    "panel_enabled": panel_enabled,
                    "panel_expiry": panel_expiry,
                    "panel_total": panel_total,
                    "panel_up": panel_up,
                    "panel_down": panel_down,
                    "issues": issue_list,
                }
            )

        common_data = get_common_template_data()
        return render_template(
            "user_diagnostics.html",
            diagnostic_user=user,
            diagnostic_rows=rows,
            issues_total=issues_total,
            checked_total=len(rows),
            **common_data,
        )

    @flask_app.route("/keys")
    @login_required
    def keys_page():
        all_keys = get_all_keys_with_usernames()
        subscription_domain = get_setting("domain")
        now = time_utils.get_msk_now()
        enabled_xui_hosts = {
            host.get("host_name")
            for host in get_all_hosts(only_enabled=True)
            if host.get("host_name")
        }

        try:
            global_plan_ids = get_global_plan_ids()
        except Exception:
            global_plan_ids = set()
        trial_duration_days = _configured_trial_duration_days()

        # Group keys by user and mark global ones
        users_map = {}
        active_subscription_users: set[int] = set()
        active_trial_users: set[int] = set()
        active_paid_users: set[int] = set()
        for key in all_keys:
            uid = key["user_id"]
            if uid not in users_map:
                users_map[uid] = {
                    "username": key.get("username") or f"User {uid}",
                    "user_id": uid,
                    "subscription_link": None,
                    "user_keys": [],
                }

            key["is_global"] = is_global_xui_key(
                key, global_plan_ids, enabled_xui_hosts
            )
            key["is_trial"] = _key_is_trial_for_owner(key, trial_duration_days)
            key["is_free_access"] = (
                _is_xui_key(key)
                and not key["is_trial"]
                and _key_plan_id(key) <= 0
            )
            key["expiry_status"] = _build_key_expiry_status(
                key, is_trial=key["is_trial"], now=now
            )
            if key["expiry_status"]["filter"] != "expired" and _is_xui_key(key):
                active_subscription_users.add(int(uid))
                if key["is_trial"]:
                    active_trial_users.add(int(uid))
                else:
                    active_paid_users.add(int(uid))
            key["copy_value"] = (key.get("connection_string") or "").strip()
            key["has_copy_value"] = bool(key["copy_value"])
            key["copy_kind"] = (
                "Telegram Proxy" if key.get("service_type") == "mtg" else "VPN ключ"
            )
            if not users_map[uid]["subscription_link"]:
                users_map[uid]["subscription_link"] = _build_subscription_link(
                    subscription_domain, key.get("subscription_token")
                )
            users_map[uid]["user_keys"].append(key)

        def _expiry_ts(key_item: dict) -> float:
            dt = time_utils.parse_iso_to_msk(key_item.get("expiry_date"))
            return dt.timestamp() if dt else 0.0

        # Keep only one GLOBAL key per host for display (latest expiry wins).
        # This prevents "3 servers from 2 hosts" when legacy duplicate rows exist.
        for user_data in users_map.values():
            global_by_host: dict[str, dict] = {}
            regular_keys: list[dict] = []
            for key in user_data["user_keys"]:
                if not key.get("is_global"):
                    regular_keys.append(key)
                    continue

                host_name = key.get("host_name") or ""
                prev = global_by_host.get(host_name)
                if not prev or _expiry_ts(key) >= _expiry_ts(prev):
                    global_by_host[host_name] = key

            deduped_global = sorted(global_by_host.values(), key=_expiry_ts)
            user_data["user_keys"] = deduped_global + regular_keys
            user_data["is_trial"] = bool(deduped_global) and all(
                key.get("is_trial") for key in deduped_global
            )
            user_data["is_free_access"] = bool(deduped_global) and all(
                key.get("is_free_access") for key in deduped_global
            )

        grouped_users = sorted(users_map.values(), key=lambda u: u["username"])
        key_stats = {
            "total_users": get_user_count(),
            "users_with_keys": len(grouped_users),
            "total_key_rows": len(all_keys),
            "active_subscriptions": len(active_subscription_users),
            "active_paid_users": len(active_paid_users),
            "active_trial_users": len(active_trial_users),
        }

        common_data = get_common_template_data()
        return render_template(
            "keys.html",
            grouped_users=grouped_users,
            key_stats=key_stats,
            task_statuses=_task_status_snapshot(),
            **common_data,
        )

    @flask_app.route("/api/tasks/status", methods=["GET"])
    @login_required
    def tasks_status_route():
        return {"status": "success", "tasks": _task_status_snapshot()}

    @flask_app.route("/keys/adjust/<int:key_id>", methods=["POST"])
    @login_required
    def adjust_key_duration(key_id):
        """Adjust key duration by days and/or hours. Supports negative values to reduce duration."""
        try:
            days_to_adjust = int(request.form.get("days", 0))
            hours_to_adjust = int(request.form.get("hours", 0))

            # Calculate total seconds to adjust
            total_seconds = days_to_adjust * 86400 + hours_to_adjust * 3600

            if total_seconds == 0:
                flash("Укажите количество дней или часов для изменения.", "warning")
                return redirect(url_for("keys_page"))

            key_data = get_key_by_id(key_id)
            if not key_data:
                flash(f"Ключ {key_id} не найден.", "danger")
                return redirect(url_for("keys_page"))
            if (
                key_data.get("service_type") == "mtg"
                and (hours_to_adjust != 0 or days_to_adjust <= 0)
            ):
                flash(
                    "Telegram Proxy можно продлить только на положительное число полных дней.",
                    "warning",
                )
                return redirect(url_for("keys_page"))

            # Check if this key belongs to a Global Plan
            is_global = False
            try:
                enabled_xui_hosts = {
                    host.get("host_name")
                    for host in get_all_hosts(only_enabled=True)
                    if host.get("host_name")
                }
                global_plan_ids = get_global_plan_ids()
                if is_global_xui_key(key_data, global_plan_ids, enabled_xui_hosts):
                    is_global = True
            except Exception as e:
                logger.error(f"Error checking global plan status: {e}")

            keys_to_adjust = [key_data]
            if is_global:
                user_keys = get_user_keys(key_data["user_id"])
                # Find other global keys for this user
                for k in user_keys:
                    if k["key_id"] != key_id and is_global_xui_key(
                        k, global_plan_ids, enabled_xui_hosts
                    ):
                        keys_to_adjust.append(k)

            success_count = 0
            new_expiry_date = None

            for k in keys_to_adjust:
                if k.get("service_type") == "mtg":
                    try:
                        node_id = int(k.get("xui_client_uuid") or 0)
                    except (TypeError, ValueError):
                        logger.error(
                            "Cannot adjust MTG key_id=%s: invalid node id %r.",
                            k.get("key_id"),
                            k.get("xui_client_uuid"),
                        )
                        continue
                    current_expiry = time_utils.parse_iso_to_msk(k.get("expiry_date"))
                    current_expiry_ms = (
                        int(current_expiry.timestamp() * 1000)
                        if current_expiry
                        else 0
                    )
                    new_expiry_ms = _run_async(
                        mtg_api.renew_proxy_for_user(
                            k["host_name"],
                            k["key_email"],
                            node_id,
                            days_to_adjust,
                            current_expiry_ms,
                        )
                    )
                    if not new_expiry_ms:
                        continue
                    expiry_dt = time_utils.from_timestamp_ms(new_expiry_ms)
                    updated = update_key_info(k["key_id"], expiry_dt)
                    if not updated:
                        logger.error(
                            "Admin MTG duration adjustment succeeded on host %s but DB update failed for key_id=%s.",
                            k["host_name"],
                            k["key_id"],
                        )
                        continue
                    success_count += 1
                    new_expiry_date = expiry_dt
                    continue

                # Call XUI logic to adjust on panel using seconds for precision.
                result = _run_async(
                    xui_api.create_or_update_key_on_host_seconds(
                        host_name=k["host_name"],
                        email=k["key_email"],
                        seconds_to_add=total_seconds,
                        telegram_id=None,  # Admin adjustment, no telegram_id available
                    )
                )

                if result:
                    # Update local DB with new expiry from result
                    expiry_dt = time_utils.from_timestamp_ms(
                        result["expiry_timestamp_ms"]
                    )
                    updated = update_key_info(
                        k["key_id"], expiry_dt, result.get("connection_string")
                    )
                    if not updated:
                        logger.error(
                            "Admin duration adjustment succeeded on host %s but DB update failed for key_id=%s.",
                            k["host_name"],
                            k["key_id"],
                        )
                        continue
                    success_count += 1
                    new_expiry_date = expiry_dt

            if success_count > 0:
                # Format the change message
                action_text = "продлена" if total_seconds > 0 else "уменьшена"
                time_parts = []
                abs_days = abs(days_to_adjust)
                abs_hours = abs(hours_to_adjust)
                if abs_days > 0:
                    time_parts.append(f"{abs_days} дн.")
                if abs_hours > 0:
                    time_parts.append(f"{abs_hours} ч.")
                time_str = " ".join(time_parts) if time_parts else "0"

                # Notify User
                bot = _bot_controller.get_bot_instance()
                if bot:
                    user_id = key_data["user_id"]
                    if total_seconds > 0:
                        msg_text = (
                            f"🎁 <b>Вам начислен бонус!</b>\n\n"
                            f"Администратор продлил вашу подписку на <b>{time_str}</b>\n"
                            f"Обновлено ключей: {success_count}.\n"
                        )
                    else:
                        msg_text = (
                            f"⚠️ <b>Изменение подписки</b>\n\n"
                            f"Срок вашей подписки был уменьшен на <b>{time_str}</b>\n"
                            f"Обновлено ключей: {success_count}.\n"
                        )
                    if new_expiry_date:
                        msg_text += f"Новая дата окончания: <b>{new_expiry_date.strftime('%d.%m.%Y %H:%M')}</b>"

                    loop = current_app.config.get("EVENT_LOOP")
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot.send_message(user_id, msg_text, parse_mode="HTML"), loop
                        )

                if is_global:
                    flash(
                        f"Глобальная подписка {action_text}! Обновлено {success_count} ключей на {time_str}.",
                        "success",
                    )
                else:
                    service_label = (
                        "Telegram Proxy"
                        if key_data.get("service_type") == "mtg"
                        else f"Ключ #{key_id}"
                    )
                    flash(f"{service_label} успешно изменён на {time_str}.", "success")
            else:
                service_label = (
                    "Telegram Proxy"
                    if key_data.get("service_type") == "mtg"
                    else "XUI"
                )
                flash(f"Ошибка при изменении ключа(ей) на сервере {service_label}.", "danger")

        except Exception as e:
            logger.error(f"Error adjusting key duration: {e}", exc_info=True)
            flash("Произошла ошибка при изменении.", "danger")

        return redirect(url_for("keys_page"))

    @flask_app.route("/keys/sync", methods=["POST"])
    @login_required
    def sync_keys_configs():
        try:
            loop = current_app.config.get("EVENT_LOOP")
            if not loop or not loop.is_running():
                flash("Цикл событий недоступен. Перезапустите приложение.", "danger")
                return redirect(url_for("keys_page"))

            _set_task_status(
                "sync_configs", "running", "Синхронизация конфигов запущена"
            )

            async def _sync_all_keys_wrapper():
                try:
                    result = await _sync_keys_job()
                    _set_task_status(
                        "sync_configs",
                        "success",
                        f"Готово: обновлено {result['updated']} ключей",
                        details=result,
                    )
                    logger.info(f"Sync keys completed: {result}")
                except Exception as e:
                    logger.error(
                        f"Error in sync keys background job: {e}", exc_info=True
                    )
                    _set_task_status(
                        "sync_configs", "error", f"Ошибка синхронизации: {e}"
                    )

            asyncio.run_coroutine_threadsafe(_sync_all_keys_wrapper(), loop)
            flash("Синхронизация ключей запущена в фоне. Проверьте логи позже.", "info")
        except Exception as e:
            logger.error(f"Error syncing keys: {e}", exc_info=True)
            _set_task_status("sync_configs", "error", f"Не удалось запустить: {e}")
            flash("Не удалось запустить синхронизацию ключей.", "danger")

        return redirect(url_for("keys_page"))

    @flask_app.route("/keys/fix-parameters", methods=["POST"])
    @login_required
    def fix_client_parameters():
        try:
            loop = current_app.config.get("EVENT_LOOP")
            if not loop or not loop.is_running():
                flash("Цикл событий недоступен. Перезапустите приложение.", "danger")
                return redirect(url_for("keys_page"))

            _set_task_status(
                "fix_parameters", "running", "Исправление параметров запущено"
            )

            async def _fix_all_clients_wrapper():
                try:
                    result = await _fix_clients_job()
                    _set_task_status(
                        "fix_parameters",
                        "success",
                        f"Готово: исправлено {result['fixed']} клиентов",
                        details=result,
                    )
                    logger.info(f"Fix parameters completed: {result}")
                except Exception as e:
                    logger.error(
                        f"Fix parameters background job failed: {e}", exc_info=True
                    )
                    _set_task_status(
                        "fix_parameters", "error", f"Ошибка исправления: {e}"
                    )

            asyncio.run_coroutine_threadsafe(_fix_all_clients_wrapper(), loop)
            flash(
                "Исправление параметров запущено в фоне. Проверьте логи позже.", "info"
            )
        except Exception as e:
            logger.error(f"Fix parameters error: {e}", exc_info=True)
            _set_task_status("fix_parameters", "error", f"Не удалось запустить: {e}")
            flash("Не удалось запустить исправление параметров клиентов.", "danger")
        return redirect(url_for("keys_page"))

    @flask_app.route("/keys/maintenance", methods=["POST"])
    @login_required
    def maintenance_route():
        try:
            loop = current_app.config.get("EVENT_LOOP")
            if not loop or not loop.is_running():
                flash("Цикл событий недоступен. Перезапустите приложение.", "danger")
                return redirect(url_for("keys_page"))

            _set_task_status(
                "maintenance", "running", "Комплексное обслуживание запущено"
            )
            _set_task_status(
                "sync_configs", "running", "Синхронизация конфигов запущена"
            )

            async def _maintenance_wrapper():
                try:
                    sync_result = await _sync_keys_job()
                    _set_task_status(
                        "sync_configs",
                        "success",
                        f"Готово: обновлено {sync_result['updated']} ключей",
                        details=sync_result,
                    )
                    _set_task_status(
                        "fix_parameters", "running", "Исправление параметров запущено"
                    )
                    fix_result = await _fix_clients_job()
                    _set_task_status(
                        "fix_parameters",
                        "success",
                        f"Готово: исправлено {fix_result['fixed']} клиентов",
                        details=fix_result,
                    )
                    _set_task_status(
                        "maintenance",
                        "success",
                        "Комплексное обслуживание завершено",
                        details={"sync": sync_result, "fix": fix_result},
                    )
                except Exception as e:
                    logger.error(
                        f"Maintenance background job failed: {e}", exc_info=True
                    )
                    _set_task_status(
                        "maintenance", "error", f"Ошибка обслуживания: {e}"
                    )

            asyncio.run_coroutine_threadsafe(_maintenance_wrapper(), loop)
            flash("Комплексное обслуживание запущено в фоне.", "info")
        except Exception as e:
            logger.error(f"Maintenance route error: {e}", exc_info=True)
            _set_task_status("maintenance", "error", f"Не удалось запустить: {e}")
            flash("Не удалось запустить комплексное обслуживание.", "danger")
        return redirect(url_for("keys_page"))

    @flask_app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
        if request.method == "POST":
            if "panel_password" in request.form and request.form.get("panel_password"):
                update_setting(
                    "panel_password",
                    generate_password_hash(request.form.get("panel_password")),
                )

            for checkbox_key in [
                "force_subscription",
                "show_about_menu_item",
                "sbp_enabled",
                "trial_enabled",
                "enable_referrals",
                "p2p_enabled",
                "stars_enabled",
                "yookassa_enabled",
                "cryptobot_enabled",
                "enable_admin_payment_notifications",
                "enable_admin_trial_notifications",
                "email_prompt_enabled",
                "enable_promo_codes",
            ]:
                values = request.form.getlist(checkbox_key)
                value = values[-1] if values else "false"
                update_setting(checkbox_key, "true" if value == "true" else "false")

            for key in ALL_SETTINGS_KEYS:
                if key in [
                    "panel_password",
                    "force_subscription",
                    "show_about_menu_item",
                    "sbp_enabled",
                    "trial_enabled",
                    "enable_referrals",
                    "p2p_enabled",
                    "stars_enabled",
                    "yookassa_enabled",
                    "cryptobot_enabled",
                    "enable_admin_payment_notifications",
                    "enable_admin_trial_notifications",
                    "email_prompt_enabled",
                    "enable_promo_codes",
                ]:
                    continue
                value = request.form.get(key)
                if value is not None:
                    if key in SECRET_SETTINGS_KEYS and not value.strip():
                        continue
                    update_setting(key, value)

            flash("Настройки успешно сохранены!", "success")
            return redirect(url_for("settings_page"))

        common_data = get_common_template_data()
        return render_template(
            "settings.html", **_load_settings_page_context(), **common_data
        )

    @flask_app.route("/start-shop-bot", methods=["POST"])
    @login_required
    def start_shop_bot_route():
        result = _bot_controller.start_shop_bot()
        flash(
            result.get("message", "An error occurred."),
            "success" if result.get("status") == "success" else "danger",
        )
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/stop-shop-bot", methods=["POST"])
    @login_required
    def stop_shop_bot_route():
        result = _bot_controller.stop_shop_bot()
        flash(
            result.get("message", "An error occurred."),
            "success" if result.get("status") == "success" else "danger",
        )
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/start-support-bot", methods=["POST"])
    @login_required
    def start_support_bot_route():
        result = _bot_controller.start_support_bot()
        flash(
            result.get("message", "An error occurred."),
            "success" if result.get("status") == "success" else "danger",
        )
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/stop-support-bot", methods=["POST"])
    @login_required
    def stop_support_bot_route():
        result = _bot_controller.stop_support_bot()
        flash(
            result.get("message", "An error occurred."),
            "success" if result.get("status") == "success" else "danger",
        )
        return redirect(request.referrer or url_for("dashboard_page"))

    # ==========================
    # UPDATE SYSTEM ROUTES
    # ==========================
    @flask_app.route("/updates", methods=["GET"])
    @login_required
    def updates_page():
        common_data = get_common_template_data()
        current_version = APP_VERSION
        return render_template(
            "updates.html", current_version=current_version, **common_data
        )

    @flask_app.route("/api/updates/check", methods=["POST"])
    @login_required
    def check_updates_route():
        result = update_manager.check_for_updates()
        if "error" in result:
            return {"status": "error", "message": result["error"]}, 500
        return {"status": "success", "data": result}

    @flask_app.route("/api/updates/perform", methods=["POST"])
    @login_required
    def perform_update_route():
        # This is a potentially long running task, ideally should be async.
        # But since it restarts the app, we can just return and let it die.
        result = update_manager.perform_update()
        if result["status"] == "error":
            return {"status": "error", "message": result["message"]}, 500

        # On success, the container will likely restart shortly, so the frontend might see a network error or reload.
        return {"status": "success", "message": result["message"]}

    @flask_app.route("/users/ban/<int:user_id>", methods=["POST"])
    @login_required
    def ban_user_route(user_id):
        ban_user(user_id)
        flash(f"Пользователь {user_id} был заблокирован.", "success")
        return redirect(url_for("users_page"))

    @flask_app.route("/users/unban/<int:user_id>", methods=["POST"])
    @login_required
    def unban_user_route(user_id):
        unban_user(user_id)
        flash(f"Пользователь {user_id} был разблокирован.", "success")
        return redirect(url_for("users_page"))

    @flask_app.route("/users/revoke/<int:user_id>", methods=["POST"])
    @login_required
    def revoke_keys_route(user_id):
        keys_to_revoke = get_user_keys(user_id)
        success_count = 0
        deleted_key_ids: list[int] = []
        failed_keys: list[str] = []

        for key in keys_to_revoke:
            try:
                result = _delete_remote_user_key(key)
                if result:
                    success_count += 1
                    if key.get("key_id") is not None:
                        deleted_key_ids.append(int(key["key_id"]))
                else:
                    failed_keys.append(
                        key.get("key_email") or f"key:{key.get('key_id')}"
                    )
            except Exception as e:
                logger.error(
                    f"Failed to revoke key '{key.get('key_email')}' for user {user_id}: {e}",
                    exc_info=True,
                )
                failed_keys.append(key.get("key_email") or f"key:{key.get('key_id')}")

        if deleted_key_ids:
            delete_keys_by_ids(deleted_key_ids)

        if success_count == len(keys_to_revoke):
            flash(
                f"Все {len(keys_to_revoke)} ключей для пользователя {user_id} были успешно отозваны.",
                "success",
            )
        else:
            flash(
                f"Удалось отозвать {success_count} из {len(keys_to_revoke)} ключей для пользователя {user_id}. "
                "Локально удалены только успешно отозванные ключи; остальные сохранены для повторной попытки.",
                "warning",
            )
            if failed_keys:
                logger.warning(
                    "User %s revoke aborted for keys still present on remote side: %s",
                    user_id,
                    ", ".join(failed_keys),
                )

        return redirect(url_for("users_page"))

    @flask_app.route("/users/issue-key/<int:user_id>", methods=["POST"])
    @login_required
    def issue_key_route(user_id):
        try:
            plan_id = request.form.get("plan_id")
            if not plan_id:
                flash("Ошибка: не выбран тариф.", "danger")
                return redirect(url_for("users_page"))

            plan = get_plan_by_id(int(plan_id))
            if not plan:
                flash("Ошибка: Тариф не найден.", "danger")
                return redirect(url_for("users_page"))
            if plan.get("service_type", "xui") == "xui" and plan.get("host_name") != "ALL":
                flash(
                    "VPN можно выдать только как единую глобальную подписку.",
                    "warning",
                )
                return redirect(url_for("users_page"))

            user = get_user(user_id)
            if not user:
                flash("Ошибка: Пользователь не найден.", "danger")
                return redirect(url_for("users_page"))

            month_qty = plan["months"]
            days_to_add = month_qty * 30
            target_expiry_dt = time_utils.get_msk_now() + timedelta(days=days_to_add)
            target_expiry_ms = time_utils.get_timestamp_ms(target_expiry_dt)

            user_keys = get_user_keys(user_id)
            key_number = None  # Will be fetched only if a NEW key is actually needed

            def _find_existing_manual_issue_key(host_name: str) -> dict | None:
                paid_match = None
                trial_match = None
                for key in user_keys:
                    if key.get("service_type", "xui") != "xui":
                        continue
                    if key.get("host_name") != host_name:
                        continue
                    if int(key.get("plan_id", 0) or 0) > 0:
                        paid_match = key
                        break
                    if trial_match is None:
                        trial_match = key
                return paid_match or trial_match

            issued_count = 0
            primary_key_id = None

            if plan["host_name"] == "ALL":
                # Global Plan
                hosts = get_all_hosts(only_enabled=True)

                # We need a key number for any NEW keys we might create
                # To be consistent with existing logic, we fetch it once
                key_number = get_next_key_number(user_id)

                for h in hosts:
                    try:
                        existing_key_db = _find_existing_manual_issue_key(
                            h["host_name"]
                        )

                        if existing_key_db:
                            # Manual issuance should set the exact plan duration from now,
                            # not extend the user's remaining time. Reuse trial keys too,
                            # otherwise admin issuance creates duplicate clients on the panel.
                            result = _run_async(
                                xui_api.create_or_update_key_on_host_absolute_expiry(
                                    host_name=h["host_name"],
                                    email=existing_key_db["key_email"],
                                    target_expiry_ms=target_expiry_ms,
                                    telegram_id=str(user_id),
                                    preserve_longer_expiry=False,
                                )
                            )
                            if result:
                                expiry_dt = time_utils.from_timestamp_ms(
                                    result["expiry_timestamp_ms"]
                                )
                                updated = update_key_info(
                                    existing_key_db["key_id"],
                                    expiry_dt,
                                    result["connection_string"],
                                )
                                updated = updated and update_key_plan_id(
                                    existing_key_db["key_id"], int(plan["plan_id"])
                                )
                                if not updated:
                                    logger.error(
                                        "Manual global issue updated host %s but DB update failed for key_id=%s.",
                                        h["host_name"],
                                        existing_key_db["key_id"],
                                    )
                                    continue
                                issued_count += 1
                        else:
                            # Create new key
                            email = f"user{user_id}-global-{h['host_name'].replace(' ', '').lower()}"
                            result = _run_async(
                                xui_api.create_or_update_key_on_host_absolute_expiry(
                                    host_name=h["host_name"],
                                    email=email,
                                    target_expiry_ms=target_expiry_ms,
                                    telegram_id=str(user_id),
                                    preserve_longer_expiry=False,
                                )
                            )
                            if result:
                                new_key_id = add_new_key(
                                    user_id=user_id,
                                    host_name=h["host_name"],
                                    xui_client_uuid=result["client_uuid"],
                                    key_email=email,
                                    expiry_timestamp_ms=result["expiry_timestamp_ms"],
                                    connection_string=result["connection_string"],
                                    plan_id=plan["plan_id"],
                                )
                                if new_key_id is None:
                                    logger.error(
                                        "Manual global issue created host %s but DB persistence failed.",
                                        h["host_name"],
                                    )
                                    continue
                                issued_count += 1
                    except Exception as e_h:
                        logger.error(
                            f"Failed to issue manual key on host {h['host_name']}: {e_h}"
                        )
                if issued_count == 0:
                    flash(
                        "Не удалось создать/обновить ни один ключ на серверах XUI.",
                        "danger",
                    )
                    return redirect(url_for("users_page"))
                if issued_count != len(hosts):
                    logger.error(
                        "Manual global issue incomplete for user %s: %s/%s hosts.",
                        user_id,
                        issued_count,
                        len(hosts),
                    )
                    flash(
                        f"Подписка выдана не полностью: {issued_count} из {len(hosts)} серверов. "
                        "Статистика не начислена; проверьте недоступные хосты и повторите операцию.",
                        "warning",
                    )
                    return redirect(url_for("users_page"))

                msg = f"Глобальная подписка успешно выдана! ({issued_count} ключей обработано)"

            else:
                # Single host
                try:
                    host_name = plan["host_name"]

                    existing_key_db = _find_existing_manual_issue_key(host_name)

                    if existing_key_db:
                        # Manual issuance should set the exact plan duration from now,
                        # not extend the user's remaining time. Reuse trial keys too,
                        # otherwise admin issuance creates duplicate clients on the panel.
                        result = _run_async(
                            xui_api.create_or_update_key_on_host_absolute_expiry(
                                host_name=host_name,
                                email=existing_key_db["key_email"],
                                target_expiry_ms=target_expiry_ms,
                                telegram_id=str(user_id),
                                preserve_longer_expiry=False,
                            )
                        )
                        if result:
                            expiry_dt = time_utils.from_timestamp_ms(
                                result["expiry_timestamp_ms"]
                            )
                            updated = update_key_info(
                                existing_key_db["key_id"],
                                expiry_dt,
                                result["connection_string"],
                            )
                            updated = updated and update_key_plan_id(
                                existing_key_db["key_id"], int(plan["plan_id"])
                            )
                            if not updated:
                                logger.error(
                                    "Manual issue updated host %s but DB update failed for key_id=%s.",
                                    host_name,
                                    existing_key_db["key_id"],
                                )
                                result = None
                            if not result:
                                issued_count = 0
                            else:
                                primary_key_id = existing_key_db["key_id"]
                                issued_count += 1
                    else:
                        # Create new
                        key_number = get_next_key_number(user_id)
                        email = f"user{user_id}-key{key_number}-{host_name.replace(' ', '').lower()}"

                        result = _run_async(
                            xui_api.create_or_update_key_on_host_absolute_expiry(
                                host_name=host_name,
                                email=email,
                                target_expiry_ms=target_expiry_ms,
                                telegram_id=str(user_id),
                                preserve_longer_expiry=False,
                            )
                        )

                        if result:
                            new_key_id = add_new_key(
                                user_id=user_id,
                                host_name=host_name,
                                xui_client_uuid=result["client_uuid"],
                                key_email=email,
                                expiry_timestamp_ms=result["expiry_timestamp_ms"],
                                connection_string=result["connection_string"],
                                plan_id=plan["plan_id"],
                            )
                            if new_key_id is not None:
                                primary_key_id = new_key_id
                                issued_count += 1

                    if issued_count > 0:
                        msg = f"Подписка на сервер {host_name} успешно выдана!"
                    else:
                        flash(
                            "Не удалось создать/обновить ключ на сервере XUI.", "danger"
                        )
                        return redirect(url_for("users_page"))

                except Exception as e_s:
                    logger.error(f"Failed to issue manual key: {e_s}")
                    flash(f"Ошибка при выдаче: {e_s}", "danger")
                    return redirect(url_for("users_page"))

            # Update user stats
            update_user_stats(user_id, 0, month_qty)

            # Notify User
            bot = _bot_controller.get_bot_instance()
            if bot:
                loop = current_app.config.get("EVENT_LOOP")
                verdict_text = f"Администратор выдал вам подписку: <b>{plan['plan_name']}</b>\nСрок: {month_qty} мес."
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        bot.send_message(
                            user_id,
                            f"🎁 <b>Вам выдана подписка!</b>\n\n{verdict_text}",
                            parse_mode="HTML",
                        ),
                        loop,
                    )

            flash(msg, "success")

        except Exception as e:
            logger.error(f"Error issuing key manually: {e}", exc_info=True)
            flash(f"Ошибка при выдаче подписки: {e}", "danger")

        return redirect(url_for("users_page"))

    @flask_app.route("/users/delete/<int:user_id>", methods=["POST"])
    @login_required
    def delete_user_route(user_id):
        keys_to_revoke = get_user_keys(user_id)
        success_count = 0
        deleted_key_ids: list[int] = []
        failed_keys: list[str] = []

        for key in keys_to_revoke:
            try:
                result = _delete_remote_user_key(key)
                if result:
                    success_count += 1
                    if key.get("key_id") is not None:
                        deleted_key_ids.append(int(key["key_id"]))
                else:
                    failed_keys.append(
                        key.get("key_email") or f"key:{key.get('key_id')}"
                    )
            except Exception as e:
                logger.error(
                    f"Failed to delete key '{key.get('key_email')}' for user {user_id}: {e}",
                    exc_info=True,
                )
                failed_keys.append(key.get("key_email") or f"key:{key.get('key_id')}")

        if failed_keys:
            if deleted_key_ids:
                delete_keys_by_ids(deleted_key_ids)
            logger.warning(
                "User %s deletion cancelled because some remote keys remain: %s",
                user_id,
                ", ".join(failed_keys),
            )
            flash(
                f"Удаление пользователя {user_id} остановлено: удалось удалить {success_count} из {len(keys_to_revoke)} ключей. "
                "Пользователь сохранён в БД, а локально удалены только уже удалённые на сервере ключи.",
                "warning",
            )
            return redirect(url_for("users_page"))

        deleted = delete_user_everywhere(user_id)
        if not deleted:
            flash(
                f"Ключи пользователя {user_id} удалены на панелях, но удаление из локальной базы завершилось ошибкой. Проверьте логи.",
                "danger",
            )
            return redirect(url_for("users_page"))

        if success_count == len(keys_to_revoke):
            flash(
                f"Пользователь {user_id} и все его данные были удалены. Ключей отозвано: {success_count}.",
                "success",
            )
        else:
            flash(
                f"Пользователь {user_id} удален из базы, но удалось отозвать {success_count} из {len(keys_to_revoke)} ключей. Проверьте логи.",
                "warning",
            )

        return redirect(url_for("users_page"))

    @flask_app.route("/add-host", methods=["POST"])
    @login_required
    def add_host_route():
        host_name = request.form["host_name"].strip()
        host_url = request.form["host_url"].strip()
        host_username = request.form["host_username"].strip()
        host_pass = request.form["host_pass"]
        api_token = request.form.get("api_token", "").strip()
        url_ok, url_error = _validate_panel_url(host_url)
        if not url_ok:
            flash(f"Хост '{host_name}' не добавлен: {url_error}.", "warning")
            return redirect(url_for("settings_page"))

        try:
            inbound = int(request.form["host_inbound_id"].strip())
        except ValueError:
            flash("ID входящего подключения должен быть целым числом.", "warning")
            return redirect(url_for("settings_page"))

        preflight_ok, preflight_message = xui_api.validate_host_write_access(
            host_url=host_url,
            username=host_username,
            password=host_pass,
            inbound_id=inbound,
            api_token=api_token,
        )
        if not preflight_ok:
            flash(
                f"Хост '{host_name}' не добавлен: {preflight_message}.",
                "danger",
            )
            return redirect(url_for("settings_page"))

        success = create_host(
            name=host_name,
            url=host_url,
            user=host_username,
            passwd=host_pass,
            inbound=inbound,
            api_token=api_token,
        )

        if not success:
            flash(
                f"Не удалось добавить хост '{host_name}': хост с таким именем или идентичными параметрами уже существует.",
                "warning",
            )
            return redirect(url_for("settings_page"))

        # Auto-provision keys for all global subscription users immediately
        _run_auto_provision_for_global_users(host_name)

        flash(
            f"Хост '{host_name}' успешно добавлен. Автопровижининг глобальных ключей запущен.",
            "success",
        )
        return redirect(url_for("settings_page"))

    @flask_app.route("/edit-host/<host_name>", methods=["GET"])
    @login_required
    def edit_host_page(host_name):
        target_host = get_host(host_name)
        if not target_host:
            flash(f"Хост '{host_name}' не найден.", "warning")
            return redirect(url_for("settings_page"))

        common_data = get_common_template_data()
        return render_template(
            "settings.html",
            edit_host=target_host,
            **_load_settings_page_context(),
            **common_data,
        )

    @flask_app.route("/settings/backup", methods=["POST"])
    @login_required
    def backup_route():
        include_env = request.form.get("include_env") == "true"
        try:
            zip_path, temp_dir = _create_backup_zip(include_env=include_env)
        except Exception as e:
            logger.error(f"Failed to create backup: {e}", exc_info=True)
            flash("Не удалось создать бэкап. Проверьте логи.", "danger")
            return redirect(url_for("settings_page"))

        @after_this_request
        def cleanup(response):
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as cleanup_error:
                logger.debug(
                    f"Backup temp cleanup failed for '{temp_dir}': {cleanup_error}"
                )
            return response

        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=zip_path.name,
        )

    @flask_app.route("/settings/import", methods=["POST"])
    @login_required
    def import_route():
        if not request.files.get("backup_file"):
            flash("Файл бэкапа не выбран.", "warning")
            return redirect(url_for("settings_page"))

        backup_file = request.files["backup_file"]
        apply_env = request.form.get("apply_env") == "true"

        try:
            restore_result = _restore_from_backup(backup_file, apply_env=apply_env)
            flash("Бэкап успешно импортирован. Текущая база заменена.", "success")
            for message in restore_result.get("restart_errors", []):
                flash(
                    f"Боты после импорта не были перезапущены автоматически: {message}",
                    "warning",
                )
        except ValueError as e:
            flash(str(e), "warning")
        except Exception as e:
            logger.error(f"Failed to restore from backup: {e}", exc_info=True)
            flash("Ошибка при импорте бэкапа. Проверьте логи.", "danger")

        return redirect(url_for("settings_page"))

    @flask_app.route("/update-host", methods=["POST"])
    @login_required
    def update_host_route():
        old_host_name = request.form.get("old_host_name", "").strip()
        new_host_name = request.form.get("host_name", "").strip()
        host_url = request.form.get("host_url", "").strip()
        host_username = request.form.get("host_username", "").strip()
        host_pass = request.form.get("host_pass", "")
        api_token = request.form.get("api_token", "").strip()
        inbound_raw = request.form.get("host_inbound_id", "").strip()

        if (
            not old_host_name
            or not new_host_name
            or not host_url
            or not host_username
            or not inbound_raw
        ):
            flash("Не все обязательные поля хоста заполнены.", "warning")
            return redirect(
                url_for("edit_host_page", host_name=old_host_name or new_host_name)
            )
        url_ok, url_error = _validate_panel_url(host_url)
        if not url_ok:
            flash(f"Хост '{new_host_name}' не обновлен: {url_error}.", "warning")
            return redirect(url_for("edit_host_page", host_name=old_host_name))

        try:
            inbound = int(inbound_raw)
        except ValueError:
            flash("ID входящего подключения должен быть целым числом.", "warning")
            return redirect(url_for("edit_host_page", host_name=old_host_name))

        current_host = get_host(old_host_name)
        preflight_password = host_pass or (
            current_host.get("host_pass") if current_host else ""
        )
        api_token_to_store = api_token or (
            current_host.get("api_token") if current_host else ""
        )
        preflight_ok, preflight_message = xui_api.validate_host_write_access(
            host_url=host_url,
            username=host_username,
            password=preflight_password,
            inbound_id=inbound,
            api_token=api_token_to_store,
        )
        if not preflight_ok:
            flash(
                f"Хост '{new_host_name}' не обновлен: {preflight_message}.",
                "danger",
            )
            return redirect(url_for("edit_host_page", host_name=old_host_name))

        success = update_host(
            old_name=old_host_name,
            new_name=new_host_name,
            url=host_url,
            user=host_username,
            passwd=host_pass,
            inbound=inbound,
            api_token=api_token_to_store,
        )

        if not success:
            flash(
                "Не удалось обновить хост. Проверьте имя хоста, URL и логи приложения.",
                "danger",
            )
            return redirect(url_for("edit_host_page", host_name=old_host_name))

        # Auto-provision keys for all global subscription users if host name changed or enabled
        _run_auto_provision_for_global_users(new_host_name)

        flash(
            f"Хост '{old_host_name}' успешно обновлен. Автопровижининг глобальных ключей запущен.",
            "success",
        )
        return redirect(url_for("settings_page"))

    @flask_app.route("/toggle-host/<host_name>", methods=["POST"])
    @login_required
    def toggle_host_route(host_name):
        host = get_host(host_name)
        if host:
            new_status = not bool(host["is_enabled"])
            toggle_host_status(host_name, new_status)

            # If enabling host, auto-provision keys for users missing this host
            if new_status:
                _run_auto_provision_for_global_users(host_name)
            flash(
                f"Хост '{host_name}' {'включен' if new_status else 'отключен'}.",
                "success",
            )
        else:
            flash(f"Хост '{host_name}' не найден.", "warning")
        return redirect(url_for("settings_page"))

    @flask_app.route("/delete-host/<host_name>", methods=["POST"])
    @login_required
    def delete_host_route(host_name):
        if not delete_host(host_name):
            flash(
                "Хост не удалось удалить из локальной базы. Проверьте логи.",
                "danger",
            )
            return redirect(url_for("settings_page"))
        flash(
            f"Хост '{host_name}', его тарифы, ключи и связанные локальные записи удалены. "
            "Доступность 3x-ui панели при удалении не проверялась.",
            "success",
        )
        return redirect(url_for("settings_page"))

    @flask_app.route("/add-plan", methods=["POST"])
    @login_required
    def add_plan_route():
        service_type = request.form.get("service_type", "xui")
        host_name = request.form["host_name"]
        if service_type == "xui" and host_name != "ALL":
            flash(
                "VPN продается только как единая подписка. Добавляйте XUI-тарифы в блоке глобальных тарифов.",
                "warning",
            )
            return redirect(url_for("settings_page"))

        create_plan(
            host_name=host_name,
            plan_name=request.form["plan_name"],
            months=int(request.form["months"]),
            price=float(request.form["price"]),
            service_type=service_type,
        )
        flash(f"Новый тариф для хоста '{host_name}' добавлен.", "success")
        return redirect(url_for("settings_page"))

    @flask_app.route("/delete-plan/<int:plan_id>", methods=["POST"])
    @login_required
    def delete_plan_route(plan_id):
        delete_plan(plan_id)
        flash("Тариф успешно удален.", "success")
        return redirect(url_for("settings_page"))

    @flask_app.route("/promo-codes/toggle-feature", methods=["POST"])
    @login_required
    def toggle_promo_codes_feature_route():
        enabled = request.form.get("enable_promo_codes") == "true"
        update_setting("enable_promo_codes", "true" if enabled else "false")
        flash(
            "Промокоды включены." if enabled else "Промокоды отключены.",
            "success",
        )
        return redirect(url_for("settings_page") + "#promo-codes")

    def _promo_expires_at_from_form() -> str | None:
        raw_date = (request.form.get("expires_on") or "").strip()
        if not raw_date:
            return None
        expiry_date = datetime.strptime(raw_date, "%Y-%m-%d")
        expiry_dt = time_utils.MSK_TZ.localize(
            expiry_date.replace(hour=23, minute=59, second=59)
        )
        return expiry_dt.isoformat()

    @flask_app.route("/promo-codes/add", methods=["POST"])
    @login_required
    def add_promo_code_route():
        try:
            duration_days = int(request.form.get("duration_days", "0"))
            max_uses = int(request.form.get("max_uses", "0"))
            expires_at = _promo_expires_at_from_form()
        except ValueError:
            flash(
                "Дни подписки, лимит применений и дата действия должны быть корректными.",
                "warning",
            )
            return redirect(url_for("settings_page") + "#promo-codes")

        success, message = create_promo_code(
            code=request.form.get("code", ""),
            duration_days=duration_days,
            max_uses=max_uses,
            expires_at=expires_at,
        )
        flash(message, "success" if success else "warning")
        return redirect(url_for("settings_page") + "#promo-codes")

    @flask_app.route("/promo-codes/update/<int:promo_id>", methods=["POST"])
    @login_required
    def update_promo_code_route(promo_id):
        try:
            duration_days = int(request.form.get("duration_days", "0"))
            max_uses = int(request.form.get("max_uses", "0"))
            expires_at = _promo_expires_at_from_form()
        except ValueError:
            flash(
                "Дни подписки, лимит применений и дата действия должны быть корректными.",
                "warning",
            )
            return redirect(url_for("settings_page") + "#promo-codes")

        success, message = update_promo_code(
            promo_id=promo_id,
            code=request.form.get("code", ""),
            duration_days=duration_days,
            max_uses=max_uses,
            expires_at=expires_at,
            is_active=request.form.get("is_active") == "true",
        )
        flash(message, "success" if success else "warning")
        return redirect(url_for("settings_page") + "#promo-codes")

    @flask_app.route("/promo-codes/toggle/<int:promo_id>", methods=["POST"])
    @login_required
    def toggle_promo_code_route(promo_id):
        is_active = request.form.get("is_active") == "true"
        if set_promo_code_active(promo_id, is_active):
            flash("Промокод включён." if is_active else "Промокод отключён.", "success")
        else:
            flash("Не удалось изменить статус промокода.", "danger")
        return redirect(url_for("settings_page") + "#promo-codes")

    @flask_app.route("/promo-codes/delete/<int:promo_id>", methods=["POST"])
    @login_required
    def delete_promo_code_route(promo_id):
        if delete_promo_code(promo_id):
            flash("Промокод удалён.", "success")
        else:
            flash("Не удалось удалить промокод.", "danger")
        return redirect(url_for("settings_page") + "#promo-codes")

    # ── MTG Proxy host routes ─────────────────────────────────────────────────

    @flask_app.route("/add-mtg-host", methods=["POST"])
    @login_required
    def add_mtg_host_route():
        host_name = request.form["host_name"]
        host_url = request.form["host_url"]
        url_ok, url_error = _validate_panel_url(host_url)
        if not url_ok:
            flash(f"MTG-хост '{host_name}' не добавлен: {url_error}.", "warning")
            return redirect(url_for("settings_page"))
        success = create_mtg_host(
            name=host_name,
            url=host_url,
            user=request.form["host_username"],
            passwd=request.form["host_pass"],
        )
        if not success:
            flash(
                f"Не удалось добавить MTG-хост '{host_name}': хост с таким именем уже существует.",
                "warning",
            )
        else:
            flash(f"MTG-хост '{host_name}' успешно добавлен.", "success")
        return redirect(url_for("settings_page"))

    @flask_app.route("/edit-mtg-host/<host_name>", methods=["GET"])
    @login_required
    def edit_mtg_host_page(host_name):
        edit_mtg_host = get_mtg_host(host_name)
        if not edit_mtg_host:
            flash(f"MTG-хост '{host_name}' не найден.", "warning")
            return redirect(url_for("settings_page"))
        common_data = get_common_template_data()
        return render_template(
            "settings.html",
            edit_mtg_host=edit_mtg_host,
            **_load_settings_page_context(),
            **common_data,
        )

    @flask_app.route("/update-mtg-host", methods=["POST"])
    @login_required
    def update_mtg_host_route():
        old_host_name = request.form["old_host_name"]
        new_host_name = request.form["host_name"]
        host_url = request.form["host_url"]
        url_ok, url_error = _validate_panel_url(host_url)
        if not url_ok:
            flash(f"MTG-хост '{new_host_name}' не обновлён: {url_error}.", "warning")
            return redirect(url_for("settings_page"))
        success = update_mtg_host(
            old_name=old_host_name,
            new_name=new_host_name,
            url=host_url,
            user=request.form["host_username"],
            passwd=request.form.get("host_pass", ""),
        )
        if success:
            flash(f"MTG-хост '{old_host_name}' успешно обновлён.", "success")
        else:
            flash(
                "Не удалось обновить MTG-хост. Проверьте имя, уникальность и логи приложения.",
                "danger",
            )
        return redirect(url_for("settings_page"))

    @flask_app.route("/toggle-mtg-host/<host_name>", methods=["POST"])
    @login_required
    def toggle_mtg_host_route(host_name):
        host = get_mtg_host(host_name)
        if host:
            new_status = not bool(host["is_enabled"])
            toggle_mtg_host_status(host_name, new_status)
            flash(
                f"MTG-хост '{host_name}' {'включён' if new_status else 'отключён'}.",
                "success",
            )
        else:
            flash(f"MTG-хост '{host_name}' не найден.", "warning")
        return redirect(url_for("settings_page"))

    @flask_app.route("/delete-mtg-host/<host_name>", methods=["POST"])
    @login_required
    def delete_mtg_host_route(host_name):
        if not delete_mtg_host(host_name):
            flash(
                "MTG-хост не удалось удалить из локальной базы. Проверьте логи.",
                "danger",
            )
            return redirect(url_for("settings_page"))
        flash(
            f"MTG-хост '{host_name}', его тарифы, ключи и связанные локальные записи удалены. "
            "Доступность MTG панели при удалении не проверялась.",
            "success",
        )
        return redirect(url_for("settings_page"))

    # ── Payment method rules ──────────────────────────────────────────────────

    @flask_app.route("/payment-rules/set", methods=["POST"])
    @login_required
    def set_payment_rule_route():
        context_key = request.form.get("context_key", "").strip()
        method = request.form.get("method", "").strip()
        is_enabled = request.form.get("is_enabled", "0") == "1"
        if context_key and method in ALL_PAYMENT_METHODS:
            set_payment_rule(context_key, method, is_enabled)
            logger.info(f"Payment rule set: {context_key} / {method} = {is_enabled}")
        else:
            logger.warning(
                f"Payment rule set IGNORED: context={context_key!r} method={method!r}"
            )
        return redirect(request.referrer or url_for("settings_page"))

    @flask_app.route("/payment-rules/reset", methods=["POST"])
    @login_required
    def reset_payment_rules_route():
        context_key = request.form.get("context_key", "").strip()
        if context_key:
            delete_payment_rules_for_context(context_key)
            flash(
                f"Правила оплаты для «{context_key}» сброшены до глобальных.", "success"
            )
        return redirect(request.referrer or url_for("settings_page"))

    # ─────────────────────────────────────────────────────────────────────────

    @flask_app.route("/yookassa-webhook", methods=["POST"])
    def yookassa_webhook_handler():
        reserved_payment_id: str | None = None
        try:
            shop_id = get_setting("yookassa_shop_id")
            secret_key = get_setting("yookassa_secret_key")

            if not shop_id or not secret_key:
                logger.error(
                    "YooKassa Webhook: Shop ID or Secret Key not configured. Rejecting request."
                )
                return "Forbidden", 403

            event_json = request.get_json(silent=True)
            if not isinstance(event_json, dict):
                logger.warning("YooKassa Webhook: Invalid JSON payload.")
                return "Bad Request", 400
            if event_json.get("event") == "payment.succeeded":
                obj = event_json.get("object", {})
                payment_id = obj.get("id")
                if not payment_id:
                    logger.warning(
                        "YooKassa webhook: Missing payment id in succeeded event."
                    )
                    return "Bad Request", 400

                if _is_webhook_processed("yookassa", payment_id):
                    return "OK", 200

                Configuration.account_id = shop_id
                Configuration.secret_key = secret_key

                try:
                    payment = Payment.find_one(payment_id)
                    if not payment or getattr(payment, "status", None) != "succeeded":
                        logger.warning(
                            f"YooKassa webhook: Payment {payment_id} is not succeeded according to API."
                        )
                        return "OK", 200
                except Exception as e:
                    logger.error(
                        f"YooKassa webhook: API verification failed for payment {payment_id}: {e}"
                    )
                    return "Error", 500

                # Use metadata from API-verified payment object, not from webhook body
                metadata = {}
                if hasattr(payment, "metadata") and payment.metadata:
                    metadata = dict(payment.metadata)
                if not metadata:
                    logger.error(
                        f"YooKassa webhook: Payment {payment_id} has no metadata in API response."
                    )
                    return "Service Unavailable", 503

                # Cross-check paid amount against metadata price
                api_amount = getattr(getattr(payment, "amount", None), "value", None)
                api_currency = getattr(
                    getattr(payment, "amount", None), "currency", None
                )
                if str(api_currency or "").upper() != "RUB":
                    logger.error(
                        f"YooKassa webhook: Unexpected currency for {payment_id}: {api_currency}"
                    )
                    return "Service Unavailable", 503
                meta_price = metadata.get("price")
                if api_amount and meta_price is not None:
                    try:
                        if abs(float(api_amount) - float(meta_price)) > 0.01:
                            logger.error(
                                f"YooKassa webhook: Amount mismatch for {payment_id}: "
                                f"API amount={api_amount}, metadata price={meta_price}"
                            )
                            return "Service Unavailable", 503
                    except (ValueError, TypeError):
                        pass

                metadata["provider_payment_id"] = payment_id
                metadata["payment_method"] = "YooKassa"

                reserved_metadata = reserve_pending_transaction(
                    payment_id,
                    metadata=metadata,
                    payment_method="YooKassa",
                    amount_currency=(
                        float(api_amount) if api_amount is not None else None
                    ),
                    currency_name=api_currency,
                )
                if reserved_metadata is None:
                    if _get_transaction_status(payment_id) == "paid":
                        _set_webhook_processed("yookassa", payment_id)
                        logger.info(
                            "YooKassa webhook: payment %s is already paid locally; marked webhook as processed.",
                            payment_id,
                        )
                        return "OK", 200
                    logger.warning(
                        "YooKassa webhook: payment %s is missing, already reserved, or no longer pending.",
                        payment_id,
                    )
                    return "Service Unavailable", 503
                metadata = reserved_metadata
                reserved_payment_id = payment_id

                bot = _bot_controller.get_bot_instance()
                payment_processor = handlers.process_successful_payment

                if metadata and bot is not None and payment_processor is not None:
                    loop = current_app.config.get("EVENT_LOOP")
                    if loop and loop.is_running():
                        processed_ok = _run_async(
                            payment_processor(bot, metadata), timeout=180
                        )
                        finalized = finalize_reserved_transaction(
                            payment_id,
                            success=bool(processed_ok),
                            metadata=metadata,
                            payment_method="YooKassa",
                            amount_currency=(
                                float(api_amount) if api_amount is not None else None
                            ),
                            currency_name=api_currency,
                        )
                        reserved_payment_id = None
                        if not finalized:
                            logger.error(
                                "YooKassa webhook: failed to finalize reserved transaction %s after processing=%s",
                                payment_id,
                                processed_ok,
                            )
                            return "Service Unavailable", 503
                        if processed_ok:
                            _set_webhook_processed("yookassa", payment_id)
                        else:
                            logger.warning(
                                f"YooKassa webhook: Payment {payment_id} was not fulfilled successfully. "
                                "Leaving webhook unmarked for retry."
                            )
                            return "Service Unavailable", 503
                    else:
                        if reserved_payment_id:
                            finalize_reserved_transaction(
                                reserved_payment_id,
                                success=False,
                                metadata=metadata,
                                payment_method="YooKassa",
                                amount_currency=(
                                    float(api_amount)
                                    if api_amount is not None
                                    else None
                                ),
                                currency_name=api_currency,
                            )
                            reserved_payment_id = None
                        logger.error(
                            "YooKassa webhook: Event loop is not available! Will retry."
                        )
                        return "Service Unavailable", 503
            return "OK", 200
        except Exception as e:
            if reserved_payment_id:
                finalize_reserved_transaction(
                    reserved_payment_id, success=False, payment_method="YooKassa"
                )
            logger.error(f"Error in yookassa webhook handler: {e}", exc_info=True)
            return "Error", 500

    def _cryptobot_webhook_handler_impl(path_secret_valid: bool = False):
        reserved_payment_id: str | None = None
        try:
            signature_valid = _is_valid_cryptobot_signature()

            configured_secret = get_setting("cryptobot_webhook_secret")
            request_secret = _extract_cryptobot_secret_from_request()
            legacy_secret_valid = (
                not get_setting("cryptobot_token")
                and bool(configured_secret)
                and bool(request_secret)
                and compare_digest(str(request_secret), str(configured_secret))
            )

            if not signature_valid and not legacy_secret_valid and not path_secret_valid:
                logger.warning(
                    "CryptoBot Webhook: missing valid signature and legacy secret check failed."
                )
                return "Forbidden", 403

            request_data = request.get_json(silent=True)
            if not isinstance(request_data, dict):
                logger.warning("CryptoBot Webhook: Invalid JSON payload.")
                return "Bad Request", 400

            if request_data and request_data.get("update_type") == "invoice_paid":
                payload_data = request_data.get("payload", {})
                if not isinstance(payload_data, dict):
                    logger.warning("CryptoBot Webhook: Payload is not an object.")
                    return "Bad Request", 400

                invoice_status = payload_data.get("status")
                if invoice_status and invoice_status != "paid":
                    logger.warning(
                        f"CryptoBot Webhook: invoice_paid update but status={invoice_status}. Ignoring."
                    )
                    return "OK", 200

                external_invoice_id = payload_data.get("invoice_id")
                if external_invoice_id and _is_webhook_processed(
                    "cryptobot", str(external_invoice_id)
                ):
                    return "OK", 200

                payload_string = payload_data.get("payload")

                if not payload_string:
                    logger.warning(
                        "CryptoBot Webhook: Received paid invoice but payload was empty."
                    )
                    return "OK", 200

                payment_id = None
                try:
                    payload_obj = json.loads(payload_string)
                    if isinstance(payload_obj, dict):
                        payment_id = str(payload_obj.get("tx_id") or "").strip()
                except json.JSONDecodeError:
                    payment_id = None

                external_id_fallback = None
                if not external_invoice_id:
                    external_id_fallback = hashlib.sha256(
                        payload_string.encode("utf-8")
                    ).hexdigest()
                    if _is_webhook_processed("cryptobot", external_id_fallback):
                        return "OK", 200

                metadata = None
                payload_price = None
                cb_amount = payload_data.get("amount")
                currency_name = payload_data.get("asset") or payload_data.get(
                    "currency"
                )
                currency_type = payload_data.get("currency_type")
                fiat_name = payload_data.get("fiat")
                normalized_currency = str(currency_name or fiat_name or "").upper()
                if normalized_currency != "RUB" or (
                    currency_type and str(currency_type).lower() != "fiat"
                ):
                    logger.error(
                        "CryptoBot webhook: Unexpected currency fields: currency=%s fiat=%s currency_type=%s",
                        currency_name,
                        fiat_name,
                        currency_type,
                    )
                    return "Bad Request", 400

                if payment_id:
                    metadata = _reserve_pending_transaction_for_cryptobot(
                        payment_id,
                        amount_currency=cb_amount,
                        currency_name=currency_name,
                    )
                    if metadata is None:
                        local_status = _get_transaction_status(payment_id)
                        if local_status == "paid":
                            if external_invoice_id:
                                _set_webhook_processed(
                                    "cryptobot", str(external_invoice_id)
                                )
                            elif external_id_fallback:
                                _set_webhook_processed(
                                    "cryptobot", external_id_fallback
                                )
                            logger.info(
                                "CryptoBot webhook: transaction %s is already paid locally; marked webhook as processed.",
                                payment_id,
                            )
                            return "OK", 200
                        if local_status == "processing":
                            logger.info(
                                "CryptoBot webhook: transaction %s is already being processed locally.",
                                payment_id,
                            )
                            return "OK", 200
                        logger.warning(
                            "CryptoBot webhook: paid invoice %s has no reservable local pending transaction (status=%s). Requesting retry.",
                            payment_id,
                            local_status,
                        )
                        return "Service Unavailable", 503
                    payload_price = metadata.get("price")
                    metadata["payment_method"] = "CryptoBot"
                    metadata["provider_payment_id"] = payment_id
                    metadata["cryptobot_invoice_id"] = str(
                        external_invoice_id or external_id_fallback or ""
                    )
                    reserved_payment_id = payment_id
                else:
                    logger.error(
                        "CryptoBot webhook: paid invoice payload has no known pending transaction id."
                    )
                    return "Bad Request", 400

                # Cross-check actual paid amount against payload price
                if cb_amount:
                    try:
                        if abs(float(cb_amount) - float(payload_price)) > 1.0:
                            if reserved_payment_id:
                                finalize_reserved_transaction(
                                    reserved_payment_id,
                                    success=False,
                                    metadata=metadata,
                                    payment_method="CryptoBot",
                                    amount_currency=cb_amount,
                                    currency_name=currency_name,
                                )
                                reserved_payment_id = None
                            logger.error(
                                f"CryptoBot webhook: Amount mismatch! "
                                f"paid={cb_amount}, payload_price={payload_price}"
                            )
                            return "Bad Request", 400
                    except (ValueError, TypeError):
                        logger.warning(
                            f"CryptoBot webhook: Could not compare amounts: {cb_amount} vs {payload_price}"
                        )

                bot = _bot_controller.get_bot_instance()
                loop = current_app.config.get("EVENT_LOOP")
                payment_processor = handlers.process_successful_payment

                if bot and loop and loop.is_running():
                    processed_ok = _run_async(
                        payment_processor(bot, metadata), timeout=180
                    )
                    if reserved_payment_id:
                        finalized = finalize_reserved_transaction(
                            reserved_payment_id,
                            success=bool(processed_ok),
                            metadata=metadata,
                            payment_method="CryptoBot",
                            amount_currency=cb_amount,
                            currency_name=currency_name,
                        )
                        reserved_payment_id = None
                        if not finalized:
                            logger.error(
                                "CryptoBot webhook: failed to finalize reserved transaction %s after processing=%s",
                                payment_id,
                                processed_ok,
                            )
                    if processed_ok:
                        if external_invoice_id:
                            _set_webhook_processed(
                                "cryptobot", str(external_invoice_id)
                            )
                        elif external_id_fallback:
                            _set_webhook_processed("cryptobot", external_id_fallback)
                    else:
                        logger.warning(
                            "CryptoBot webhook: payment was not fulfilled successfully. "
                            "Leaving webhook unmarked for retry."
                        )
                        return "Service Unavailable", 503
                else:
                    if reserved_payment_id:
                        finalize_reserved_transaction(
                            reserved_payment_id,
                            success=False,
                            metadata=metadata,
                            payment_method="CryptoBot",
                            amount_currency=cb_amount,
                            currency_name=currency_name,
                        )
                        reserved_payment_id = None
                    logger.error(
                        "cryptobot Webhook: Could not process payment because bot or event loop is not running. Will retry."
                    )
                    return "Service Unavailable", 503

            return "OK", 200

        except Exception as e:
            if reserved_payment_id:
                finalize_reserved_transaction(
                    reserved_payment_id, success=False, payment_method="CryptoBot"
                )
            logger.error(f"Error in cryptobot webhook handler: {e}", exc_info=True)
            return "Error", 500

    @flask_app.route("/cryptobot-webhook", methods=["POST"])
    def cryptobot_webhook_handler():
        if get_setting("cryptobot_token"):
            return _cryptobot_webhook_handler_impl()
        return "Forbidden", 403

    @flask_app.route("/cryptobot-webhook/<token>", methods=["POST"])
    def cryptobot_webhook_handler_with_token(token: str):
        configured_secret = get_setting("cryptobot_webhook_secret")
        if configured_secret and compare_digest(str(token), str(configured_secret)):
            if get_setting("cryptobot_token"):
                logger.warning(
                    "CryptoBot Webhook: path secret accepted only as route compatibility; HMAC signature is still required."
                )
                return _cryptobot_webhook_handler_impl(path_secret_valid=False)
            logger.warning(
                "CryptoBot Webhook: path-based secret is deprecated but temporarily accepted without HMAC because cryptobot_token is not configured."
            )
            return _cryptobot_webhook_handler_impl(path_secret_valid=True)
        return "Forbidden", 403

    @flask_app.route("/settings/toggle_global_plans", methods=["POST"])
    @login_required
    def toggle_global_plans_route():
        current_status = get_setting("enable_global_plans")
        # Default to 'true' if not set, so toggling makes it 'false'
        # Actually default is usually empty/none, so treat None as 'true' or 'false'?
        # Let's say default is enabled.
        if not current_status:
            current_status = "true"

        new_status = "false" if current_status == "true" else "true"
        update_setting("enable_global_plans", new_status)
        flash(
            f"Global plans {'enabled' if new_status == 'true' else 'disabled'}.",
            "success",
        )
        return redirect(url_for("settings_page"))

    return flask_app

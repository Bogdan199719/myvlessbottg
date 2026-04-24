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
from flask import (
    Flask,
    request,
    render_template,
    redirect,
    url_for,
    flash,
    session,
    current_app,
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

from shop_bot.modules import xui_api
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
    set_trial_used,
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
)

_bot_controller = None


def _build_subscription_link(domain: str | None, token: str | None) -> str | None:
    domain_value = (domain or "").strip()
    token_value = (token or "").strip()
    if not domain_value or not token_value:
        return None
    if not domain_value.startswith(("http://", "https://")):
        domain_value = f"https://{domain_value}"
    return f"{domain_value.rstrip('/')}/sub/{token_value}"


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

        if include_env:
            env_path = Path(".env")
            if env_path.exists():
                shutil.copy(env_path, temp_dir / ".env")

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
    for member in zip_ref.infolist():
        member_name = member.filename
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
                shutil.copyfile(env_src, Path(".env"))

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


def _sanitize_csv_cell(value) -> str:
    text = str(value or "")
    if text[:1] in {"=", "+", "-", "@"}:
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
]


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

    flask_app.register_blueprint(subscription_bp)

    secret_key = os.getenv("FLASK_SECRET_KEY")
    if not secret_key:
        secret_key = get_setting("flask_secret_key")
    if not secret_key:
        secret_key = os.urandom(32).hex()
        update_setting("flask_secret_key", secret_key)
    flask_app.config["SECRET_KEY"] = secret_key

    # Security Hardening
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

    flask_app.jinja_env.globals["csrf_token"] = generate_csrf_token

    @flask_app.after_request
    def add_secure_flag_to_session_cookie(response):
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
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
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

        mtg_hosts = get_all_mtg_hosts()
        for host in mtg_hosts:
            host["plans"] = get_plans_for_host(host["host_name"], service_type="mtg")

        return {
            "settings": current_settings,
            "hosts": hosts,
            "global_plans": get_plans_for_host("ALL", service_type="xui"),
            "mtg_hosts": mtg_hosts,
            "payment_rules": get_all_payment_rules(),
            "all_payment_methods": ALL_PAYMENT_METHODS,
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

        chart_data = get_daily_stats_for_charts(days=30)
        common_data = get_common_template_data()

        return render_template(
            "dashboard.html",
            stats=stats,
            problem_users=problem_users,
            chart_data=chart_data,
            transactions=transactions,
            current_page=page,
            total_pages=total_pages,
            **common_data,
        )

    @flask_app.route("/users")
    @login_required
    def users_page():
        users = get_all_users()
        for user in users:
            user["user_keys"] = get_user_keys(user["telegram_id"])

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
            "users.html", users=users, issuance_data=issuance_data, **common_data
        )

    @flask_app.route("/export/users.csv")
    @login_required
    def export_users_csv():
        rows = []
        now = time_utils.get_msk_now()
        for user in get_all_users():
            keys = get_user_keys(int(user["telegram_id"]))
            active_keys = 0
            for key in keys:
                expiry = time_utils.parse_iso_to_msk(key.get("expiry_date"))
                if expiry and expiry > now:
                    active_keys += 1
            rows.append(
                {
                    "telegram_id": user.get("telegram_id"),
                    "username": user.get("username") or "",
                    "is_banned": int(bool(user.get("is_banned"))),
                    "trial_used": int(bool(user.get("trial_used"))),
                    "registration_date": user.get("registration_date") or "",
                    "total_spent": user.get("total_spent") or 0,
                    "total_months": user.get("total_months") or 0,
                    "keys_total": len(keys),
                    "keys_active": active_keys,
                }
            )

        return _csv_response(
            rows,
            filename=f"users-{time_utils.get_msk_now().strftime('%Y%m%d-%H%M%S')}.csv",
            fieldnames=[
                "telegram_id",
                "username",
                "is_banned",
                "trial_used",
                "registration_date",
                "total_spent",
                "total_months",
                "keys_total",
                "keys_active",
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
                       amount_currency, currency_name, payment_method, metadata, created_date
                FROM transactions
                ORDER BY created_date DESC
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
        enabled_xui_hosts = {
            host.get("host_name")
            for host in get_all_hosts(only_enabled=True)
            if host.get("host_name")
        }

        # Identify global plan IDs
        try:
            global_plan_ids = {
                int(p["plan_id"])
                for p in get_plans_for_host("ALL", service_type="xui")
                if p.get("plan_id") is not None
            }
        except Exception:
            global_plan_ids = set()

        # Group keys by user and mark global ones
        users_map = {}
        for key in all_keys:
            uid = key["user_id"]
            if uid not in users_map:
                users_map[uid] = {
                    "username": key.get("username") or f"User {uid}",
                    "user_id": uid,
                    "subscription_link": None,
                    "user_keys": [],
                }

            # Mark if key is part of a global subscription
            plan_id = key.get("plan_id")
            host_name = key.get("host_name")
            is_xui_bundle = (
                key.get("service_type") == "xui"
                and bool(key.get("subscription_token"))
                and host_name in enabled_xui_hosts
            )
            key["is_global"] = is_xui_bundle or (
                plan_id is not None and int(plan_id) in global_plan_ids
            )
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
                int(key.get("plan_id") or 0) == 0 for key in deduped_global
            )

        grouped_users = sorted(users_map.values(), key=lambda u: u["username"])

        common_data = get_common_template_data()
        return render_template(
            "keys.html",
            grouped_users=grouped_users,
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

            # Check if this key belongs to a Global Plan
            is_global = False
            try:
                enabled_xui_hosts = {
                    host.get("host_name")
                    for host in get_all_hosts(only_enabled=True)
                    if host.get("host_name")
                }
                global_plan_ids = {
                    int(p["plan_id"])
                    for p in get_plans_for_host("ALL", service_type="xui")
                    if p.get("plan_id") is not None
                }
                plan_id = key_data.get("plan_id")
                if (
                    key_data.get("service_type") == "xui"
                    and key_data.get("host_name") in enabled_xui_hosts
                ) or (plan_id is not None and int(plan_id) in global_plan_ids):
                    is_global = True
            except Exception as e:
                logger.error(f"Error checking global plan status: {e}")

            keys_to_adjust = [key_data]
            if is_global:
                user_keys = get_user_keys(key_data["user_id"])
                # Find other global keys for this user
                for k in user_keys:
                    plan_id = k.get("plan_id")
                    if k["key_id"] != key_id and (
                        (
                            k.get("service_type") == "xui"
                            and k.get("host_name") in enabled_xui_hosts
                        )
                        or (plan_id is not None and int(plan_id) in global_plan_ids)
                    ):
                        keys_to_adjust.append(k)

            success_count = 0
            new_expiry_date = None

            for k in keys_to_adjust:
                # Call logic to adjust on panel using seconds for precision
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
                    update_key_info(
                        k["key_id"], expiry_dt, result.get("connection_string")
                    )
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
                    flash(f"Ключ #{key_id} успешно изменён на {time_str}.", "success")
            else:
                flash(f"Ошибка при изменении ключа(ей) на сервере XUI.", "danger")

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
            ]:
                values = request.form.getlist(checkbox_key)
                value = values[-1] if values else "false"
                update_setting(checkbox_key, "true" if value == "true" else "false")

            for key in ALL_SETTINGS_KEYS:
                if key in [
                    "panel_password",
                    "force_subscription",
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
                ]:
                    continue
                value = request.form.get(key)
                if value is not None:
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
                                update_key_info(
                                    existing_key_db["key_id"],
                                    expiry_dt,
                                    result["connection_string"],
                                )
                                update_key_plan_id(
                                    existing_key_db["key_id"], int(plan["plan_id"])
                                )
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
                                add_new_key(
                                    user_id=user_id,
                                    host_name=h["host_name"],
                                    xui_client_uuid=result["client_uuid"],
                                    key_email=email,
                                    expiry_timestamp_ms=result["expiry_timestamp_ms"],
                                    connection_string=result["connection_string"],
                                    plan_id=plan["plan_id"],
                                )
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
                            update_key_info(
                                existing_key_db["key_id"],
                                expiry_dt,
                                result["connection_string"],
                            )
                            update_key_plan_id(
                                existing_key_db["key_id"], int(plan["plan_id"])
                            )
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
        success = create_host(
            name=host_name,
            url=request.form["host_url"].strip(),
            user=request.form["host_username"].strip(),
            passwd=request.form["host_pass"],
            inbound=int(request.form["host_inbound_id"].strip()),
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

        try:
            inbound = int(inbound_raw)
        except ValueError:
            flash("ID входящего подключения должен быть целым числом.", "warning")
            return redirect(url_for("edit_host_page", host_name=old_host_name))

        success = update_host(
            old_name=old_host_name,
            new_name=new_host_name,
            url=host_url,
            user=host_username,
            passwd=host_pass,
            inbound=inbound,
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
        keys = get_keys_for_host(host_name)
        if keys:

            async def _delete_all_clients_strict():
                tasks = [
                    xui_api.delete_client_on_host(host_name, key.get("key_email"))
                    for key in keys
                    if key.get("key_email")
                ]
                if not tasks:
                    return True
                # Use semaphore to limit concurrent deletions (avoid overwhelming the API)
                semaphore = asyncio.Semaphore(5)

                async def delete_with_semaphore(client_task):
                    async with semaphore:
                        return await client_task

                results = await asyncio.wait_for(
                    asyncio.gather(
                        *[delete_with_semaphore(task) for task in tasks],
                        return_exceptions=True,
                    ),
                    timeout=300,  # Increased timeout for large number of clients
                )
                all_ok = True
                for result in results:
                    if isinstance(result, Exception):
                        logger.error(
                            f"Error deleting client during host removal: {result}",
                            exc_info=True,
                        )
                        all_ok = False
                    elif result is False:
                        all_ok = False
                return all_ok

            loop = current_app.config.get("EVENT_LOOP")
            try:
                if loop and loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        _delete_all_clients_strict(), loop
                    )
                    all_ok = future.result(timeout=305)  # Match the increased timeout
                else:
                    logger.error(
                        f"Cannot delete clients from host '{host_name}': event loop is not running."
                    )
                    all_ok = False
            except Exception as e:
                logger.error(
                    f"Failed to delete clients from host '{host_name}': {e}",
                    exc_info=True,
                )
                all_ok = False

            if not all_ok:
                flash(
                    "Удаление остановлено: не все клиенты удалены из 3x-ui. Хост не удалён.",
                    "danger",
                )
                return redirect(url_for("settings_page"))

        if not delete_host(host_name):
            flash(
                "Хост не удалось удалить из локальной базы. Проверьте логи.",
                "danger",
            )
            return redirect(url_for("settings_page"))
        flash(f"Хост '{host_name}' и все его тарифы были удалены.", "success")
        return redirect(url_for("settings_page"))

    @flask_app.route("/add-plan", methods=["POST"])
    @login_required
    def add_plan_route():
        service_type = request.form.get("service_type", "xui")
        create_plan(
            host_name=request.form["host_name"],
            plan_name=request.form["plan_name"],
            months=int(request.form["months"]),
            price=float(request.form["price"]),
            service_type=service_type,
        )
        flash(
            f"Новый тариф для хоста '{request.form['host_name']}' добавлен.", "success"
        )
        return redirect(url_for("settings_page"))

    @flask_app.route("/delete-plan/<int:plan_id>", methods=["POST"])
    @login_required
    def delete_plan_route(plan_id):
        delete_plan(plan_id)
        flash("Тариф успешно удален.", "success")
        return redirect(url_for("settings_page"))

    # ── MTG Proxy host routes ─────────────────────────────────────────────────

    @flask_app.route("/add-mtg-host", methods=["POST"])
    @login_required
    def add_mtg_host_route():
        host_name = request.form["host_name"]
        success = create_mtg_host(
            name=host_name,
            url=request.form["host_url"],
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
        success = update_mtg_host(
            old_name=old_host_name,
            new_name=new_host_name,
            url=request.form["host_url"],
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
        from shop_bot.modules import mtg_api as _mtg_api

        keys = get_keys_for_host(host_name)
        # Remove proxies from MTG panel before deleting DB state to avoid orphaned remote users.
        if keys:

            async def _delete_mtg_proxies():
                all_ok = True
                for key in keys:
                    proxy_name = key.get("key_email")
                    node_id_str = key.get("xui_client_uuid")
                    if proxy_name and node_id_str and key.get("service_type") == "mtg":
                        try:
                            deleted = await _mtg_api.delete_proxy_for_user(
                                host_name, proxy_name, int(node_id_str)
                            )
                            if not deleted:
                                all_ok = False
                        except Exception as e:
                            logger.warning(
                                f"Could not delete MTG proxy '{proxy_name}': {e}"
                            )
                            all_ok = False
                return all_ok

            try:
                all_ok = _run_async(_delete_mtg_proxies(), timeout=120)
            except Exception as e:
                logger.error(
                    f"Failed to delete MTG proxies for host '{host_name}': {e}",
                    exc_info=True,
                )
                all_ok = False

            if not all_ok:
                flash(
                    "Удаление остановлено: не все MTG proxy удалены с панели. Хост не удалён.",
                    "danger",
                )
                return redirect(url_for("settings_page"))

        if not delete_mtg_host(host_name):
            flash(
                "MTG-хост не удалось удалить из локальной базы. Проверьте логи.",
                "danger",
            )
            return redirect(url_for("settings_page"))
        flash(f"MTG-хост '{host_name}' и все его тарифы удалены.", "success")
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
        return redirect(url_for("settings_page") + "#payment-rules")

    @flask_app.route("/payment-rules/reset", methods=["POST"])
    @login_required
    def reset_payment_rules_route():
        context_key = request.form.get("context_key", "").strip()
        if context_key:
            delete_payment_rules_for_context(context_key)
            flash(
                f"Правила оплаты для «{context_key}» сброшены до глобальных.", "success"
            )
        return redirect(url_for("settings_page"))

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

    def _cryptobot_webhook_handler_impl():
        reserved_payment_id: str | None = None
        try:
            signature_valid = _is_valid_cryptobot_signature()

            configured_secret = get_setting("cryptobot_webhook_secret")
            request_secret = _extract_cryptobot_secret_from_request()
            legacy_secret_valid = bool(
                configured_secret
                and request_secret
                and compare_digest(str(request_secret), str(configured_secret))
            )

            if not signature_valid and not legacy_secret_valid:
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

                if payment_id:
                    metadata = _reserve_pending_transaction_for_cryptobot(
                        payment_id,
                        amount_currency=cb_amount,
                        currency_name=currency_name,
                    )
                    if metadata is None:
                        logger.warning(
                            "CryptoBot webhook: pending transaction %s not found or already reserved.",
                            payment_id,
                        )
                        return "OK", 200
                    payload_price = metadata.get("price")
                    metadata["payment_method"] = "CryptoBot"
                    metadata["provider_payment_id"] = payment_id
                    metadata["cryptobot_invoice_id"] = str(
                        external_invoice_id or external_id_fallback or ""
                    )
                    reserved_payment_id = payment_id
                else:
                    parts = payload_string.split(":")
                    if len(parts) < 9:
                        logger.error(
                            f"cryptobot Webhook: Invalid payload format received: {payload_string}"
                        )
                        return "Error", 400

                    metadata = {
                        "user_id": parts[0],
                        "months": parts[1],
                        "price": parts[2],
                        "action": parts[3],
                        "key_id": parts[4],
                        "host_name": parts[5],
                        "plan_id": parts[6],
                        "customer_email": parts[7] if parts[7] != "None" else None,
                        "payment_method": "CryptoBot",
                        "provider_payment_id": str(
                            external_invoice_id or external_id_fallback or ""
                        ),
                        "cryptobot_invoice_id": str(
                            external_invoice_id or external_id_fallback or ""
                        ),
                    }
                    payload_price = parts[2]

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
            logger.warning(
                "CryptoBot Webhook: path-based secret is deprecated but temporarily accepted."
            )
            return _cryptobot_webhook_handler_impl()
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

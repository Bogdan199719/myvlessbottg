import os
import logging
import asyncio
import json
import hashlib
import base64
import sqlite3
import tempfile
import zipfile
import shutil
import sys
from hmac import compare_digest
from datetime import datetime
from shop_bot.utils import time_utils, update_manager
from shop_bot.version import APP_VERSION
from functools import wraps
from math import ceil
from pathlib import Path
from flask import Flask, request, render_template, redirect, url_for, flash, session, current_app, send_file, after_this_request
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from yookassa import Configuration
from yookassa import Payment

from shop_bot.modules import xui_api
from shop_bot.bot import handlers 
from shop_bot.webhook_server.subscription_api import subscription_bp
from shop_bot.data_manager import scheduler
from shop_bot.data_manager.database import (
    get_all_settings, update_setting, get_all_hosts, get_plans_for_host,
    create_host, delete_host, create_plan, delete_plan, get_user_count,
    get_total_keys_count, get_total_spent_sum, get_daily_stats_for_charts,
    get_recent_transactions, get_paginated_transactions, get_all_users, get_user_keys,
    ban_user, unban_user, delete_user_keys, delete_user_everywhere, get_setting, find_and_complete_ton_transaction, DB_FILE,
    register_user_if_not_exists, get_next_key_number, get_key_by_id,
    update_key_info, set_trial_used, set_terms_agreed, get_plan_by_id, log_transaction,
    get_referral_count, add_to_referral_balance, create_pending_transaction, run_migration,
    set_referral_balance, set_referral_balance_all, get_all_keys_with_usernames,
    update_key_connection_string,
    get_host, update_host, toggle_host_status, get_keys_for_host,
    add_new_key, get_user, update_user_stats
)

_bot_controller = None

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
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        if include_env:
            env_path = Path(".env")
            if env_path.exists():
                shutil.copy(env_path, temp_dir / ".env")

        zip_path = temp_dir / f"backup-{time_utils.get_msk_now().strftime('%Y%m%d-%H%M%S')}.zip"
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
        member_path = (extract_dir / member.filename).resolve()
        if not str(member_path).startswith(str(extract_root)):
            raise ValueError("Недопустимый путь в архиве.")
    zip_ref.extractall(extract_dir)

def _restore_from_backup(zip_file, apply_env: bool = False):
    temp_dir = Path(tempfile.mkdtemp(prefix="restore_"))
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
                    raise ValueError("Контрольная сумма БД не совпадает, архив повреждён.")

        # Остановить ботов перед заменой БД
        try:
            if _bot_controller and _bot_controller.get_status().get("is_running"):
                _bot_controller.stop()
        except Exception as e:
            logger.error(f"Failed to stop bots before restore: {e}", exc_info=True)

        # Резервная копия текущей базы
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        if DB_FILE.exists():
            backup_path = DB_FILE.with_suffix(f".bak.{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}")
            shutil.copyfile(DB_FILE, backup_path)

        # Замена базы
        shutil.copyfile(db_src, DB_FILE)
        run_migration()

        if apply_env:
            env_src = extract_dir / ".env"
            if env_src.exists():
                shutil.copyfile(env_src, Path(".env"))

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def _ensure_processed_webhooks_table():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_webhooks (
                    provider TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (provider, external_id)
                )
                """
            )
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
        logger.error(f"Failed to check webhook processed for {provider}:{external_id}: {e}")
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
        logger.error(f"Failed to set webhook processed for {provider}:{external_id}: {e}")

ALL_SETTINGS_KEYS = [
    "panel_login", "panel_password", "about_text", "terms_url", "privacy_url",
    "support_user", "support_text", "channel_url", "telegram_bot_token",
    "telegram_bot_username", "admin_telegram_id", "yookassa_shop_id",
    "yookassa_secret_key", "sbp_enabled", "receipt_email", "cryptobot_token", "cryptobot_webhook_secret",
    "heleket_merchant_id", "heleket_api_key", "domain", "referral_percentage",
    "referral_discount", "ton_wallet_address", "tonapi_key", "force_subscription", "trial_enabled", "trial_duration_days", "enable_referrals", "minimum_withdrawal",
    "support_group_id", "support_bot_token", "p2p_enabled", "p2p_card_number", "stars_enabled", "stars_rub_per_star",
    "enable_admin_payment_notifications", "enable_admin_trial_notifications", "subscription_name",
    "subscription_live_sync", "subscription_live_stats", "subscription_allow_fallback_host_fetch",
    "subscription_auto_provision",
    "panel_sync_enabled", "xtls_sync_enabled"
]

def create_webhook_app(bot_controller_instance):
    global _bot_controller
    _bot_controller = bot_controller_instance

    _ensure_processed_webhooks_table()
    
    # Ensure template and static folder relative to this file's location
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    flask_app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, 'templates'),
        static_folder=os.path.join(base_dir, 'static')
    )
    
    flask_app.register_blueprint(subscription_bp)
    
    secret_key = os.getenv('FLASK_SECRET_KEY')
    if not secret_key:
        secret_key = get_setting('flask_secret_key')
    if not secret_key:
        secret_key = os.urandom(32).hex()
        update_setting('flask_secret_key', secret_key)
    flask_app.config['SECRET_KEY'] = secret_key
    
    # Security Hardening
    flask_app.config['SESSION_COOKIE_HTTPONLY'] = True
    flask_app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    # CSRF Protection
    @flask_app.before_request
    def csrf_protect():
        if request.method == "POST":
            # Skip CSRF for webhooks
            if request.path in ['/yookassa-webhook', '/cryptobot-webhook', '/heleket-webhook', '/ton-webhook']:
                return
            if request.path.startswith('/cryptobot-webhook/'):
                return
                
            target_token = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
            token = session.get('_csrf_token')
            if not token or token != target_token:
                return "CSRF Token missing or invalid!", 403

    def generate_csrf_token():
        if '_csrf_token' not in session:
            session['_csrf_token'] = os.urandom(24).hex()
        return session['_csrf_token']

    flask_app.jinja_env.globals['csrf_token'] = generate_csrf_token

    @flask_app.context_processor
    def inject_current_year():
        return {'current_year': time_utils.get_msk_now().year}

    def login_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'logged_in' not in session:
                return redirect(url_for('login_page', next=request.url))
            return f(*args, **kwargs)
        return decorated_function

    def _verify_and_upgrade_panel_password(plain_password: str, stored_password: str | None) -> bool:
        if not stored_password:
            return False

        # werkzeug hashes usually start with something like: pbkdf2:sha256:...
        is_hashed = stored_password.startswith('pbkdf2:') or stored_password.startswith('scrypt:')
        if is_hashed:
            return check_password_hash(stored_password, plain_password)

        # Legacy plaintext password support (auto-upgrade on successful login)
        if plain_password == stored_password:
            try:
                update_setting('panel_password', generate_password_hash(plain_password))
            except Exception:
                logger.exception("Failed to upgrade legacy panel_password to hashed format")
            return True

        return False

    @flask_app.route('/login', methods=['GET', 'POST'])
    def login_page():
        settings = get_all_settings()
        if request.method == 'POST':
            username_ok = request.form.get('username') == settings.get("panel_login")
            password_ok = _verify_and_upgrade_panel_password(
                request.form.get('password', ''),
                settings.get("panel_password"),
            )
            if username_ok and password_ok:
                session['logged_in'] = True
                next_url = request.args.get('next')
                if next_url and next_url.startswith('/'): # Validate redirect
                    return redirect(next_url)
                return redirect(url_for('dashboard_page'))
            else:
                flash('Неверный логин или пароль', 'danger')
        return render_template('login.html')

    @flask_app.route('/logout', methods=['POST'])
    @login_required
    def logout_page():
        session.pop('logged_in', None)
        flash('Вы успешно вышли.', 'success')
        return redirect(url_for('login_page'))

    def get_common_template_data():
        bot_status = _bot_controller.get_status()
        settings = get_all_settings()
        required_for_start = ['telegram_bot_token', 'telegram_bot_username', 'admin_telegram_id']
        all_settings_ok = all(settings.get(key) for key in required_for_start)
        return {"bot_status": bot_status, "all_settings_ok": all_settings_ok}

    @flask_app.route('/')
    @login_required
    def index():
        return redirect(url_for('dashboard_page'))

    @flask_app.route('/dashboard')
    @login_required
    def dashboard_page():
        stats = {
            "user_count": get_user_count(),
            "total_keys": get_total_keys_count(),
            "total_spent": get_total_spent_sum(),
            "host_count": len(get_all_hosts())
        }
        
        page = request.args.get('page', 1, type=int)
        per_page = 8
        
        transactions, total_transactions = get_paginated_transactions(page=page, per_page=per_page)
        total_pages = ceil(total_transactions / per_page)
        
        chart_data = get_daily_stats_for_charts(days=30)
        common_data = get_common_template_data()
        
        return render_template(
            'dashboard.html',
            stats=stats,
            chart_data=chart_data,
            transactions=transactions,
            current_page=page,
            total_pages=total_pages,
            **common_data
        )

    @flask_app.route('/users')
    @login_required
    def users_page():
        users = get_all_users()
        for user in users:
            user['user_keys'] = get_user_keys(user['telegram_id'])
        
        # Prepare plans for manual issuance
        all_hosts = get_all_hosts()
        # Structure: {'global': [plans], 'hosts': {hostname: [plans]}}
        issuance_data = {
            'global_plans': get_plans_for_host('ALL'),
            'host_plans': {}
        }
        for host in all_hosts:
            plans = get_plans_for_host(host['host_name'])
            if plans:
                issuance_data['host_plans'][host['host_name']] = plans

        common_data = get_common_template_data()
        return render_template('users.html', users=users, issuance_data=issuance_data, **common_data)

    @flask_app.route('/keys')
    @login_required
    def keys_page():
        all_keys = get_all_keys_with_usernames()
        
        # Identify global plan IDs
        try:
            global_plan_ids = {
                int(p['plan_id'])
                for p in get_plans_for_host('ALL')
                if p.get('plan_id') is not None
            }
        except Exception:
            global_plan_ids = set()

        # Group keys by user and mark global ones
        users_map = {}
        for key in all_keys:
            uid = key['user_id']
            if uid not in users_map:
                users_map[uid] = {
                    'username': key.get('username') or f"User {uid}",
                    'user_id': uid,
                    'user_keys': []
                }
            
            # Mark if key is part of a global subscription
            key['is_global'] = bool(key.get('plan_id') and int(key['plan_id']) in global_plan_ids)
            users_map[uid]['user_keys'].append(key)
        
        grouped_users = sorted(users_map.values(), key=lambda u: u['username'])
        
        common_data = get_common_template_data()
        return render_template('keys.html', grouped_users=grouped_users, **common_data)



    @flask_app.route('/keys/adjust/<int:key_id>', methods=['POST'])
    @login_required
    def adjust_key_duration(key_id):
        """Adjust key duration by days and/or hours. Supports negative values to reduce duration."""
        try:
            days_to_adjust = int(request.form.get('days', 0))
            hours_to_adjust = int(request.form.get('hours', 0))
            
            # Calculate total seconds to adjust
            total_seconds = days_to_adjust * 86400 + hours_to_adjust * 3600
            
            if total_seconds == 0:
                flash("Укажите количество дней или часов для изменения.", "warning")
                return redirect(url_for('keys_page'))
            
            key_data = get_key_by_id(key_id)
            if not key_data:
                flash(f"Ключ {key_id} не найден.", "danger")
                return redirect(url_for('keys_page'))

            # Check if this key belongs to a Global Plan
            is_global = False
            try:
                global_plan_ids = {
                    int(p['plan_id'])
                    for p in get_plans_for_host('ALL')
                    if p.get('plan_id') is not None
                }
                if key_data.get('plan_id') and int(key_data['plan_id']) in global_plan_ids:
                    is_global = True
            except Exception as e:
                logger.error(f"Error checking global plan status: {e}")
            
            keys_to_adjust = [key_data]
            if is_global:
                user_keys = get_user_keys(key_data['user_id'])
                # Find other global keys for this user
                for k in user_keys:
                    if k['key_id'] != key_id and k.get('plan_id') and int(k['plan_id']) in global_plan_ids:
                        keys_to_adjust.append(k)
            
            success_count = 0
            new_expiry_date = None

            for k in keys_to_adjust:
                # Call logic to adjust on panel using seconds for precision
                result = asyncio.run(xui_api.create_or_update_key_on_host_seconds(
                    host_name=k['host_name'],
                    email=k['key_email'],
                    seconds_to_add=total_seconds,
                    telegram_id=None  # Admin adjustment, no telegram_id available
                ))
                
                if result:
                    # Update local DB with new expiry from result
                    expiry_dt = time_utils.from_timestamp_ms(result['expiry_timestamp_ms'])
                    update_key_info(k['key_id'], expiry_dt, result.get('connection_string'))
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
                    user_id = key_data['user_id']
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

                    loop = current_app.config.get('EVENT_LOOP')
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot.send_message(user_id, msg_text, parse_mode='HTML'),
                            loop
                        )

                if is_global:
                    flash(f"Глобальная подписка {action_text}! Обновлено {success_count} ключей на {time_str}.", "success")
                else:
                    flash(f"Ключ #{key_id} успешно изменён на {time_str}.", "success")
            else:
                flash(f"Ошибка при изменении ключа(ей) на сервере XUI.", "danger")

        except Exception as e:
            logger.error(f"Error adjusting key duration: {e}", exc_info=True)
            flash("Произошла ошибка при изменении.", "danger")
        
        return redirect(url_for('keys_page'))

    @flask_app.route('/keys/sync', methods=['POST'])
    @login_required
    def sync_keys_configs():
        try:
            all_keys = get_all_keys_with_usernames()
            loop = current_app.config.get('EVENT_LOOP')
            if not loop or not loop.is_running():
                flash("Цикл событий недоступен. Перезапустите приложение.", "danger")
                return redirect(url_for('keys_page'))

            keys_by_host = {}
            for key in all_keys:
                host_name = key.get('host_name')
                if not host_name:
                    continue
                keys_by_host.setdefault(host_name, []).append(key)

            async def _sync_all_keys():
                total_updated = 0
                hosts = [h['host_name'] for h in get_all_hosts(only_enabled=True)]
                for host_name in hosts:
                    if host_name not in keys_by_host:
                        continue
                    try:
                        mapping = await asyncio.wait_for(
                            xui_api.get_connection_strings_for_host(host_name),
                            timeout=15
                        )
                    except Exception as k_e:
                        logger.warning(f"Failed to sync host '{host_name}': {k_e!r}", exc_info=True)
                        await asyncio.sleep(0.5)
                        continue

                    for key in keys_by_host[host_name]:
                        email = key.get('key_email')
                        if not email:
                            continue
                        conn = mapping.get(email)
                        if conn:
                            update_key_connection_string(key['key_id'], conn)
                            total_updated += 1

                    await asyncio.sleep(0.5)

                logger.info(f"Sync keys completed. Updated connection strings: {total_updated}")

            asyncio.run_coroutine_threadsafe(_sync_all_keys(), loop)
            flash("Синхронизация ключей запущена в фоне. Проверьте логи позже.", "info")
        except Exception as e:
            logger.error(f"Error syncing keys: {e}", exc_info=True)
            flash("Не удалось запустить синхронизацию ключей.", "danger")

        return redirect(url_for('keys_page'))

    @flask_app.route('/keys/fix-parameters', methods=['POST'])
    @login_required
    def fix_client_parameters():
        try:
            loop = current_app.config.get('EVENT_LOOP')
            if not loop or not loop.is_running():
                flash("Цикл событий недоступен. Перезапустите приложение.", "danger")
                return redirect(url_for('keys_page'))

            async def _fix_all_clients():
                total_fixed = 0
                hosts = [h['host_name'] for h in get_all_hosts(only_enabled=True)]
                for host_name in hosts:
                    try:
                        fixed = await asyncio.wait_for(
                            xui_api.fix_all_client_parameters_on_host(host_name),
                            timeout=20
                        )
                        total_fixed += fixed
                    except Exception as k_e:
                        logger.warning(f"Failed to fix clients on host '{host_name}': {k_e!r}", exc_info=True)
                    await asyncio.sleep(1)

                logger.info(f"Fix parameters completed. Updated clients: {total_fixed}")

            asyncio.run_coroutine_threadsafe(_fix_all_clients(), loop)
            flash("Исправление параметров запущено в фоне. Проверьте логи позже.", "info")
        except Exception as e:
            logger.error(f"Fix parameters error: {e}", exc_info=True)
            flash("Не удалось запустить исправление параметров клиентов.", "danger")
        return redirect(url_for('keys_page'))

    @flask_app.route('/settings', methods=['GET', 'POST'])
    @login_required
    def settings_page():
        if request.method == 'POST':
            if 'panel_password' in request.form and request.form.get('panel_password'):
                update_setting('panel_password', generate_password_hash(request.form.get('panel_password')))

            for checkbox_key in ['force_subscription', 'sbp_enabled', 'trial_enabled', 'enable_referrals', 'p2p_enabled', 'stars_enabled', 'yookassa_enabled', 'cryptobot_enabled', 'heleket_enabled', 'tonconnect_enabled', 'enable_admin_payment_notifications', 'enable_admin_trial_notifications', 'email_prompt_enabled']:
                values = request.form.getlist(checkbox_key)
                value = values[-1] if values else 'false'
                update_setting(checkbox_key, 'true' if value == 'true' else 'false')

            for key in ALL_SETTINGS_KEYS:
                if key in ['panel_password', 'force_subscription', 'sbp_enabled', 'trial_enabled', 'enable_referrals', 'p2p_enabled', 'stars_enabled', 'yookassa_enabled', 'cryptobot_enabled', 'heleket_enabled', 'tonconnect_enabled', 'enable_admin_payment_notifications', 'enable_admin_trial_notifications', 'email_prompt_enabled']:
                    continue
                update_setting(key, request.form.get(key, ''))

            flash('Настройки успешно сохранены!', 'success')
            return redirect(url_for('settings_page'))

        current_settings = get_all_settings()
        hosts = get_all_hosts()
        for host in hosts:
            host['plans'] = get_plans_for_host(host['host_name'])
        
        global_plans = get_plans_for_host('ALL')
        
        common_data = get_common_template_data()
        return render_template('settings.html', settings=current_settings, hosts=hosts, global_plans=global_plans, **common_data)

    @flask_app.route('/start-shop-bot', methods=['POST'])
    @login_required
    def start_shop_bot_route():
        result = _bot_controller.start_shop_bot()
        flash(result.get('message', 'An error occurred.'), 'success' if result.get('status') == 'success' else 'danger')
        return redirect(request.referrer or url_for('dashboard_page'))

    @flask_app.route('/stop-shop-bot', methods=['POST'])
    @login_required
    def stop_shop_bot_route():
        result = _bot_controller.stop_shop_bot()
        flash(result.get('message', 'An error occurred.'), 'success' if result.get('status') == 'success' else 'danger')
        return redirect(request.referrer or url_for('dashboard_page'))

    @flask_app.route('/start-support-bot', methods=['POST'])
    @login_required
    def start_support_bot_route():
        result = _bot_controller.start_support_bot()
        flash(result.get('message', 'An error occurred.'), 'success' if result.get('status') == 'success' else 'danger')
        return redirect(request.referrer or url_for('dashboard_page'))

    @flask_app.route('/stop-support-bot', methods=['POST'])
    @login_required
    def stop_support_bot_route():
        result = _bot_controller.stop_support_bot()
        flash(result.get('message', 'An error occurred.'), 'success' if result.get('status') == 'success' else 'danger')
        return redirect(request.referrer or url_for('dashboard_page'))

    # ==========================
    # UPDATE SYSTEM ROUTES
    # ==========================
    @flask_app.route('/updates', methods=['GET'])
    @login_required
    def updates_page():
        common_data = get_common_template_data()
        current_version = APP_VERSION
        return render_template('updates.html', current_version=current_version, **common_data)

    @flask_app.route('/api/updates/check', methods=['POST'])
    @login_required
    def check_updates_route():
        result = update_manager.check_for_updates()
        if "error" in result:
             return {"status": "error", "message": result["error"]}, 500
        return {"status": "success", "data": result}

    @flask_app.route('/api/updates/perform', methods=['POST'])
    @login_required
    def perform_update_route():
        # This is a potentially long running task, ideally should be async.
        # But since it restarts the app, we can just return and let it die.
        result = update_manager.perform_update()
        if result["status"] == "error":
            return {"status": "error", "message": result["message"]}, 500
        
        # On success, the container will likely restart shortly, so the frontend might see a network error or reload.
        return {"status": "success", "message": result["message"]}


    @flask_app.route('/users/ban/<int:user_id>', methods=['POST'])
    @login_required
    def ban_user_route(user_id):
        ban_user(user_id)
        flash(f'Пользователь {user_id} был заблокирован.', 'success')
        return redirect(url_for('users_page'))

    @flask_app.route('/users/unban/<int:user_id>', methods=['POST'])
    @login_required
    def unban_user_route(user_id):
        unban_user(user_id)
        flash(f'Пользователь {user_id} был разблокирован.', 'success')
        return redirect(url_for('users_page'))

    @flask_app.route('/users/revoke/<int:user_id>', methods=['POST'])
    @login_required
    def revoke_keys_route(user_id):
        keys_to_revoke = get_user_keys(user_id)
        success_count = 0
        
        for key in keys_to_revoke:
            result = asyncio.run(xui_api.delete_client_on_host(key['host_name'], key['key_email']))
            if result:
                success_count += 1
        
        delete_user_keys(user_id)
        
        if success_count == len(keys_to_revoke):
            flash(f"Все {len(keys_to_revoke)} ключей для пользователя {user_id} были успешно отозваны.", 'success')
        else:
            flash(f"Удалось отозвать {success_count} из {len(keys_to_revoke)} ключей для пользователя {user_id}. Проверьте логи.", 'warning')

        return redirect(url_for('users_page'))

    @flask_app.route('/users/issue-key/<int:user_id>', methods=['POST'])
    @login_required
    def issue_key_route(user_id):
        try:
            plan_id = request.form.get('plan_id')
            if not plan_id:
                flash("Ошибка: не выбран тариф.", "danger")
                return redirect(url_for('users_page'))

            plan = get_plan_by_id(int(plan_id))
            if not plan:
                flash("Ошибка: Тариф не найден.", "danger")
                return redirect(url_for('users_page'))

            user = get_user(user_id)
            if not user:
                 flash("Ошибка: Пользователь не найден.", "danger")
                 return redirect(url_for('users_page'))

            month_qty = plan['months']
            days_to_add = month_qty * 30 
            
            user_keys = get_user_keys(user_id)
            key_number = None # Will be fetched only if a NEW key is actually needed
            
            issued_count = 0
            primary_key_id = None
            
            if plan['host_name'] == 'ALL':
                # Global Plan
                hosts = get_all_hosts(only_enabled=True)
                
                # We need a key number for any NEW keys we might create
                # To be consistent with existing logic, we fetch it once
                key_number = get_next_key_number(user_id)
                
                for h in hosts:
                     try:
                        existing_key_db = None
                        for k in user_keys:
                            # Re-use existing PAID keys on this host
                            if k['host_name'] == h['host_name'] and k.get('plan_id', 0) > 0:
                                existing_key_db = k
                                break
                        
                        if existing_key_db:
                            # Update existing key on panel and DB
                            result = asyncio.run(xui_api.create_or_update_key_on_host(
                                host_name=h['host_name'],
                                email=existing_key_db['key_email'],
                                days_to_add=days_to_add,
                                telegram_id=str(user_id)
                            ))
                            if result:
                                expiry_dt = time_utils.from_timestamp_ms(result['expiry_timestamp_ms'])
                                update_key_info(existing_key_db['key_id'], expiry_dt, result['connection_string'])
                                issued_count += 1
                        else:
                            # Create new key
                            email = f"user{user_id}-key{key_number}-{h['host_name'].replace(' ', '').lower()}"
                            result = asyncio.run(xui_api.create_or_update_key_on_host(
                                host_name=h['host_name'],
                                email=email,
                                days_to_add=days_to_add,
                                telegram_id=str(user_id)
                            ))
                            if result:
                                add_new_key(
                                    user_id=user_id,
                                    host_name=h['host_name'],
                                    xui_client_uuid=result['client_uuid'],
                                    key_email=email,
                                    expiry_timestamp_ms=result['expiry_timestamp_ms'],
                                    connection_string=result['connection_string'],
                                    plan_id=plan['plan_id']
                                )
                                issued_count += 1
                     except Exception as e_h:
                          logger.error(f"Failed to issue manual key on host {h['host_name']}: {e_h}")
                
                msg = f"Глобальная подписка успешно выдана! ({issued_count} ключей обработано)"
            
            else:
                # Single host
                try:
                    host_name = plan['host_name']
                    
                    existing_key_db = None
                    for k in user_keys:
                        if k['host_name'] == host_name and k.get('plan_id', 0) > 0:
                            existing_key_db = k
                            break
                    
                    if existing_key_db:
                        # Extend existing
                        result = asyncio.run(xui_api.create_or_update_key_on_host(
                            host_name=host_name,
                            email=existing_key_db['key_email'],
                            days_to_add=days_to_add,
                            telegram_id=str(user_id)
                        ))
                        if result:
                            expiry_dt = datetime.fromtimestamp(result['expiry_timestamp_ms'] / 1000)
                            update_key_info(existing_key_db['key_id'], expiry_dt, result['connection_string'])
                            primary_key_id = existing_key_db['key_id']
                            issued_count += 1
                    else:
                        # Create new
                        key_number = get_next_key_number(user_id)
                        email = f"user{user_id}-key{key_number}-{host_name.replace(' ', '').lower()}"
                        
                        result = asyncio.run(xui_api.create_or_update_key_on_host(
                            host_name=host_name,
                            email=email,
                            days_to_add=days_to_add,
                            telegram_id=str(user_id)
                        ))
                        
                        if result:
                            new_key_id = add_new_key(
                                user_id=user_id,
                                host_name=host_name,
                                xui_client_uuid=result['client_uuid'],
                                key_email=email,
                                expiry_timestamp_ms=result['expiry_timestamp_ms'],
                                connection_string=result['connection_string'],
                                plan_id=plan['plan_id']
                            )
                            primary_key_id = new_key_id
                            issued_count += 1
                    
                    if issued_count > 0:
                        msg = f"Подписка на сервер {host_name} успешно выдана!"
                    else:
                        flash("Не удалось создать/обновить ключ на сервере XUI.", "danger")
                        return redirect(url_for('users_page'))
                        
                except Exception as e_s:
                     logger.error(f"Failed to issue manual key: {e_s}")
                     flash(f"Ошибка при выдаче: {e_s}", "danger")
                     return redirect(url_for('users_page'))

            # Update user stats
            update_user_stats(user_id, 0, month_qty) 
            
            # Notify User
            bot = _bot_controller.get_bot_instance()
            if bot:
                loop = current_app.config.get('EVENT_LOOP')
                verdict_text = f"Администратор выдал вам подписку: <b>{plan['plan_name']}</b>\nСрок: {month_qty} мес."
                if loop and loop.is_running():
                     asyncio.run_coroutine_threadsafe(
                        bot.send_message(user_id, f"🎁 <b>Вам выдана подписка!</b>\n\n{verdict_text}", parse_mode='HTML'),
                        loop
                    )

            flash(msg, "success")
            
        except Exception as e:
            logger.error(f"Error issuing key manually: {e}", exc_info=True)
            flash(f"Ошибка при выдаче подписки: {e}", "danger")

        return redirect(url_for('users_page'))

    @flask_app.route('/users/delete/<int:user_id>', methods=['POST'])
    @login_required
    def delete_user_route(user_id):
        keys_to_revoke = get_user_keys(user_id)
        success_count = 0

        for key in keys_to_revoke:
            result = asyncio.run(xui_api.delete_client_on_host(key['host_name'], key['key_email']))
            if result:
                success_count += 1

        delete_user_everywhere(user_id)

        if success_count == len(keys_to_revoke):
            flash(f"Пользователь {user_id} и все его данные были удалены. Ключей отозвано: {success_count}.", 'success')
        else:
            flash(f"Пользователь {user_id} удален из базы, но удалось отозвать {success_count} из {len(keys_to_revoke)} ключей. Проверьте логи.", 'warning')

        return redirect(url_for('users_page'))

    @flask_app.route('/add-host', methods=['POST'])
    @login_required
    def add_host_route():
        create_host(
            name=request.form['host_name'],
            url=request.form['host_url'],
            user=request.form['host_username'],
            passwd=request.form['host_pass'],
            inbound=int(request.form['host_inbound_id'])
        )
        flash(f"Хост '{request.form['host_name']}' успешно добавлен.", 'success')
        return redirect(url_for('settings_page'))

    @flask_app.route('/edit-host/<host_name>', methods=['GET'])
    @login_required
    def edit_host_page(host_name):
        current_settings = get_all_settings()
        hosts = get_all_hosts()
        for host in hosts:
            host['plans'] = get_plans_for_host(host['host_name'])
        global_plans = get_plans_for_host('ALL')
        
        target_host = get_host(host_name)
        if not target_host:
             flash(f"Хост '{host_name}' не найден.", 'warning')
             return redirect(url_for('settings_page'))
             
        common_data = get_common_template_data()
        return render_template('settings.html', settings=current_settings, hosts=hosts, global_plans=global_plans, edit_host=target_host, **common_data)

    @flask_app.route('/settings/backup', methods=['POST'])
    @login_required
    def backup_route():
        include_env = True
        try:
            zip_path, temp_dir = _create_backup_zip(include_env=include_env)
        except Exception as e:
            logger.error(f"Failed to create backup: {e}", exc_info=True)
            flash("Не удалось создать бэкап. Проверьте логи.", "danger")
            return redirect(url_for('settings_page'))

        @after_this_request
        def cleanup(response):
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
            return response

        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=zip_path.name
        )

    @flask_app.route('/settings/import', methods=['POST'])
    @login_required
    def import_route():
        if not request.files.get('backup_file'):
            flash("Файл бэкапа не выбран.", "warning")
            return redirect(url_for('settings_page'))

        backup_file = request.files['backup_file']
        apply_env = True

        try:
            _restore_from_backup(backup_file, apply_env=apply_env)
            flash("Бэкап успешно импортирован. Текущая база заменена.", "success")
        except ValueError as e:
            flash(str(e), "warning")
        except Exception as e:
            logger.error(f"Failed to restore from backup: {e}", exc_info=True)
            flash("Ошибка при импорте бэкапа. Проверьте логи.", "danger")

        return redirect(url_for('settings_page'))

    @flask_app.route('/update-host', methods=['POST'])
    @login_required
    def update_host_route():
        old_host_name = request.form['old_host_name']
        update_host(
            old_name=old_host_name,
            new_name=request.form['host_name'],
            url=request.form['host_url'],
            user=request.form['host_username'],
            passwd=request.form['host_pass'], # Can be empty
            inbound=int(request.form['host_inbound_id'])
        )
        
        flash(f"Хост '{old_host_name}' успешно обновлен.", 'success')
        return redirect(url_for('settings_page'))

    @flask_app.route('/toggle-host/<host_name>', methods=['POST'])
    @login_required
    def toggle_host_route(host_name):
        host = get_host(host_name)
        if host:
             new_status = not bool(host['is_enabled'])
             toggle_host_status(host_name, new_status)
             flash(f"Хост '{host_name}' {'включен' if new_status else 'отключен'}.", 'success')
        else:
             flash(f"Хост '{host_name}' не найден.", 'warning')
        return redirect(url_for('settings_page'))

    @flask_app.route('/delete-host/<host_name>', methods=['POST'])
    @login_required
    def delete_host_route(host_name):
        keys = get_keys_for_host(host_name)
        if keys:
            async def _delete_all_clients_strict():
                tasks = [
                    xui_api.delete_client_on_host(host_name, key.get('key_email'))
                    for key in keys
                    if key.get('key_email')
                ]
                if not tasks:
                    return True
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=60
                )
                all_ok = True
                for result in results:
                    if isinstance(result, Exception):
                        logger.error(f"Error deleting client during host removal: {result}", exc_info=True)
                        all_ok = False
                    elif result is False:
                        all_ok = False
                return all_ok

            loop = current_app.config.get('EVENT_LOOP')
            try:
                if loop and loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(_delete_all_clients_strict(), loop)
                    all_ok = future.result(timeout=65)
                else:
                    all_ok = asyncio.run(_delete_all_clients_strict())
            except Exception as e:
                logger.error(f"Failed to delete clients from host '{host_name}': {e}", exc_info=True)
                all_ok = False

            if not all_ok:
                flash("Удаление остановлено: не все клиенты удалены из 3x-ui. Хост не удалён.", "danger")
                return redirect(url_for('settings_page'))

        delete_host(host_name)
        flash(f"Хост '{host_name}' и все его тарифы были удалены.", 'success')
        return redirect(url_for('settings_page'))

    @flask_app.route('/add-plan', methods=['POST'])
    @login_required
    def add_plan_route():
        create_plan(
            host_name=request.form['host_name'],
            plan_name=request.form['plan_name'],
            months=int(request.form['months']),
            price=float(request.form['price'])
        )
        flash(f"Новый тариф для хоста '{request.form['host_name']}' добавлен.", 'success')
        return redirect(url_for('settings_page'))

    @flask_app.route('/delete-plan/<int:plan_id>', methods=['POST'])
    @login_required
    def delete_plan_route(plan_id):
        delete_plan(plan_id)
        flash("Тариф успешно удален.", 'success')
        return redirect(url_for('settings_page'))

    @flask_app.route('/yookassa-webhook', methods=['POST'])
    def yookassa_webhook_handler():
        try:
            shop_id = get_setting("yookassa_shop_id")
            secret_key = get_setting("yookassa_secret_key")

            if not shop_id or not secret_key:
                logger.error("YooKassa Webhook: Shop ID or Secret Key not configured. Rejecting request.")
                return 'Forbidden', 403

            event_json = request.json
            if event_json.get("event") == "payment.succeeded":
                obj = event_json.get("object", {})
                metadata = obj.get("metadata", {})
                payment_id = obj.get("id")

                if payment_id:
                    if _is_webhook_processed("yookassa", payment_id):
                        return 'OK', 200

                    Configuration.account_id = shop_id
                    Configuration.secret_key = secret_key

                    try:
                        payment = Payment.find_one(payment_id)
                        if not payment or getattr(payment, 'status', None) != 'succeeded':
                            logger.warning(f"YooKassa webhook: Payment {payment_id} is not succeeded according to API.")
                            return 'OK', 200
                    except Exception as e:
                        logger.error(f"YooKassa webhook: API verification failed for payment {payment_id}: {e}")
                        return 'Error', 500

                    _set_webhook_processed("yookassa", payment_id)

                bot = _bot_controller.get_bot_instance()
                payment_processor = handlers.process_successful_payment

                if metadata and bot is not None and payment_processor is not None:
                    loop = current_app.config.get('EVENT_LOOP')
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(payment_processor(bot, metadata), loop)
                    else:
                        logger.error("YooKassa webhook: Event loop is not available!")
            return 'OK', 200
        except Exception as e:
            logger.error(f"Error in yookassa webhook handler: {e}", exc_info=True)
            return 'Error', 500

    def _cryptobot_webhook_handler_impl(secret_token: str | None = None):
        try:
            configured_secret = get_setting("cryptobot_webhook_secret")
            if not configured_secret:
                logger.error("CryptoBot Webhook: Secret not configured. Rejecting request for security.")
                return 'Forbidden', 403

            if configured_secret:
                if not secret_token or not compare_digest(str(secret_token), str(configured_secret)):
                    logger.warning("CryptoBot Webhook: Invalid or missing secret token.")
                    return 'Forbidden', 403

            request_data = request.json

            if request_data and request_data.get('update_type') == 'invoice_paid':
                payload_data = request_data.get('payload', {})

                invoice_status = payload_data.get('status')
                if invoice_status and invoice_status != 'paid':
                    logger.warning(f"CryptoBot Webhook: invoice_paid update but status={invoice_status}. Ignoring.")
                    return 'OK', 200

                external_invoice_id = payload_data.get('invoice_id')
                if external_invoice_id and _is_webhook_processed("cryptobot", str(external_invoice_id)):
                    return 'OK', 200

                payload_string = payload_data.get('payload')

                if not payload_string:
                    logger.warning("CryptoBot Webhook: Received paid invoice but payload was empty.")
                    return 'OK', 200

                external_id_fallback = None
                if not external_invoice_id:
                    external_id_fallback = hashlib.sha256(payload_string.encode('utf-8')).hexdigest()
                    if _is_webhook_processed("cryptobot", external_id_fallback):
                        return 'OK', 200

                parts = payload_string.split(':')
                if len(parts) < 9:
                    logger.error(f"cryptobot Webhook: Invalid payload format received: {payload_string}")
                    return 'Error', 400

                metadata = {
                    "user_id": parts[0],
                    "months": parts[1],
                    "price": parts[2],
                    "action": parts[3],
                    "key_id": parts[4],
                    "host_name": parts[5],
                    "plan_id": parts[6],
                    "customer_email": parts[7] if parts[7] != 'None' else None,
                    "payment_method": parts[8]
                }

                if external_invoice_id:
                    _set_webhook_processed("cryptobot", str(external_invoice_id))
                elif external_id_fallback:
                    _set_webhook_processed("cryptobot", external_id_fallback)

                bot = _bot_controller.get_bot_instance()
                loop = current_app.config.get('EVENT_LOOP')
                payment_processor = handlers.process_successful_payment

                if bot and loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(payment_processor(bot, metadata), loop)
                else:
                    logger.error("cryptobot Webhook: Could not process payment because bot or event loop is not running.")

            return 'OK', 200

        except Exception as e:
            logger.error(f"Error in cryptobot webhook handler: {e}", exc_info=True)
            return 'Error', 500

    @flask_app.route('/cryptobot-webhook', methods=['POST'])
    def cryptobot_webhook_handler():
        configured_secret = get_setting("cryptobot_webhook_secret")
        if configured_secret:
            logger.warning("CryptoBot Webhook: Secret is configured; use /cryptobot-webhook/<token>.")
            return 'Forbidden', 403
        return _cryptobot_webhook_handler_impl(secret_token=None)

    @flask_app.route('/cryptobot-webhook/<token>', methods=['POST'])
    def cryptobot_webhook_handler_with_token(token: str):
        return _cryptobot_webhook_handler_impl(secret_token=token)

    @flask_app.route('/heleket-webhook', methods=['POST'])
    def heleket_webhook_handler():
        try:
            data = request.json
            logger.info(f"Received Heleket webhook: {data}")

            api_key = get_setting("heleket_api_key")
            if not api_key: return 'Error', 500

            sign = data.pop("sign", None)
            if not sign: return 'Error', 400
                
            sorted_data_str = json.dumps(data, sort_keys=True, separators=(",", ":"))
            
            base64_encoded = base64.b64encode(sorted_data_str.encode()).decode()
            raw_string = f"{base64_encoded}{api_key}"
            expected_sign = hashlib.md5(raw_string.encode()).hexdigest()

            if not compare_digest(expected_sign, sign):
                logger.warning("Heleket webhook: Invalid signature.")
                return 'Forbidden', 403

            if data.get('status') in ["paid", "paid_over"]:
                metadata_str = data.get('description')
                if not metadata_str: return 'Error', 400
                
                # Generate unique ID for idempotency check
                # Use order_id or invoice_id from Heleket if available, otherwise hash metadata
                external_id = data.get('order_id') or data.get('uuid') or hashlib.sha256(metadata_str.encode('utf-8')).hexdigest()
                
                if _is_webhook_processed("heleket", str(external_id)):
                    logger.info(f"Heleket webhook: Payment {external_id} already processed, skipping.")
                    return 'OK', 200
                
                metadata = json.loads(metadata_str)
                
                _set_webhook_processed("heleket", str(external_id))
                
                bot = _bot_controller.get_bot_instance()
                loop = current_app.config.get('EVENT_LOOP')
                payment_processor = handlers.process_successful_payment

                if bot and loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(payment_processor(bot, metadata), loop)
            
            return 'OK', 200
        except Exception as e:
            logger.error(f"Error in heleket webhook handler: {e}", exc_info=True)
            return 'Error', 500

    @flask_app.route('/settings/toggle_global_plans', methods=['POST'])
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
        flash(f"Global plans {'enabled' if new_status == 'true' else 'disabled'}.", "success")
        return redirect(url_for('settings_page'))

    @flask_app.route('/ton-webhook', methods=['POST'])
    def ton_webhook_handler():
        try:
            data = request.json
            logger.info(f"Received TonAPI webhook: {data}")

            # Safe verification via TonAPI
            tonapi_key = get_setting("tonapi_key")
            if not tonapi_key:
                logger.error("TON Webhook: tonapi_key is not configured")
                return 'Error', 500

            # Extract tx_hash from webhook (depends on webhook structure, supporting both likely formats)
            tx_hash = data.get('tx_hash')
            if not tx_hash and 'events' in data: # Some formats use events
                for event in data['events']:
                     if 'tx_hash' in event:
                         tx_hash = event['tx_hash']
                         break
            
            # If simplistic format from original code is assumed (nested txs), try finding a hash there
            if not tx_hash:
                 for tx in data.get('in_progress_txs', []) + data.get('txs', []):
                     if 'hash' in tx:
                         tx_hash = tx['hash']
                         break

            if not tx_hash:
                logger.error("TON Webhook: Could not find tx_hash in webhook data")
                return 'OK', 200

            import urllib.request
            import urllib.error

            # Verify transaction with TonAPI
            url = f"https://tonapi.io/v2/blockchain/transactions/{tx_hash}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tonapi_key}"})
            
            try:
                with urllib.request.urlopen(req) as response:
                    real_tx_data = json.loads(response.read().decode())
            except urllib.error.HTTPError as e:
                logger.error(f"TON Webhook: Verification failed. Could not fetch tx {tx_hash} from TonAPI: {e}")
                return 'OK', 200 # Don't retry if it doesn't exist
            
            # Now process details from REAL data (trusted), not webhook data
            in_msg = real_tx_data.get('in_msg')
            if in_msg and in_msg.get('decoded_body'):
                 # TonAPI returns 'decoded_body' which is often the comment object or text
                 # Check 'comment' field in decoded_body or raw 'message' if provided
                 comment = in_msg.get('decoded_body', {}).get('text')
            elif in_msg and in_msg.get('message'):
                 comment = in_msg.get('message')
            else:
                 # Fallback if comment is structure directly
                 comment = in_msg.get('decoded_body') if isinstance(in_msg.get('decoded_body'), str) else None

            if comment:
                payment_id = comment
                amount_nano = int(in_msg.get('value', 0))
                amount_ton = float(amount_nano / 1_000_000_000)

                metadata = find_and_complete_ton_transaction(payment_id, amount_ton)
                
                if metadata:
                    logger.info(f"TON Payment successful (Verified) for payment_id: {payment_id}")
                    bot = _bot_controller.get_bot_instance()
                    loop = current_app.config.get('EVENT_LOOP')
                    payment_processor = handlers.process_successful_payment

                    if bot and loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(payment_processor(bot, metadata), loop)
            
            return 'OK', 200
        except Exception as e:
            logger.error(f"Error in ton webhook handler: {e}", exc_info=True)
            return 'Error', 500

    return flask_app

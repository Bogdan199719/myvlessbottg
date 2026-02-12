import sqlite3
from datetime import datetime
from shop_bot.utils import time_utils
import logging
from pathlib import Path
import json
import os
import uuid

logger = logging.getLogger(__name__)

# Use environment variable for DB path or default to the current working directory
# In Docker, os.getcwd() will be /app/project
DEFAULT_DB_PATH = Path(os.getcwd()) / "users.db"
DB_FILE = Path(os.getenv("DB_PATH", DEFAULT_DB_PATH))

def initialize_db():
    try:
        # Ensure directory exists
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY, username TEXT, total_spent REAL DEFAULT 0,
                    total_months INTEGER DEFAULT 0, trial_used BOOLEAN DEFAULT 0,
                    agreed_to_terms BOOLEAN DEFAULT 0,
                    registration_date TIMESTAMP,
                    is_banned BOOLEAN DEFAULT 0,
                    referred_by INTEGER,
                    referral_balance REAL DEFAULT 0,
                    referral_balance_all REAL DEFAULT 0,
                    subscription_token TEXT UNIQUE
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS vpn_keys_missing (
                    key_email TEXT PRIMARY KEY,
                    host_name TEXT,
                    first_seen TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS vpn_keys (
                    key_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    host_name TEXT NOT NULL,
                    xui_client_uuid TEXT NOT NULL,
                    key_email TEXT NOT NULL UNIQUE,
                    expiry_date TIMESTAMP,
                    created_date TIMESTAMP,
                    connection_string TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    username TEXT,
                    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    amount_rub REAL NOT NULL,
                    amount_currency REAL,
                    currency_name TEXT,
                    payment_method TEXT,
                    metadata TEXT,
                    created_date TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS support_threads (
                    user_id INTEGER PRIMARY KEY,
                    thread_id INTEGER NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS xui_hosts(
                    host_name TEXT NOT NULL,
                    host_url TEXT NOT NULL,
                    host_username TEXT NOT NULL,
                    host_pass TEXT NOT NULL,
                    host_inbound_id INTEGER NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_name TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    months INTEGER NOT NULL,
                    price REAL NOT NULL
                )
            ''')            
            default_settings = {
                "panel_login": os.getenv("PANEL_LOGIN", "admin"),
                "panel_password": os.getenv("PANEL_PASSWORD", "admin"),
                "about_text": None,
                "terms_url": None,
                "privacy_url": None,
                "support_user": None,
                "support_text": None,
                "channel_url": None,
                "force_subscription": "true",
                "receipt_email": "example@example.com",
                "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
                "support_bot_token": os.getenv("SUPPORT_BOT_TOKEN"),
                "telegram_bot_username": os.getenv("TELEGRAM_BOT_USERNAME"),
                "trial_enabled": "true",
                "trial_duration_days": "3",
                "trial_duration_value": "24",
                "trial_duration_unit": "hours",
                "trial_host_name": None,
                "enable_referrals": "true",
                "referral_percentage": "10",
                "referral_discount": "5",
                "minimum_withdrawal": "100",
                "support_group_id": None,
                "admin_telegram_id": os.getenv("ADMIN_TELEGRAM_ID"),
                "yookassa_shop_id": os.getenv("YOOKASSA_SHOP_ID"),
                "yookassa_secret_key": os.getenv("YOOKASSA_SECRET_KEY"),
                "yookassa_enabled": os.getenv("YOOKASSA_ENABLED", "false"),
                "sbp_enabled": "false",
                "stars_enabled": "false",
                "stars_rub_per_star": "0",
                "cryptobot_enabled": os.getenv("CRYPTOBOT_ENABLED", "false"),
                "cryptobot_token": os.getenv("CRYPTOBOT_TOKEN"),
                "cryptobot_webhook_secret": os.getenv("CRYPTOBOT_WEBHOOK_SECRET"),
                "heleket_enabled": os.getenv("HELEKET_ENABLED", "false"),
                "heleket_merchant_id": os.getenv("HELEKET_MERCHANT_ID"),
                "heleket_api_key": os.getenv("HELEKET_API_KEY"),
                "domain": os.getenv("DOMAIN"),
                "tonconnect_enabled": os.getenv("TONCONNECT_ENABLED", "false"),
                "ton_wallet_address": os.getenv("TON_WALLET_ADDRESS"),
                "tonapi_key": os.getenv("TONAPI_KEY"),
                "p2p_enabled": "false",
                "p2p_card_number": None,
                "subscription_live_sync": "false",
                "subscription_live_stats": "false",
                "subscription_allow_fallback_host_fetch": "false",
                "subscription_auto_provision": "false",
                "panel_sync_enabled": "false",
                "xtls_sync_enabled": "false",
            }
            run_migration()
            for key, value in default_settings.items():
                cursor.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
            logging.info(f"Database initialized successfully at {DB_FILE}")
            
        # Clear any stale pending payment flags on startup
        clear_all_pending_payments()
    except sqlite3.Error as e:
        logging.error(f"Database error on initialization: {e}")

def run_migration():
    if not DB_FILE.exists():
        logging.error("Users.db database file was not found. There is nothing to migrate.")
        return

    logging.info(f"Starting the migration of the database: {DB_FILE}")

    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()

            logging.info("The migration of the table 'users' ...")
        
            cursor.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if 'referred_by' not in columns:
                cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
                logging.info(" -> The column 'referred_by' is successfully added.")
            else:
                logging.info(" -> The column 'referred_by' already exists.")
                
            if 'referral_balance' not in columns:
                cursor.execute("ALTER TABLE users ADD COLUMN referral_balance REAL DEFAULT 0")
                logging.info(" -> The column 'referral_balance' is successfully added.")
            else:
                logging.info(" -> The column 'referral_balance' already exists.")
            
            if 'referral_balance_all' not in columns:
                cursor.execute("ALTER TABLE users ADD COLUMN referral_balance_all REAL DEFAULT 0")
                logging.info(" -> The column 'referral_balance_all' is successfully added.")
            else:
                logging.info(" -> The column 'referral_balance_all' already exists.")

            if 'subscription_token' not in columns:
                cursor.execute("ALTER TABLE users ADD COLUMN subscription_token TEXT")
                logging.info(" -> The column 'subscription_token' is successfully added.")
                
                # Generate tokens for existing users
                cursor.execute("SELECT telegram_id FROM users WHERE subscription_token IS NULL")
                users_without_token = cursor.fetchall()
                for (uid,) in users_without_token:
                    new_token = str(uuid.uuid4())
                    cursor.execute("UPDATE users SET subscription_token = ? WHERE telegram_id = ?", (new_token, uid))
                
                # Create unique index after populating
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_subscription_token ON users (subscription_token)")
                logging.info(f" -> Generated subscription tokens for {len(users_without_token)} existing users and created unique index.")
            else:
                logging.info(" -> The column 'subscription_token' already exists.")
                # Ensure unique index exists even if column was created earlier
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_subscription_token ON users (subscription_token)")
                # Backfill missing tokens if any users have NULL/empty values
                cursor.execute("SELECT telegram_id FROM users WHERE subscription_token IS NULL OR subscription_token = ''")
                users_without_token = cursor.fetchall()
                for (uid,) in users_without_token:
                    new_token = str(uuid.uuid4())
                    cursor.execute("UPDATE users SET subscription_token = ? WHERE telegram_id = ?", (new_token, uid))
                if users_without_token:
                    logging.info(f" -> Backfilled subscription tokens for {len(users_without_token)} users.")

            # Check for is_enabled column in xui_hosts
            cursor.execute("PRAGMA table_info(xui_hosts)")
            host_columns = [row[1] for row in cursor.fetchall()]
            if 'is_enabled' not in host_columns:
                cursor.execute("ALTER TABLE xui_hosts ADD COLUMN is_enabled BOOLEAN DEFAULT 1")
                logging.info(" -> The column 'is_enabled' is successfully added to xui_hosts.")
            else:
                logging.info(" -> The column 'is_enabled' already exists in xui_hosts.")

            cursor.execute("PRAGMA table_info(vpn_keys)")
            vpn_keys_columns = [row[1] for row in cursor.fetchall()]
            if 'connection_string' not in vpn_keys_columns:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN connection_string TEXT")
                logging.info(" -> The column 'connection_string' is successfully added to vpn_keys.")
            else:
                logging.info(" -> The column 'connection_string' already exists in vpn_keys.")
            
            # Migrate new payment method toggle settings
            logging.info("Migration of bot_settings for payment methods...")
            new_payment_settings = {
               "yookassa_enabled": "false",
                "cryptobot_enabled": "false",
                "heleket_enabled": "false",
                "tonconnect_enabled": "false"
            }
            
            for key, default_value in new_payment_settings.items():
                cursor.execute("SELECT 1 FROM bot_settings WHERE key = ?", (key,))
                if not cursor.fetchone():
                    cursor.execute("INSERT INTO bot_settings (key, value) VALUES (?, ?)", (key, default_value))
                    logging.info(f" -> Added setting '{key}' with default value '{default_value}'.")
                else:
                    logging.info(f" -> Setting '{key}' already exists.")
            
            # Add plan_id column to vpn_keys for trial/paid key distinction
            logging.info("Migration of vpn_keys table to add plan_id...")
            cursor.execute("PRAGMA table_info(vpn_keys)")
            vpn_keys_columns = [row[1] for row in cursor.fetchall()]
            if 'plan_id' not in vpn_keys_columns:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN plan_id INTEGER DEFAULT 0")
                logging.info(" -> The column 'plan_id' is successfully added to vpn_keys.")
                logging.info(" -> Existing keys will have plan_id=0 (trial). Update manually if needed.")
            else:
                logging.info(" -> The column 'plan_id' already exists in vpn_keys.")
            
            # Add pending_payment flag to prevent race conditions
            logging.info("Migration of users table to add pending_payment protection...")
            cursor.execute("PRAGMA table_info(users)")
            user_columns = [row[1] for row in cursor.fetchall()]
            if 'pending_payment' not in user_columns:
                cursor.execute("ALTER TABLE users ADD COLUMN pending_payment BOOLEAN DEFAULT 0")
                logging.info(" -> The column 'pending_payment' is successfully added for race condition protection.")
            else:
                logging.info(" -> The column 'pending_payment' already exists in users.")
            
            # Create indexes for performance
            logging.info("Creating performance indexes...")
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_vpn_keys_user_id ON vpn_keys(user_id)")
                logging.info(" -> Index on vpn_keys.user_id created.")
            except sqlite3.OperationalError:
                logging.info(" -> Index on vpn_keys.user_id already exists.")
            
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_vpn_keys_expiry ON vpn_keys(expiry_date)")
                logging.info(" -> Index on vpn_keys.expiry_date created.")
            except sqlite3.OperationalError:
                logging.info(" -> Index on vpn_keys.expiry_date already exists.")
            
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id)")
                logging.info(" -> Index on transactions.user_id created.")
            except sqlite3.OperationalError:
                logging.info(" -> Index on transactions.user_id already exists.")
            
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned)")
                logging.info(" -> Index on users.is_banned created.")
            except sqlite3.OperationalError:
                logging.info(" -> Index on users.is_banned already exists.")
            
            logging.info("The table 'users' has been successfully updated.")

            logging.info("The migration of the table 'Transactions' ...")

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'")
            table_exists = cursor.fetchone()

            if table_exists:
                cursor.execute("PRAGMA table_info(transactions)")
                trans_columns = [row[1] for row in cursor.fetchall()]
                
                if 'payment_id' in trans_columns and 'status' in trans_columns and 'username' in trans_columns:
                    logging.info("The 'Transactions' table already has a new structure. Migration is not required.")
                else:
                    backup_name = f"transactions_backup_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                    logging.warning(f"The old structure of the TRANSACTIONS table was discovered. I rename in '{backup_name}' ...")
                    cursor.execute(f"ALTER TABLE transactions RENAME TO {backup_name}")
                    
                    logging.info("I create a new table 'Transactions' with the correct structure ...")
                    create_new_transactions_table(cursor)
                    logging.info("The new table 'Transactions' has been successfully created. The old data is saved.")
            else:
                logging.info("TRANSACTIONS table was not found. I create a new one ...")
                create_new_transactions_table(cursor)
                logging.info("The new table 'Transactions' has been successfully created.")

            logging.info("The migration of the table 'sent_notifications' ...")
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sent_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    key_id INTEGER,
                    notification_type TEXT NOT NULL,
                    hours_mark INTEGER,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            logging.info(" -> Table 'sent_notifications' is ready.")

            conn.commit()
        
        logging.info("--- The database is successfully completed! ---")

    except sqlite3.Error as e:
        logging.error(f"An error occurred during migration: {e}")

def create_new_transactions_table(cursor: sqlite3.Cursor):
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            username TEXT,
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            amount_rub REAL NOT NULL,
            amount_currency REAL,
            currency_name TEXT,
            payment_method TEXT,
            metadata TEXT,
            created_date TIMESTAMP
        )
    ''')

def create_host(name: str, url: str, user: str, passwd: str, inbound: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO xui_hosts (host_name, host_url, host_username, host_pass, host_inbound_id, is_enabled) VALUES (?, ?, ?, ?, ?, 1)",
                (name, url, user, passwd, inbound)
            )
            conn.commit()
            logging.info(f"Host '{name}' added.")
    except sqlite3.Error as e:
        logging.error(f"Failed to add host: {e}")

def update_host(old_name: str, new_name: str, url: str, user: str, passwd: str, inbound: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # If password is empty, don't update it
            if passwd:
                cursor.execute(
                    """
                    UPDATE xui_hosts 
                    SET host_name=?, host_url=?, host_username=?, host_pass=?, host_inbound_id=?
                    WHERE host_name=?
                    """,
                    (new_name, url, user, passwd, inbound, old_name)
                )
            else:
                 cursor.execute(
                    """
                    UPDATE xui_hosts 
                    SET host_name=?, host_url=?, host_username=?, host_inbound_id=?
                    WHERE host_name=?
                    """,
                    (new_name, url, user, inbound, old_name)
                )
            
            # Also update related plans and keys if name changed
            if old_name != new_name:
                cursor.execute("UPDATE plans SET host_name=? WHERE host_name=?", (new_name, old_name))
                cursor.execute("UPDATE vpn_keys SET host_name=? WHERE host_name=?", (new_name, old_name))
                cursor.execute("UPDATE vpn_keys_missing SET host_name=? WHERE host_name=?", (new_name, old_name))
                
            conn.commit()
            logging.info(f"Host '{old_name}' updated to '{new_name}'.")
    except sqlite3.Error as e:
        logging.error(f"Failed to update host '{old_name}': {e}")

def toggle_host_status(host_name: str, is_enabled: bool):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE xui_hosts SET is_enabled=? WHERE host_name=?", (1 if is_enabled else 0, host_name))
            conn.commit()
            logging.info(f"Host '{host_name}' status set to {is_enabled}.")
    except sqlite3.Error as e:
        logging.error(f"Failed to toggle status for host '{host_name}': {e}")

def delete_host(host_name: str):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM xui_hosts WHERE host_name = ?", (host_name,))
            cursor.execute("DELETE FROM plans WHERE host_name = ?", (host_name,))
            cursor.execute("DELETE FROM vpn_keys WHERE host_name = ?", (host_name,))
            cursor.execute("DELETE FROM vpn_keys_missing WHERE host_name = ?", (host_name,))
            conn.commit()
            logging.info(f"Host '{host_name}' deleted.")
    except sqlite3.Error as e:
        logging.error(f"Failed to delete host '{host_name}': {e}")

def get_host(host_name: str) -> dict | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM xui_hosts WHERE host_name = ?", (host_name,))
            result = cursor.fetchone()
            return dict(result) if result else None
    except sqlite3.Error as e:
        logging.error(f"Error getting host '{host_name}': {e}")
        return None

def get_all_hosts(only_enabled: bool = False) -> list[dict]:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if only_enabled:
                 cursor.execute("SELECT * FROM xui_hosts WHERE is_enabled = 1")
            else:
                 cursor.execute("SELECT * FROM xui_hosts")
            hosts = cursor.fetchall()
            return [dict(row) for row in hosts]
    except sqlite3.Error as e:
        logging.error(f"Error getting list of all hosts: {e}")
        return []

def get_all_keys_with_usernames() -> list[dict]:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT k.*, u.username, 
                       (julianday(k.expiry_date) - julianday('now')) as days_left
                FROM vpn_keys k
                LEFT JOIN users u ON k.user_id = u.telegram_id
                ORDER BY k.created_date DESC
            ''')
            results = [dict(row) for row in cursor.fetchall()]
            
            # Post-process days_left for better display
            for res in results:
                if res['days_left'] is not None:
                     res['days_left'] = int(res['days_left'])
            
            return results
    except sqlite3.Error as e:
        logging.error(f"Failed to get all keys with usernames: {e}")
        return []

def get_all_keys() -> list[dict]:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys")
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get all keys: {e}")
        return []

def get_setting(key: str) -> str | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
            result = cursor.fetchone()
            return result[0] if result else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get setting '{key}': {e}")
        return None
        
def get_all_settings() -> dict:
    settings = {}
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM bot_settings")
            rows = cursor.fetchall()
            for row in rows:
                settings[row['key']] = row['value']
    except sqlite3.Error as e:
        logging.error(f"Failed to get all settings: {e}")
    return settings

def update_setting(key: str, value: str):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
            logging.info(f"Setting '{key}' updated.")
    except sqlite3.Error as e:
        logging.error(f"Failed to update setting '{key}': {e}")

def create_plan(host_name: str, plan_name: str, months: int, price: float):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO plans (host_name, plan_name, months, price) VALUES (?, ?, ?, ?)",
                (host_name, plan_name, months, price)
            )
            conn.commit()
            logging.info(f"Created new plan '{plan_name}' for host '{host_name}'.")
    except sqlite3.Error as e:
        logging.error(f"Failed to create plan for host '{host_name}': {e}")

def get_plans_for_host(host_name: str) -> list[dict]:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM plans WHERE host_name = ? ORDER BY months", (host_name,))
            plans = cursor.fetchall()
            return [dict(plan) for plan in plans]
    except sqlite3.Error as e:
        logging.error(f"Failed to get plans for host '{host_name}': {e}")
        return []

def get_plan_by_id(plan_id: int) -> dict | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,))
            plan = cursor.fetchone()
            return dict(plan) if plan else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get plan by id '{plan_id}': {e}")
        return None

def delete_plan(plan_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
            conn.commit()
            logging.info(f"Deleted plan with id {plan_id}.")
    except sqlite3.Error as e:
        logging.error(f"Failed to delete plan with id {plan_id}: {e}")

def register_user_if_not_exists(telegram_id: int, username: str, referrer_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT telegram_id, subscription_token FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cursor.fetchone()
            if not row:
                token = str(uuid.uuid4())
                cursor.execute(
                    "INSERT INTO users (telegram_id, username, registration_date, referred_by, subscription_token) VALUES (?, ?, ?, ?, ?)",
                    (telegram_id, username, time_utils.get_msk_now(), referrer_id, token)
                )
            else:
                cursor.execute("UPDATE users SET username = ? WHERE telegram_id = ?", (username, telegram_id))
                # Ensure legacy users have a subscription token
                existing_token = row[1] if len(row) > 1 else None
                if not existing_token:
                    new_token = str(uuid.uuid4())
                    cursor.execute(
                        "UPDATE users SET subscription_token = ? WHERE telegram_id = ?",
                        (new_token, telegram_id)
                    )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to register user {telegram_id}: {e}")

def get_or_create_subscription_token(telegram_id: int) -> str | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT subscription_token FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
            new_token = str(uuid.uuid4())
            cursor.execute(
                "UPDATE users SET subscription_token = ? WHERE telegram_id = ?",
                (new_token, telegram_id)
            )
            conn.commit()
            return new_token
    except sqlite3.Error as e:
        logging.error(f"Failed to get/create subscription token for user {telegram_id}: {e}")
        return None

def add_to_referral_balance(user_id: int, amount: float):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET referral_balance = referral_balance + ? WHERE telegram_id = ?", (amount, user_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to add to referral balance for user {user_id}: {e}")

def set_referral_balance(user_id: int, value: float):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET referral_balance = ? WHERE telegram_id = ?", (value, user_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to set referral balance for user {user_id}: {e}")

def set_referral_balance_all(user_id: int, value: float):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET referral_balance_all = ? WHERE telegram_id = ?", (value, user_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to set total referral balance for user {user_id}: {e}")

def get_referral_balance(user_id: int) -> float:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT referral_balance FROM users WHERE telegram_id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else 0.0
    except sqlite3.Error as e:
        logging.error(f"Failed to get referral balance for user {user_id}: {e}")
        return 0.0

def get_referral_count(user_id: int) -> int:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,))
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error(f"Failed to get referral count for user {user_id}: {e}")
        return 0

def get_user(telegram_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            user_data = cursor.fetchone()
            return dict(user_data) if user_data else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get user {telegram_id}: {e}")
        return None

def get_user_by_token(token: str):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE subscription_token = ?", (token,))
            user_data = cursor.fetchone()
            return dict(user_data) if user_data else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get user by token: {e}")
        return None

def set_terms_agreed(telegram_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET agreed_to_terms = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            logging.info(f"User {telegram_id} has agreed to terms.")
    except sqlite3.Error as e:
        logging.error(f"Failed to set terms agreed for user {telegram_id}: {e}")

def update_user_stats(telegram_id: int, amount_spent: float, months_purchased: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET total_spent = total_spent + ?, total_months = total_months + ? WHERE telegram_id = ?", (amount_spent, months_purchased, telegram_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update user stats for {telegram_id}: {e}")

def get_user_count() -> int:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error(f"Failed to get user count: {e}")
        return 0

def get_total_keys_count() -> int:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM vpn_keys")
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error(f"Failed to get total keys count: {e}")
        return 0

def get_total_spent_sum() -> float:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(total_spent) FROM users")
            return cursor.fetchone()[0] or 0.0
    except sqlite3.Error as e:
        logging.error(f"Failed to get total spent sum: {e}")
        return 0.0

def create_pending_transaction(payment_id: str, user_id: int, amount_rub: float, metadata: dict) -> int:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO transactions (payment_id, user_id, status, amount_rub, metadata) VALUES (?, ?, ?, ?, ?)",
                (payment_id, user_id, 'pending', amount_rub, json.dumps(metadata))
            )
            conn.commit()
            return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Failed to create pending transaction: {e}")
        return 0

def find_and_complete_ton_transaction(payment_id: str, amount_ton: float) -> dict | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT * FROM transactions WHERE payment_id = ? AND status = 'pending'", (payment_id,))
            transaction = cursor.fetchone()
            if not transaction:
                logger.warning(f"TON Webhook: Received payment for unknown or completed payment_id: {payment_id}")
                return None
            
            
            cursor.execute(
                "UPDATE transactions SET status = 'paid', amount_currency = ?, currency_name = 'TON', payment_method = 'TON' WHERE payment_id = ?",
                (amount_ton, payment_id)
            )
            conn.commit()
            
            return json.loads(transaction['metadata'])
    except sqlite3.Error as e:
        logging.error(f"Failed to complete TON transaction {payment_id}: {e}")
        return None

def log_transaction(username: str, transaction_id: str | None, payment_id: str | None, user_id: int, status: str, amount_rub: float, amount_currency: float | None, currency_name: str | None, payment_method: str, metadata: str):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO transactions
                   (username, transaction_id, payment_id, user_id, status, amount_rub, amount_currency, currency_name, payment_method, metadata, created_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (username, transaction_id, payment_id, user_id, status, amount_rub, amount_currency, currency_name, payment_method, metadata, time_utils.get_msk_now())
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to log transaction for user {user_id}: {e}")

def get_paginated_transactions(page: int = 1, per_page: int = 15) -> tuple[list[dict], int]:
    offset = (page - 1) * per_page
    transactions = []
    total = 0
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) FROM transactions")
            total = cursor.fetchone()[0]

            query = "SELECT * FROM transactions ORDER BY created_date DESC LIMIT ? OFFSET ?"
            cursor.execute(query, (per_page, offset))
            
            for row in cursor.fetchall():
                transaction_dict = dict(row)
                
                metadata_str = transaction_dict.get('metadata')
                if metadata_str:
                    try:
                        metadata = json.loads(metadata_str)
                        transaction_dict['host_name'] = metadata.get('host_name', 'N/A')
                        transaction_dict['plan_name'] = metadata.get('plan_name', 'N/A')
                    except json.JSONDecodeError:
                        transaction_dict['host_name'] = 'Error'
                        transaction_dict['plan_name'] = 'Error'
                else:
                    transaction_dict['host_name'] = 'N/A'
                    transaction_dict['plan_name'] = 'N/A'

                try:
                    expiry_query = "SELECT MAX(expiry_date) FROM vpn_keys WHERE user_id = ?"
                    expiry_params = [transaction_dict.get('user_id')]
                    host_name = transaction_dict.get('host_name')
                    if host_name and host_name not in ('N/A', 'Error'):
                        expiry_query += " AND host_name = ?"
                        expiry_params.append(host_name)

                    cursor.execute(expiry_query, tuple(expiry_params))
                    transaction_dict['subscription_expires_at'] = cursor.fetchone()[0]
                except sqlite3.Error:
                    transaction_dict['subscription_expires_at'] = None
                
                transactions.append(transaction_dict)
            
    except sqlite3.Error as e:
        logging.error(f"Failed to get paginated transactions: {e}")
    
    return transactions, total

def set_trial_used(telegram_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET trial_used = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            logging.info(f"Trial period marked as used for user {telegram_id}.")
    except sqlite3.Error as e:
        logging.error(f"Failed to set trial used for user {telegram_id}: {e}")

def add_new_key(user_id: int, host_name: str, xui_client_uuid: str, key_email: str, expiry_timestamp_ms: int, connection_string: str = None, plan_id: int = 0):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            expiry_date = time_utils.from_timestamp_ms(expiry_timestamp_ms)
            cursor.execute(
                "INSERT INTO vpn_keys (user_id, host_name, xui_client_uuid, key_email, expiry_date, connection_string, plan_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, host_name, xui_client_uuid, key_email, expiry_date, connection_string, plan_id)
            )
            new_key_id = cursor.lastrowid
            
            # Ensure key is removed from missing list if it was there
            cursor.execute("DELETE FROM vpn_keys_missing WHERE key_email = ?", (key_email,))
            
            conn.commit()
            return new_key_id
    except sqlite3.IntegrityError as e:
        message = str(e)
        if "UNIQUE constraint failed: vpn_keys.key_email" in message:
            existing_key = get_key_by_email(key_email)
            if existing_key:
                # Key already exists: update it instead of failing
                updated = update_key_by_email(
                    key_email=key_email,
                    host_name=host_name,
                    xui_client_uuid=xui_client_uuid,
                    expiry_timestamp_ms=expiry_timestamp_ms,
                    connection_string=connection_string,
                    plan_id=plan_id
                )
                if updated:
                    return existing_key.get('key_id')
            logging.warning(f"Key '{key_email}' already exists but could not be updated.")
            return None
        logging.error(f"Failed to add new key for user {user_id}: {e}")
        return None
    except sqlite3.Error as e:
        logging.error(f"Failed to add new key for user {user_id}: {e}")
        return None

def update_key_by_email(key_email: str, host_name: str, xui_client_uuid: str, expiry_timestamp_ms: int, connection_string: str | None = None, plan_id: int = 0):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            expiry_date = time_utils.from_timestamp_ms(expiry_timestamp_ms)
            if connection_string:
                cursor.execute(
                    "UPDATE vpn_keys SET host_name = ?, xui_client_uuid = ?, expiry_date = ?, connection_string = ?, plan_id = ? WHERE key_email = ?",
                    (host_name, xui_client_uuid, expiry_date, connection_string, plan_id, key_email)
                )
            else:
                cursor.execute(
                    "UPDATE vpn_keys SET host_name = ?, xui_client_uuid = ?, expiry_date = ?, plan_id = ? WHERE key_email = ?",
                    (host_name, xui_client_uuid, expiry_date, plan_id, key_email)
                )
            cursor.execute("DELETE FROM vpn_keys_missing WHERE key_email = ?", (key_email,))
            conn.commit()
            return True
    except sqlite3.Error as e:
        logging.error(f"Failed to update key by email {key_email}: {e}")
        return False

def delete_key_by_email(email: str):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM vpn_keys WHERE key_email = ?", (email,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to delete key by email {email}: {e}")

def mark_key_missing(key_email: str, first_seen: str, host_name: str | None = None):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO vpn_keys_missing (key_email, host_name, first_seen) VALUES (?, ?, ?)",
                (key_email, host_name, first_seen)
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to mark key missing {key_email}: {e}")

def get_missing_keys():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys_missing")
            return [dict(r) for r in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get missing keys: {e}")
        return []

def purge_missing_key(key_email: str):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM vpn_keys_missing WHERE key_email = ?", (key_email,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to purge missing key {key_email}: {e}")

def get_user_keys(user_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE user_id = ? ORDER BY key_id", (user_id,))
            keys = cursor.fetchall()
            return [dict(key) for key in keys]
    except sqlite3.Error as e:
        logging.error(f"Failed to get keys for user {user_id}: {e}")
        return []

def get_key_by_id(key_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE key_id = ?", (key_id,))
            key_data = cursor.fetchone()
            return dict(key_data) if key_data else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get key by ID {key_id}: {e}")
        return None

def get_key_by_email(key_email: str):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE key_email = ?", (key_email,))
            key_data = cursor.fetchone()
            return dict(key_data) if key_data else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get key by email {key_email}: {e}")
        return None

def get_user_paid_keys(user_id: int):
    """Get only paid keys for user (plan_id > 0)"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE user_id = ? AND plan_id > 0 ORDER BY key_id", (user_id,))
            keys = cursor.fetchall()
            return [dict(key) for key in keys]
    except sqlite3.Error as e:
        logging.error(f"Failed to get paid keys for user {user_id}: {e}")
        return []

def migrate_global_plan_ids():
    """
    Heuristic migration: если есть глобальные тарифы (host_name='ALL'),
    то пользователям с 2+ активными платными ключами без plan_id присваиваем
    глобальный plan_id. Это помогает старым подписчикам корректно подтягивать новые хосты.
    """
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT plan_id FROM plans WHERE host_name = 'ALL' ORDER BY plan_id LIMIT 1")
            row = cursor.fetchone()
            if not row:
                logging.info("Migration: no global plans found, skipping global plan_id migration.")
                return 0
            global_plan_id = int(row['plan_id'])

            now = datetime.now()
            cursor.execute("""
                SELECT key_id, user_id, host_name, plan_id, expiry_date
                FROM vpn_keys
                WHERE plan_id <= 0
            """)
            rows = cursor.fetchall()
            if not rows:
                logging.info("Migration: no keys with plan_id <= 0 found.")
                return 0

            # bucket by user
            user_keys: dict[int, list[sqlite3.Row]] = {}
            for r in rows:
                user_keys.setdefault(int(r['user_id']), []).append(r)

            updated = 0
            for user_id, keys in user_keys.items():
                # count distinct active hosts
                active_hosts = set()
                for k in keys:
                    expiry_raw = k['expiry_date']
                    try:
                        if expiry_raw and datetime.fromisoformat(expiry_raw) > now:
                            active_hosts.add(k['host_name'])
                    except Exception:
                        continue
                if len(active_hosts) < 2:
                    continue

                key_ids = [int(k['key_id']) for k in keys]
                cursor.executemany(
                    "UPDATE vpn_keys SET plan_id = ? WHERE key_id = ?",
                    [(global_plan_id, kid) for kid in key_ids]
                )
                updated += len(key_ids)

            conn.commit()
            logging.info(f"Migration: updated {updated} legacy keys to global plan_id={global_plan_id}.")
            return updated
    except sqlite3.Error as e:
        logging.error(f"Migration failed: {e}")
        return 0

def get_user_trial_keys(user_id: int):
    """Get only trial keys for user (plan_id = 0)"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE user_id = ? AND plan_id = 0 ORDER BY key_id", (user_id,))
            keys = cursor.fetchall()
            return [dict(key) for key in keys]
    except sqlite3.Error as e:
        logging.error(f"Failed to get trial keys for user {user_id}: {e}")
        return []

def update_key_plan_id(key_id: int, plan_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE vpn_keys SET plan_id = ? WHERE key_id = ?", (plan_id, key_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update plan_id for key {key_id}: {e}")

def update_key_info(key_id: int, expiry_date: datetime, connection_string: str | None = None):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            if connection_string:
                cursor.execute("UPDATE vpn_keys SET expiry_date = ?, connection_string = ? WHERE key_id = ?", (expiry_date, connection_string, key_id))
            else:
                 cursor.execute("UPDATE vpn_keys SET expiry_date = ? WHERE key_id = ?", (expiry_date, key_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update key {key_id}: {e}")

def update_key_connection_string(key_id: int, connection_string: str):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE vpn_keys SET connection_string = ? WHERE key_id = ?", (connection_string, key_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update connection string for key {key_id}: {e}")

def update_key_plan_id(key_id: int, plan_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE vpn_keys SET plan_id = ? WHERE key_id = ?", (int(plan_id), int(key_id)))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update plan_id for key {key_id}: {e}")

import re

def get_next_key_number(user_id: int) -> int:
    """
    Safely determine the next key number for a user to avoid collisions.
    Parses existing key emails (e.g. user123-key5-ru) to find the max 'keyX' number.
    """
    keys = get_user_keys(user_id)
    max_num = 0
    pattern = re.compile(rf"user{user_id}-key(\d+)-")
    
    for key in keys:
        email = key.get('key_email', '')
        match = pattern.search(email)
        if match:
            try:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num
            except ValueError:
                continue
    
    return max_num + 1

def get_keys_for_host(host_name: str) -> list[dict]:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE host_name = ?", (host_name,))
            keys = cursor.fetchall()
            return [dict(key) for key in keys]
    except sqlite3.Error as e:
        logging.error(f"Failed to get keys for host '{host_name}': {e}")
        return []

def get_all_vpn_users():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT user_id FROM vpn_keys")
            users = cursor.fetchall()
            return [dict(user) for user in users]
    except sqlite3.Error as e:
        logging.error(f"Failed to get all vpn users: {e}")
        return []

def update_key_status_from_server(key_email: str, xui_client_data):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            if xui_client_data:
                expiry_date = time_utils.from_timestamp_ms(xui_client_data.expiry_time)
                cursor.execute("UPDATE vpn_keys SET xui_client_uuid = ?, expiry_date = ? WHERE key_email = ?", (xui_client_data.id, expiry_date, key_email))

                # Key found, remove from missing
                cursor.execute("DELETE FROM vpn_keys_missing WHERE key_email = ?", (key_email,))
            else:
                cursor.execute("DELETE FROM vpn_keys WHERE key_email = ?", (key_email,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update key status for {key_email}: {e}")

def get_daily_stats_for_charts(days: int = 30) -> dict:
    stats = {'users': {}, 'keys': {}}
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            query_users = """
                SELECT date(registration_date) as day, COUNT(*)
                FROM users
                WHERE registration_date >= date('now', ?)
                GROUP BY day
                ORDER BY day;
            """
            cursor.execute(query_users, (f'-{days} days',))
            for row in cursor.fetchall():
                stats['users'][row[0]] = row[1]
            
            query_keys = """
                SELECT date(created_date) as day, COUNT(*)
                FROM vpn_keys
                WHERE created_date >= date('now', ?)
                GROUP BY day
                ORDER BY day;
            """
            cursor.execute(query_keys, (f'-{days} days',))
            for row in cursor.fetchall():
                stats['keys'][row[0]] = row[1]
    except sqlite3.Error as e:
        logging.error(f"Failed to get daily stats for charts: {e}")
    return stats


def get_recent_transactions(limit: int = 15) -> list[dict]:
    transactions = []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = """
                SELECT
                    k.key_id,
                    k.host_name,
                    k.created_date,
                    u.telegram_id,
                    u.username
                FROM vpn_keys k
                JOIN users u ON k.user_id = u.telegram_id
                ORDER BY k.created_date DESC
                LIMIT ?;
            """
            cursor.execute(query, (limit,))
            transactions = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get recent transactions: {e}")
    return transactions

def add_support_thread(user_id: int, thread_id: int):
    if thread_id is None:
        logger.warning(f"Attempted to add None thread_id for user {user_id}. Ignoring.")
        return
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO support_threads (user_id, thread_id) VALUES (?, ?)", (user_id, thread_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to add support thread for user {user_id}: {e}")

def delete_support_thread(user_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM support_threads WHERE user_id = ?", (user_id,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to delete support thread for user {user_id}: {e}")

def get_support_thread_id(user_id: int) -> int | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT thread_id FROM support_threads WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get support thread_id for user {user_id}: {e}")
        return None

def get_user_id_by_thread(thread_id: int) -> int | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM support_threads WHERE thread_id = ?", (thread_id,))
            result = cursor.fetchone()
            return result[0] if result else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get user_id for thread {thread_id}: {e}")
        return None

def get_latest_transaction(user_id: int) -> dict | None:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM transactions WHERE user_id = ? ORDER BY created_date DESC LIMIT 1", (user_id,))
            transaction = cursor.fetchone()
            return dict(transaction) if transaction else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get latest transaction for user {user_id}: {e}")
        return None

def get_all_users() -> list[dict]:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users ORDER BY registration_date DESC")
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get all users: {e}")
        return []

def ban_user(telegram_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_banned = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to ban user {telegram_id}: {e}")

def unban_user(telegram_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_banned = 0 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to unban user {telegram_id}: {e}")

def delete_user_keys(user_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM vpn_keys WHERE user_id = ?", (user_id,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to delete keys for user {user_id}: {e}")

def delete_user_everywhere(user_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()

            cursor.execute("UPDATE users SET referred_by = NULL WHERE referred_by = ?", (user_id,))
            cursor.execute("DELETE FROM support_threads WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM vpn_keys WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM sent_notifications WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM users WHERE telegram_id = ?", (user_id,))

            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to delete user {user_id} everywhere: {e}")

def is_notification_sent(user_id: int, key_id: int | None, notification_type: str, hours_mark: int | None) -> bool:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            if key_id is not None:
                cursor.execute(
                    "SELECT 1 FROM sent_notifications WHERE user_id = ? AND key_id = ? AND notification_type = ? AND hours_mark = ?",
                    (user_id, key_id, notification_type, hours_mark)
                )
            else:
                cursor.execute(
                    "SELECT 1 FROM sent_notifications WHERE user_id = ? AND key_id IS NULL AND notification_type = ? AND hours_mark = ?",
                    (user_id, notification_type, hours_mark)
                )
            return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Error checking sent notification: {e}")
        return False

def mark_notification_sent(user_id: int, key_id: int | None, notification_type: str, hours_mark: int | None):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO sent_notifications (user_id, key_id, notification_type, hours_mark) VALUES (?, ?, ?, ?)",
                (user_id, key_id, notification_type, hours_mark)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error marking notification sent: {e}")

def cleanup_notifications(days_to_keep: int = 30):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(sent_notifications)")
            columns = [row[1] for row in cursor.fetchall()]

            # Historical schema used "sent_at". Some old DBs may have "created_at".
            date_column = None
            if 'sent_at' in columns:
                date_column = 'sent_at'
            elif 'created_at' in columns:
                date_column = 'created_at'
            else:
                # Keep compatibility with legacy DBs by adding a timestamp column once.
                cursor.execute("ALTER TABLE sent_notifications ADD COLUMN sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                conn.commit()
                date_column = 'sent_at'

            cursor.execute(f"DELETE FROM sent_notifications WHERE {date_column} < datetime('now', ?)", (f'-{days_to_keep} days',))
            conn.commit()
            deleted = cursor.rowcount
            if deleted > 0:
                logging.info(f"Cleaned up {deleted} old notification records.")
    except Exception as e:
        logger.error(f"Error cleaning up notifications: {e}")


# ============================================================================
# Race Condition Protection Functions
# ============================================================================

def set_pending_payment(user_id: int, is_pending: bool) -> bool:
    """
    Set or clear pending payment flag to prevent race conditions when multiple
    payment webhooks arrive simultaneously for the same user.
    Returns True if successful, False otherwise.
    """
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET pending_payment = ? WHERE telegram_id = ?",
                (1 if is_pending else 0, user_id)
            )
            conn.commit()
            if cursor.rowcount > 0:
                logging.info(f"Pending payment flag for user {user_id} set to {is_pending}")
                return True
            else:
                logging.warning(f"User {user_id} not found when setting pending payment flag")
                return False
    except sqlite3.Error as e:
        logging.error(f"Failed to set pending payment flag for user {user_id}: {e}")
        return False

def get_pending_payment_status(user_id: int) -> bool:
    """
    Check if user has a pending payment in progress.
    Returns True if there's a pending payment, False otherwise.
    """
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT pending_payment FROM users WHERE telegram_id = ?",
                (user_id,)
            )
            result = cursor.fetchone()
            if result:
                return bool(result[0])
            else:
                logging.warning(f"User {user_id} not found when checking pending payment status")
                return False
    except sqlite3.Error as e:
        logging.error(f"Failed to get pending payment status for user {user_id}: {e}")
        return False

def clear_all_pending_payments() -> int:
    """
    Clear pending payment flags for all users. 
    Used on startup to ensure no stale flags remain.
    Returns count of affected users.
    """
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET pending_payment = 0 WHERE pending_payment = 1")
            conn.commit()
            affected = cursor.rowcount
            if affected > 0:
                logging.info(f"Cleared {affected} stale pending payment flags on startup")
            return affected
    except sqlite3.Error as e:
        logging.error(f"Failed to clear pending payments: {e}")
        return 0

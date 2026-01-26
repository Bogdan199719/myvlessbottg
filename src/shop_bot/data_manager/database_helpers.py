import sqlite3 as sl

from shop_bot.data_manager.database import DB_FILE


def get_user_paid_keys(user_id):
    """Get only paid keys for user (plan_id > 0)"""
    conn = sl.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT key_id, user_id, host_name, xui_client_uuid, key_email, expiry_date, created_date, connection_string, plan_id
        FROM vpn_keys
        WHERE user_id = ? AND plan_id > 0
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    
    keys = []
    for row in rows:
        keys.append({
            'key_id': row[0],
            'user_id': row[1],
            'host_name': row[2],
            'xui_client_uuid': row[3],
            'key_email': row[4],
            'expiry_date': row[5],
            'created_date': row[6],
            'connection_string': row[7],
            'plan_id': row[8]
        })
    return keys


def get_user_trial_keys(user_id):
    """Get only trial keys for user (plan_id = 0)"""
    conn = sl.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT key_id, user_id, host_name, xui_client_uuid, key_email, expiry_date, created_date, connection_string, plan_id
        FROM vpn_keys
        WHERE user_id = ? AND plan_id = 0
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    
    keys = []
    for row in rows:
        keys.append({
            'key_id': row[0],
            'user_id': row[1],
            'host_name': row[2],
            'xui_client_uuid': row[3],
            'key_email': row[4],
            'expiry_date': row[5],
            'created_date': row[6],
            'connection_string': row[7],
            'plan_id': row[8]
        })
    return keys

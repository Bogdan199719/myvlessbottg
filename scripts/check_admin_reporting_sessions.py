#!/usr/bin/env python3
"""Integration checks with an isolated synthetic database, never production data."""

import os
import tempfile
from pathlib import Path
import sys
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def main():
    assert os.environ.get("ISOLATED_ADMIN_TEST") == "1", "explicit isolation required"
    interfaces = {
        line.split(":")[0].strip()
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]
    }
    assert interfaces <= {"lo"}, "network-none container required"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    with tempfile.TemporaryDirectory(prefix="admin-reporting-") as temp:
        db_path = Path(temp) / "synthetic.db"
        os.environ["DB_PATH"] = str(db_path)
        os.environ["FLASK_SECRET_KEY"] = "synthetic-admin-test-secret-0123456789abcdef"
        from werkzeug.security import generate_password_hash
        from shop_bot.data_manager import database as db

        db.initialize_db()
        from shop_bot.webhook_server import app as module

        db.update_setting("panel_login", "synthetic-admin")
        db.update_setting(
            "panel_password", generate_password_hash("synthetic-password")
        )
        now = datetime.now(timezone.utc)
        created = (now - timedelta(days=2)).isoformat()
        future = (now + timedelta(days=30)).isoformat()
        past = (now - timedelta(days=1)).isoformat()
        with sqlite3.connect(db_path) as conn:
            for uid in (1, 2, 3):
                conn.execute(
                    "INSERT INTO users(telegram_id,username,registration_date,trial_used) VALUES(?,?,?,?)",
                    (uid, f"synthetic-{uid}", created, uid == 2),
                )
            for uid, plan, expiry in [(1, 1, future), (2, 0, past), (3, 1, future)]:
                conn.execute(
                    "INSERT INTO vpn_keys(user_id,host_name,xui_client_uuid,key_email,expiry_date,created_date,plan_id,service_type,connection_string) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        uid,
                        "synthetic-host",
                        f"synthetic-uuid-{uid}",
                        f"synthetic-{uid}@test.invalid",
                        expiry,
                        created,
                        plan,
                        "xui",
                        f"synthetic://{uid}",
                    ),
                )
            for pid, uid, amount, method in [
                ("money1", 1, 100, "YooKassa"),
                ("money2", 1, 200, "YooKassa"),
                ("archived", 99, 300, "YooKassa"),
                ("free", 3, 0, "Promo"),
            ]:
                conn.execute(
                    "INSERT INTO transactions(payment_id,user_id,username,status,amount_rub,payment_method,created_date,paid_date,metadata) VALUES(?,?,?,'paid',?,?,?,?,?)",
                    (
                        pid,
                        uid,
                        f"synthetic-{uid}",
                        amount,
                        method,
                        created,
                        created,
                        json.dumps({"plan_id": 1}),
                    ),
                )
            before_keys = conn.execute(
                "SELECT * FROM vpn_keys ORDER BY key_id"
            ).fetchall()
            before_transactions = conn.execute(
                "SELECT * FROM transactions ORDER BY transaction_id"
            ).fetchall()

        app = module.create_webhook_app(
            SimpleNamespace(
                get_status=lambda: {
                    "shop_bot_running": False,
                    "support_bot_running": False,
                }
            )
        )
        app.config["TESTING"] = True
        base = "https://localhost"
        client = app.test_client()
        assert client.get("/dashboard", base_url=base).status_code == 302
        assert client.get("/login", base_url=base).status_code == 200
        with client.session_transaction(base_url=base) as state:
            csrf = state["_csrf_token"]
        login = client.post(
            "/login",
            base_url=base,
            data={
                "username": "synthetic-admin",
                "password": "synthetic-password",
                "csrf_token": csrf,
            },
        )
        assert login.status_code == 302
        with client.session_transaction(base_url=base) as state:
            assert state.get("admin_credential_fingerprint")

        for kind, heading, expected_count in [
            ("all", "Все завершённые покупки", 4),
            ("money", "Денежные оплаты", 3),
            ("promo", "Бесплатные активации по промокоду", 1),
        ]:
            response = client.get(
                f"/dashboard?purchases={kind}#completed-purchases", base_url=base
            )
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert f"<h2>{heading}</h2>" in html
            assert f"показано {expected_count} из {expected_count}" in html
            assert "Профили в списке: 1; без профиля: 1" in html
            assert "оплачивали среди текущих профилей: 33.3%" in html
            assert "600 ₽" in html, "purchase filter must not alter historical revenue"
            if os.environ.get("ADMIN_PREVIEW_DIR"):
                preview = Path(os.environ["ADMIN_PREVIEW_DIR"])
                preview.mkdir(parents=True, exist_ok=True)
                (preview / f"dashboard-{kind}.html").write_text(html, encoding="utf-8")
            table = html.split('<section id="completed-purchases"', 1)[1].split(
                "</section>", 1
            )[0]
            assert table.count('<td class="tx-date">') == expected_count
        assert (
            client.get("/dashboard?purchases=invalid", base_url=base).status_code == 200
        )
        for path in (
            "/users?status=paid",
            "/users?status=paid_ever",
            "/keys",
            "/settings",
            "/updates",
        ):
            assert client.get(path, base_url=base).status_code == 200, path

        assert db.ban_user(1) is True and db.unban_user(1) is True
        assert db.ban_user(404) is False and db.unban_user(404) is False
        for operation in ("ban", "unban"):
            original = getattr(module, operation + "_user")
            setattr(module, operation + "_user", lambda uid: False)
            try:
                client.get("/users", base_url=base)
                with client.session_transaction(base_url=base) as state:
                    csrf = state["_csrf_token"]
                    state.pop("_flashes", None)
                response = client.post(
                    f"/users/{operation}/1", base_url=base, data={"csrf_token": csrf}
                )
                assert response.status_code == 302
                with client.session_transaction(base_url=base) as state:
                    assert state["_flashes"][-1][0] == "danger"
            finally:
                setattr(module, operation + "_user", original)

        db.update_setting(
            "panel_password", generate_password_hash("changed-synthetic-password")
        )
        assert (
            client.get("/dashboard", base_url=base).status_code == 302
        ), "old session must be revoked"
        with client.session_transaction(base_url=base) as state:
            assert "logged_in" not in state and "_csrf_token" not in state
            state["logged_in"] = True
        assert (
            client.get("/dashboard", base_url=base).status_code == 302
        ), "legacy unbound session must be rejected"
        with sqlite3.connect(db_path) as conn:
            assert (
                before_keys
                == conn.execute("SELECT * FROM vpn_keys ORDER BY key_id").fetchall()
            )
            assert (
                before_transactions
                == conn.execute(
                    "SELECT * FROM transactions ORDER BY transaction_id"
                ).fetchall()
            )
        print(
            "Admin reporting/session integration checks passed; synthetic keys and transactions preserved."
        )


if __name__ == "__main__":
    main()

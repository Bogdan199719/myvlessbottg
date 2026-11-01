"""Check expiration/capacity without evicting active brute-force bans."""
import os
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from shop_bot.utils import login_attempts as guard

attempts = {}
for i in range(5):
    assert not guard.login_is_limited(attempts, "one", i, 300, 5)
    guard.record_login_failure(attempts, "one", i, 300, 5)
assert guard.login_is_limited(attempts, "one", 5, 300, 5)
assert guard.login_is_limited(attempts, "one", 299, 300, 5)
assert not guard.login_is_limited(attempts, "one", 300, 300, 5)
assert len(attempts["one"]) == 4
with patch.object(guard, "MAX_TRACKED_IPS", 8):
    attempts = {"banned": [1] * 5}
    for i in range(100):
        guard.record_login_failure(attempts, str(i), 2, 300, 5)
    assert len(attempts) == 8
    assert guard.login_is_limited(attempts, "new", 3, 300, 5)
    assert guard.login_is_limited(attempts, "banned", 3, 300, 5)
    assert not guard.login_is_limited(attempts, "new", 303, 300, 5)
    assert not attempts
    lock = threading.Lock()
    def failure(i):
        with lock:
            guard.record_login_failure(attempts, str(i % 16), 304, 300, 5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(failure, range(200)))
    assert len(attempts) <= 8 and all(len(v) <= 5 for v in attempts.values())

# Keeping the most recent failures must match the original unbounded tracker.
full = []
bounded = {}
for stamp in [0, 4, 2, 1, 3, 100, 110, 105]:
    full.append(stamp)
    guard.record_login_failure(bounded, "one", stamp, 300, 5)
for now in [200, 300, 301, 303, 304, 400, 410]:
    assert guard.login_is_limited(bounded, "one", now, 300, 5) == (sum(now-t < 300 for t in full) >= 5)

with tempfile.TemporaryDirectory(prefix="login-check-") as temporary:
    os.environ["DB_PATH"] = str(Path(temporary) / "check.db")
    from shop_bot.data_manager import database as db
    from shop_bot.webhook_server.app import create_webhook_app
    from werkzeug.security import generate_password_hash
    from types import SimpleNamespace
    db.initialize_db()
    db.update_setting("panel_login", "test")
    db.update_setting("panel_password", generate_password_hash("password"))
    app = create_webhook_app(SimpleNamespace())
    app.config["TESTING"] = True
    client = app.test_client()
    def post(password, ip="192.0.2.1"):
        with client.session_transaction() as session:
            session["_csrf_token"] = "test-csrf"
        return client.post("/login", base_url="https://localhost", data={"username":"test", "password":password, "csrf_token":"test-csrf"}, environ_overrides={"REMOTE_ADDR":ip})
    with patch("shop_bot.webhook_server.app._time.monotonic", return_value=1000):
        for _ in range(5):
            assert post("bad").status_code == 200
        assert post("password").status_code == 429
        assert post("password", "192.0.2.2").status_code == 302
    with patch("shop_bot.webhook_server.app._time.monotonic", return_value=1300):
        assert post("password").status_code == 302
    db.update_setting("admin_ip_allowlist", "192.0.2.3")
    for _ in range(7):
        assert post("bad", "192.0.2.3").status_code == 200
    assert post("password", "192.0.2.3").status_code == 302
print("Stage 1 login bounds: OK")

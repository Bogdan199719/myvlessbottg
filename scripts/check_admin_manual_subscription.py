#!/usr/bin/env python3
"""Static safeguards for manual subscription grants in the web admin."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "src/shop_bot/webhook_server/app.py").read_text(encoding="utf-8")
USERS = (ROOT / "src/shop_bot/webhook_server/templates/users.html").read_text(
    encoding="utf-8"
)


def main() -> None:
    route = APP.split("def issue_key_route(user_id):", 1)[1].split(
        '@flask_app.route("/users/delete/', 1
    )[0]
    adjust_route = APP.split("def adjust_key_duration(key_id):", 1)[1].split(
        '@flask_app.route("/keys/sync', 1
    )[0]
    assert 'request.form.get("custom_days")' in route
    assert "1 <= custom_days <= 3650" in route
    assert "create_or_update_key_on_host_seconds(" in route
    assert "seconds_to_add=days_to_add * 86400" in route
    assert "global_base_dt = max(" in route
    assert "target_expiry_ms=global_target_expiry_ms" in route
    assert "preserve_longer_expiry=True" in route
    assert 'plan.get("service_type") == "mtg"' in route
    assert "mtg_api.renew_proxy_for_user(" in route
    assert "mtg_api.create_proxy_for_user(" in route
    assert 'name="custom_days"' in USERS
    assert 'name="plan_id" id="planSelect" required' not in USERS
    assert 'request.form.get("days") or "0"' in adjust_route
    assert "Дни и часы должны быть целыми числами." in adjust_route
    assert "positive_target_ms" in adjust_route
    assert "preserve_longer_expiry=True" in adjust_route
    assert "mtg_api.get_proxy_details(" in adjust_route
    print("Admin manual subscription safeguards: OK")


if __name__ == "__main__":
    main()

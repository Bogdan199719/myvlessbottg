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
    days_route = route.split("if custom_days_raw:", 1)[1].split(
        "if not plan_id:", 1
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
    assert 'time_utils.get_msk_now() + timedelta(days=custom_days)' in days_route
    assert 'async def update_duration(key):' in days_route
    assert 'user_keys, lambda key: key["host_name"], update_duration' in days_route
    assert 'run_panel_jobs(hosts,' in route
    assert 'timeout=45,' in days_route
    assert 'target_expiry_ms=target_expiry_ms' in days_route
    assert 'preserve_longer_expiry=False' in days_route
    assert 'if any(key.get("service_type") == "mtg" for key in user_keys)' in days_route
    assert "Срок отсчитывается от выдачи и устанавливается для всех серверов." in USERS
    assert 'name="plan_id" id="planSelect" required' not in USERS
    assert "@single_manual_issue_per_user" in APP
    assert "subscription_operations.try_acquire(normalized_user_id)" in APP
    assert "subscription_operations.release(normalized_user_id)" in APP
    assert "@single_key_duration_operation" in APP
    assert 'issueKeyForm.dataset.submitting === "1"' in USERS
    assert 'submitButton.disabled = true' in USERS
    assert 'request.form.get("days") or "0"' in adjust_route
    assert "Дни и часы должны быть целыми числами." in adjust_route
    assert "positive_target_ms" in adjust_route
    assert "preserve_longer_expiry=True" in adjust_route
    assert "mtg_api.get_proxy_details(" in adjust_route
    print("Admin manual subscription safeguards: OK")


if __name__ == "__main__":
    main()

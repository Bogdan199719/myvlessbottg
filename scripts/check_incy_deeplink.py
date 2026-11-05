#!/usr/bin/env python3
"""Validate Incy subscription deeplinks without touching runtime data."""

from unittest.mock import patch

from flask import Flask

from shop_bot.webhook_server.subscription_api import redirect_to_incy


def main() -> int:
    app = Flask(__name__)
    with app.test_request_context("/"):
        with (
            patch(
                "shop_bot.webhook_server.subscription_api.get_user_by_token",
                return_value={"is_banned": False},
            ),
            patch(
                "shop_bot.webhook_server.subscription_api.get_setting",
                return_value="vpn.example",
            ),
        ):
            response = redirect_to_incy("test-token")

    assert response.status_code == 302
    assert (
        response.headers["Location"]
        == "incy://import/https://vpn.example/sub/test-token"
    )
    print("OK: Incy HTTPS redirect opens the subscription import deeplink.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

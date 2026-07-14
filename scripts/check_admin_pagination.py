#!/usr/bin/env python3
"""Deterministic checks for server-side users/keys list pagination."""

from __future__ import annotations

import ast
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from flask import Flask  # noqa: E402
from jinja2 import Environment, FileSystemLoader  # noqa: E402
from shop_bot.webhook_server.app import (  # noqa: E402
    _admin_list_request_args,
    _paginate_admin_items,
)


APP_PATH = SRC_ROOT / "shop_bot/webhook_server/app.py"
TEMPLATE_DIR = SRC_ROOT / "shop_bot/webhook_server/templates"


def _function_source(source: str, function_name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == function_name:
                return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {function_name} is missing")


def _check_pagination_helpers() -> None:
    page_items, pagination = _paginate_admin_items(list(range(73)), 99, 25)
    assert page_items == list(range(50, 73))
    assert pagination["page"] == 3
    assert pagination["total_pages"] == 3
    assert pagination["total_items"] == 73
    assert pagination["has_previous"] and not pagination["has_next"]

    empty_items, empty_pagination = _paginate_admin_items([], 4, 50)
    assert empty_items == []
    assert empty_pagination["page"] == 1
    assert empty_pagination["total_pages"] == 1

    flask_app = Flask(__name__)
    with flask_app.test_request_context(
        "/users?q=" + ("x" * 250) + "&status=invalid&page=-5&per_page=999"
    ):
        query, status, page, per_page = _admin_list_request_args({"all", "paid"})
    assert len(query) == 200
    assert status == "all"
    assert page == 1
    assert per_page == 50


def _check_route_invariants() -> None:
    source = APP_PATH.read_text(encoding="utf-8")
    users_source = _function_source(source, "users_page")
    keys_source = _function_source(source, "keys_page")

    assert "get_user_keys(" not in users_source, (
        "users_page must not issue one key query per user"
    )
    assert "get_all_keys_with_usernames()" in users_source
    assert "keys_by_user" in users_source
    assert "_paginate_admin_items(" in users_source
    assert "_paginate_admin_items(" in keys_source
    assert "filtered_users" in users_source
    assert "filtered_rows" in keys_source
    assert '"connection_string"' not in keys_source.split(
        '"search":', 1
    )[1], "key secrets must not be included in the search text"


def _check_templates() -> None:
    environment = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    for template_name in ("users.html", "keys.html"):
        environment.get_template(template_name)

    users_template = (TEMPLATE_DIR / "users.html").read_text(encoding="utf-8")
    keys_template = (TEMPLATE_DIR / "keys.html").read_text(encoding="utf-8")
    for template_source in (users_template, keys_template):
        assert 'name="q"' in template_source
        assert 'name="per_page"' in template_source
        assert "pagination.total_pages" in template_source
        assert "pagination.total_items" in template_source

    # The legacy JavaScript filters only the current DOM page. These hooks must
    # be absent so search/filter state is always evaluated by the server.
    assert 'id="usersSearch"' not in users_template
    assert "data-user-filter" not in users_template
    assert 'id="keysSearch"' not in keys_template
    assert "data-key-filter" not in keys_template
    assert "data-copy=" not in keys_template, (
        "admin key secrets must be loaded on demand, not embedded in HTML"
    )
    assert "data-secret-url=" in keys_template

    app_source = APP_PATH.read_text(encoding="utf-8")
    assert '@flask_app.route("/keys/<int:key_id>/secret")' in app_source
    assert 'response.headers["Cache-Control"] = "no-store, private"' in app_source


def main() -> int:
    _check_pagination_helpers()
    _check_route_invariants()
    _check_templates()
    print("Admin pagination checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

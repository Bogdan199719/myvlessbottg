#!/usr/bin/env python3
"""Verify that subscription reads stay independent from panel provisioning."""

from __future__ import annotations

import ast
from pathlib import Path


def _function_node(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Function {name!r} not found")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Call):
            continue
        if isinstance(candidate.func, ast.Name):
            names.add(candidate.func.id)
        elif isinstance(candidate.func, ast.Attribute):
            names.add(candidate.func.attr)
    return names


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    subscription_path = root / "src/shop_bot/webhook_server/subscription_api.py"
    scheduler_path = root / "src/shop_bot/data_manager/scheduler.py"

    subscription_source = subscription_path.read_text(encoding="utf-8")
    scheduler_source = scheduler_path.read_text(encoding="utf-8")
    subscription_tree = ast.parse(subscription_source)
    scheduler_tree = ast.parse(scheduler_source)

    endpoint = _function_node(subscription_tree, "get_subscription")
    endpoint_calls = _called_names(endpoint)
    forbidden_endpoint_calls = {
        "create_or_update_key_on_host_absolute_expiry",
        "add_new_key",
        "update_key_by_email",
    }
    unexpected = forbidden_endpoint_calls & endpoint_calls
    assert not unexpected, (
        "Subscription endpoint must not provision or persist panel keys: "
        f"{sorted(unexpected)}"
    )
    assert "returning available " in subscription_source
    assert "configs immediately while background reconciliation" in subscription_source

    reconciler = next(
        node
        for node in ast.walk(scheduler_tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "auto_provision_new_hosts_for_global_users"
    )
    reconciler_calls = _called_names(reconciler)
    assert "create_or_update_key_on_host_absolute_expiry" in reconciler_calls
    assert "_is_host_in_failure_backoff" in reconciler_calls
    assert "_mark_host_failure" in reconciler_calls

    periodic = next(
        node
        for node in ast.walk(scheduler_tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "periodic_subscription_check"
    )
    assert "auto_provision_new_hosts_for_global_users" in _called_names(periodic)

    print(
        "OK: subscription reads are fast-path only; background reconciliation "
        "retains host backoff."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

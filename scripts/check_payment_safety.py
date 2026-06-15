#!/usr/bin/env python3
"""Static safety checks for payment fulfillment handlers.

The checks are intentionally narrow. They guard production regressions that can
turn a successful fulfillment into a retry, leave users stuck in pending state,
or make admin P2P commands unusable.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "src" / "shop_bot" / "bot" / "handlers.py"


def _load_tree() -> tuple[str, ast.Module]:
    source = HANDLERS.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(HANDLERS))


def _functions(tree: ast.AST) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    result: dict[str, ast.AsyncFunctionDef | ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            result[node.name] = node
    return result


def _source_segment(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    return segment or ""


def _has_final_pending_clear(source: str, fn: ast.AsyncFunctionDef) -> bool:
    text = _source_segment(source, fn)
    return (
        "finally:" in text
        and "if pending_flag_set:" in text
        and "set_pending_payment(user_id, False)" in text
    )


def _has_broad_cleanup_guard(
    source: str, fn: ast.AsyncFunctionDef, marker: str
) -> bool:
    text = _source_segment(source, fn)
    marker_index = text.find(marker)
    if marker_index < 0:
        return False
    prefix = text[:marker_index]
    suffix = text[marker_index:]
    return "try:" in prefix and "except Exception as e:" in suffix


def _p2p_handlers_parse_command_args(
    source: str, functions: dict[str, ast.AST]
) -> bool:
    for name in ("admin_approve_p2p_handler", "admin_decline_p2p_handler"):
        fn = functions.get(name)
        if not isinstance(fn, ast.AsyncFunctionDef):
            return False
        text = _source_segment(source, fn)
        if "_p2p_request_id_from_command(message, command)" not in text:
            return False
        if '.split("_")' in text:
            return False
    return True


def _execute_results_are_db_gated(source: str, fn: ast.AsyncFunctionDef) -> bool:
    text = _source_segment(source, fn)
    if "results.append(res)\n            if existing_key_db:" in text:
        return False
    required_fragments = (
        "if not updated:",
        "if new_key_id is None:",
        "results.append(res)",
    )
    return all(fragment in text for fragment in required_fragments)


def _global_fulfillment_is_complete_and_idempotent(
    source: str, functions: dict[str, ast.AST]
) -> bool:
    process_fn = functions.get("process_successful_payment")
    execute_fn = functions.get("_execute_payment_for_hosts")
    target_fn = functions.get("_target_expiry_ms_for_global_payment")
    if not (
        isinstance(process_fn, ast.AsyncFunctionDef)
        and isinstance(execute_fn, ast.AsyncFunctionDef)
        and isinstance(target_fn, ast.FunctionDef)
    ):
        return False

    process_text = _source_segment(source, process_fn)
    execute_text = _source_segment(source, execute_fn)
    target_text = _source_segment(source, target_fn)
    required_process_fragments = (
        'host_name == "ALL"',
        "_target_expiry_ms_for_global_payment(",
        "len(results) != len(hosts_to_process)",
        "return False",
    )
    required_execute_fragments = (
        "target_expiry_ms",
        "create_or_update_key_on_host_absolute_expiry",
    )
    required_target_fragments = (
        "fulfillment_target_expiry_ms",
        'metadata["fulfillment_target_expiry_ms"]',
    )
    return (
        all(fragment in process_text for fragment in required_process_fragments)
        and all(fragment in execute_text for fragment in required_execute_fragments)
        and all(fragment in target_text for fragment in required_target_fragments)
    )


def _promo_fulfillment_is_resumable(source: str, functions: dict[str, ast.AST]) -> bool:
    promo_fn = functions.get("process_promo_code_handler")
    if not isinstance(promo_fn, ast.AsyncFunctionDef):
        return False
    promo_text = _source_segment(source, promo_fn)
    required_fragments = (
        "set_promo_fulfillment_target(",
        "promo_fulfillment_started = True",
        "not promo_fulfillment_started",
        "Повторите ввод этого же кода позже",
    )
    forbidden_fragments = (
        "if not results:\n                release_promo_code_claim",
        "if len(results) != len(hosts_to_process):\n                release_promo_code_claim",
    )
    return all(fragment in promo_text for fragment in required_fragments) and not any(
        fragment in promo_text for fragment in forbidden_fragments
    )


def _fulfillment_survives_notification_failure(
    source: str, fn: ast.AsyncFunctionDef
) -> bool:
    text = _source_segment(source, fn)
    required_fragments = (
        "processing_message = _BestEffortProcessingMessage()",
        "will continue without a processing message",
    )
    forbidden_fragment = (
        "could not start fulfillment because the processing message failed"
    )
    return all(fragment in text for fragment in required_fragments) and (
        forbidden_fragment not in text
    )


def main() -> int:
    source, tree = _load_tree()
    functions = _functions(tree)
    failures: list[str] = []

    process_fn = functions.get("process_successful_payment")
    execute_fn = functions.get("_execute_payment_for_hosts")
    promo_fn = functions.get("process_promo_code_handler")

    if not isinstance(process_fn, ast.AsyncFunctionDef):
        failures.append("process_successful_payment is missing")
    else:
        if not _has_final_pending_clear(source, process_fn):
            failures.append(
                "process_successful_payment must clear pending_payment in finally"
            )
        if not _has_broad_cleanup_guard(
            source, process_fn, "await bot.delete_message("
        ):
            failures.append("old payment-message cleanup must catch broad Exception")
        if not _has_broad_cleanup_guard(
            source, process_fn, "await processing_message.delete()"
        ):
            failures.append("processing-message delete must be best-effort")
        if not _fulfillment_survives_notification_failure(source, process_fn):
            failures.append(
                "payment fulfillment must continue when Telegram status messaging fails"
            )

    if not isinstance(execute_fn, ast.AsyncFunctionDef):
        failures.append("_execute_payment_for_hosts is missing")
    elif not _execute_results_are_db_gated(source, execute_fn):
        failures.append(
            "_execute_payment_for_hosts results must be gated by DB persistence"
        )

    if not _global_fulfillment_is_complete_and_idempotent(source, functions):
        failures.append(
            "ALL fulfillment must require every host and use a persisted absolute target expiry"
        )

    if not _p2p_handlers_parse_command_args(source, functions):
        failures.append("P2P slash commands must parse CommandObject args")

    if not isinstance(promo_fn, ast.AsyncFunctionDef):
        failures.append("process_promo_code_handler is missing")
    else:
        promo_text = _source_segment(source, promo_fn)
        if "promo_applied = True" not in promo_text:
            failures.append(
                "promo handler must distinguish applied promo from reserved promo"
            )
        if "promo and not promo_applied" not in promo_text:
            failures.append("promo handler must not release already-applied promos")
        if not _promo_fulfillment_is_resumable(source, functions):
            failures.append(
                "promo fulfillment must persist its target and retain partial reservations"
            )

    if failures:
        print("Payment safety checks FAILED:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("Payment safety checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

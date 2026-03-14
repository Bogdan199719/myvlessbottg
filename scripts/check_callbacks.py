#!/usr/bin/env python3
"""Validate Telegram callback_data coverage between keyboards.py and handlers.py.

Exit codes:
- 0: everything looks good
- 1: missing handlers for keyboard callbacks
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEYBOARDS = ROOT / "src" / "shop_bot" / "bot" / "keyboards.py"
HANDLERS = ROOT / "src" / "shop_bot" / "bot" / "handlers.py"

# Callbacks intentionally kept for compatibility or external/manual use.
ALLOW_UNHANDLED_KEYBOARD_CALLBACKS = {
    "show_main_menu",
    "buy_vpn",
    "select_host_*_*",  # keyboard template; effective buttons are select_host_new_*
}


def _normalize_pattern(raw: str) -> str:
    # Replace f-string placeholders with wildcard marker.
    return re.sub(r"\{[^}]+\}", "*", raw)


def _extract_keyboard_callbacks(text: str) -> set[str]:
    callbacks = set()
    for raw in re.findall(r'callback_data\s*=\s*f?"([^"]+)"', text):
        callbacks.add(_normalize_pattern(raw))
    return callbacks


def _extract_handler_patterns(text: str) -> set[str]:
    patterns = set(re.findall(r'F\.data\s*==\s*"([^"]+)"', text))
    starts = re.findall(r'F\.data\.startswith\("([^"]+)"\)', text)
    patterns.update(f"{prefix}*" for prefix in starts)
    return patterns


def _covered_by_handler(callback: str, handlers: set[str]) -> bool:
    def literal_prefix(pattern: str) -> str:
        return pattern.split("*", 1)[0]

    if callback in handlers:
        return True

    # If callback is a wildcard template, compare prefix.
    if callback.endswith("*"):
        prefix = literal_prefix(callback)
        return any(literal_prefix(h).startswith(prefix) for h in handlers)

    # Callback may map to a startswith handler.
    return any(h.endswith("*") and callback.startswith(literal_prefix(h)) for h in handlers)


def main() -> int:
    kb_text = KEYBOARDS.read_text(encoding="utf-8")
    handlers_text = HANDLERS.read_text(encoding="utf-8")

    kb_callbacks = _extract_keyboard_callbacks(kb_text)
    handler_patterns = _extract_handler_patterns(handlers_text)

    unhandled = sorted(
        cb for cb in kb_callbacks
        if cb not in ALLOW_UNHANDLED_KEYBOARD_CALLBACKS and not _covered_by_handler(cb, handler_patterns)
    )

    print(f"Keyboard callbacks: {len(kb_callbacks)}")
    print(f"Handler patterns: {len(handler_patterns)}")

    if unhandled:
        print("\nERROR: callbacks from keyboards.py without handler coverage:")
        for cb in unhandled:
            print(f" - {cb}")
        return 1

    print("\nOK: all keyboard callbacks are covered by handlers (with allowlist exceptions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""FSM transition guard for bot handlers.

Checks critical callbacks/messages exist for stateful flows so users don't get stuck.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "src" / "shop_bot" / "bot" / "handlers.py"

# State -> required callback values (exact matches) in @user_router.callback_query(...)
REQUIRED_STATE_CALLBACKS: dict[str, set[str]] = {
    "Onboarding.waiting_for_subscription_and_agreement": {
        "check_subscription_and_agree"
    },
    "Broadcast.waiting_for_button_option": {
        "broadcast_add_button",
        "broadcast_skip_button",
    },
    "Broadcast.waiting_for_confirmation": {"confirm_broadcast"},
    "PaymentProcess.waiting_for_email": {"back_to_plans", "skip_email"},
    "PaymentProcess.waiting_for_payment_method": {
        "pay_yookassa",
        "pay_stars",
        "pay_cryptobot",
        "pay_p2p",
    },
}

# States requiring message handlers: users can send text while in these states.
REQUIRED_STATE_MESSAGE_HANDLERS: set[str] = {
    "Onboarding.waiting_for_subscription_and_agreement",
    "Broadcast.waiting_for_message",
    "Broadcast.waiting_for_button_text",
    "Broadcast.waiting_for_button_url",
    "PaymentProcess.waiting_for_email",
}

# Global callbacks that must always exist.
REQUIRED_GLOBAL_CALLBACKS: set[str] = {
    "back_to_main_menu",
    "back_to_email_prompt",
}

# A state-group cancel guard should exist to escape broadcast flow from any substate.
REQUIRED_STATEFILTER_GUARDS: set[str] = {
    "StateFilter(Broadcast):cancel_broadcast",
}


def _extract_balanced_args(text: str, start: int) -> str | None:
    """Extract content between balanced parentheses starting at position `start`."""
    if start >= len(text) or text[start] != "(":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    return None


def _extract_callback_decorators(text: str):
    # Matches single-line and multi-line decorators with balanced parentheses:
    # @user_router.callback_query(State, F.data == "x")
    # @user_router.callback_query(\n    State,\n    F.data == "x",\n)
    marker = "@user_router.callback_query("
    idx = 0
    while True:
        pos = text.find(marker, idx)
        if pos == -1:
            break
        paren_start = pos + len(marker) - 1  # position of '('
        args_raw = _extract_balanced_args(text, paren_start)
        idx = paren_start + 1
        if args_raw is None:
            continue
        args = " ".join(args_raw.split())  # normalize whitespace

        state = None
        callback = None
        starts = None

        # First positional argument can be state marker.
        state_match = re.match(r"\s*([A-Za-z0-9_\.\(\)]+)\s*,", args)
        if state_match:
            state = state_match.group(1)

        eq_match = re.search(r'F\.data\s*==\s*"([^"]+)"', args)
        if eq_match:
            callback = eq_match.group(1)

        starts_match = re.search(r'F\.data\.startswith\("([^"]+)"\)', args)
        if starts_match:
            starts = starts_match.group(1)

        yield state, callback, starts, args


def _extract_message_decorators(text: str):
    marker = "@user_router.message("
    idx = 0
    while True:
        pos = text.find(marker, idx)
        if pos == -1:
            break
        paren_start = pos + len(marker) - 1
        args_raw = _extract_balanced_args(text, paren_start)
        idx = paren_start + 1
        if args_raw is None:
            continue
        args = " ".join(args_raw.split())
        yield args


def main() -> int:
    text = HANDLERS.read_text(encoding="utf-8")

    callbacks_by_state: dict[str, set[str]] = {}
    global_callbacks: set[str] = set()
    statefilter_guards: set[str] = set()

    for state, callback, starts, args in _extract_callback_decorators(text):
        if callback:
            if state and state.startswith("StateFilter("):
                statefilter_guards.add(f"{state}:{callback}")
            elif state:
                callbacks_by_state.setdefault(state, set()).add(callback)
            else:
                global_callbacks.add(callback)
        if starts:
            # Track prefix handlers as informational only.
            if state:
                callbacks_by_state.setdefault(state, set()).add(f"{starts}*")
            else:
                global_callbacks.add(f"{starts}*")

    message_states: set[str] = set()
    for args in _extract_message_decorators(text):
        state_match = re.match(r"\s*([A-Za-z0-9_\.\(\)]+)\s*\)", args + ")")
        # Accept explicit state as first arg.
        if state_match:
            token = state_match.group(1)
            if "." in token or token.startswith("StateFilter("):
                message_states.add(token)

    errors: list[str] = []

    for state, required in REQUIRED_STATE_CALLBACKS.items():
        present = callbacks_by_state.get(state, set())
        missing = sorted(required - present)
        if missing:
            errors.append(f"State {state} missing callbacks: {', '.join(missing)}")

    for state in sorted(REQUIRED_STATE_MESSAGE_HANDLERS):
        if state not in message_states:
            errors.append(f"State {state} missing message handler")

    missing_global = sorted(
        cb for cb in REQUIRED_GLOBAL_CALLBACKS if cb not in global_callbacks
    )
    if missing_global:
        errors.append(f"Missing global callbacks: {', '.join(missing_global)}")

    missing_guards = sorted(
        g for g in REQUIRED_STATEFILTER_GUARDS if g not in statefilter_guards
    )
    if missing_guards:
        errors.append(f"Missing StateFilter guards: {', '.join(missing_guards)}")

    print(
        f"State callbacks tracked: {sum(len(v) for v in callbacks_by_state.values())}"
    )
    print(f"Message states tracked: {len(message_states)}")
    print(f"Global callbacks tracked: {len(global_callbacks)}")

    if errors:
        print("\nERROR: FSM checks failed:")
        for err in errors:
            print(f" - {err}")
        return 1

    print("\nOK: critical FSM transitions are covered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate parsing and normalization of Telegram administrator IDs."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from shop_bot.utils.admin_ids import (
    is_admin_telegram_id,
    normalize_admin_telegram_ids,
    parse_admin_telegram_ids,
)


def main() -> int:
    assert parse_admin_telegram_ids("123") == (123,)
    assert parse_admin_telegram_ids("123, 456;123") == (123, 456)
    assert parse_admin_telegram_ids(None) == ()
    assert is_admin_telegram_id(456, "123,456")
    assert not is_admin_telegram_id(789, "123,456")
    assert normalize_admin_telegram_ids("123, 456,123") == "123,456"

    for invalid_value in ("", "abc", "123,bad", "-123", "0"):
        try:
            normalize_admin_telegram_ids(invalid_value)
        except ValueError:
            continue
        raise AssertionError(f"Invalid administrator list accepted: {invalid_value!r}")

    print("OK: Telegram administrator ID parsing is consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Regression checks for bot lifecycle and bounded subscription workers."""

from __future__ import annotations

import asyncio
import threading
import time

from shop_bot.bot_controller import BotController
from shop_bot.webhook_server import subscription_api


class _ReturningDispatcher:
    async def start_polling(self, _bot) -> None:
        return None


class _FailingCloseBot:
    async def close(self) -> None:
        raise RuntimeError("expected close failure")


async def _check_bot_state_resets_after_close_failure() -> None:
    controller = BotController()
    controller.shop_is_running = True
    controller.shop_bot = _FailingCloseBot()
    controller.shop_dp = _ReturningDispatcher()

    await controller._start_polling(
        controller.shop_bot,
        controller.shop_dp,
        "ShopBot",
    )

    assert not controller.shop_is_running
    assert controller.shop_bot is None
    assert controller.shop_dp is None
    assert controller.shop_task is None

    previous_bot = object()
    replacement_bot = object()
    controller.shop_bot = replacement_bot
    controller.shop_is_running = True
    controller._reset_bot_state("ShopBot", expected_bot=previous_bot)
    assert controller.shop_bot is replacement_bot
    assert controller.shop_is_running


def _check_subscription_worker_capacity_is_bounded() -> None:
    release_workers = threading.Event()

    def blocked_call() -> None:
        release_workers.wait(timeout=2)

    try:
        for _ in range(4):
            assert subscription_api._call_with_timeout(blocked_call, 0.01) is None

        started_at = time.monotonic()
        assert subscription_api._call_with_timeout(blocked_call, 0.01) is None
        assert time.monotonic() - started_at < 0.1
    finally:
        release_workers.set()


def main() -> int:
    asyncio.run(_check_bot_state_resets_after_close_failure())
    _check_subscription_worker_capacity_is_bounded()
    print("Runtime stability checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

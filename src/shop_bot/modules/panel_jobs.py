"""Bounded subscription work grouped by physical 3x-ui panel."""

import asyncio
import logging
import time
from collections import defaultdict
from typing import Awaitable, Callable, TypeVar

from shop_bot.data_manager.database import get_host
from shop_bot.modules.host_selector import panel_group_key


Item = TypeVar("Item")
Result = TypeVar("Result")
MAX_CONCURRENT_PANELS = 5
logger = logging.getLogger(__name__)


async def run_panel_jobs(
    items: list[Item],
    host_name: Callable[[Item], str],
    operation: Callable[[Item], Awaitable[Result]],
) -> list[Result]:
    """Run each panel's jobs sequentially and preserve the input result order.

    Worker-level locks still protect writes across requests and cancellation.
    Grouping avoids filling the thread pool with jobs waiting for one panel.
    Operations must handle individual failures and persist successes immediately.
    """
    if not items:
        return []
    started_at = time.monotonic()

    def group_items():
        groups = defaultdict(list)
        hosts = {}
        for index, item in enumerate(items):
            name = host_name(item)
            if name not in hosts:
                hosts[name] = get_host(name) or {}
            panel = panel_group_key(hosts[name].get("host_url"), name)
            groups[panel].append((index, item))
        return list(groups.values())

    groups = await asyncio.to_thread(group_items)
    capacity = asyncio.Semaphore(MAX_CONCURRENT_PANELS)

    async def run_group(group):
        completed = []
        async with capacity:
            for index, item in group:
                completed.append((index, await operation(item)))
        return completed

    tasks = [asyncio.create_task(run_group(group)) for group in groups]
    try:
        completed = await asyncio.gather(*tasks)
    finally:
        # gather does not cancel siblings when one fails. Do not leave queued
        # provisioning jobs running after their caller exits.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            "XUI subscription batch: hosts=%s panels=%s elapsed_seconds=%.3f",
            len(items),
            len(groups),
            time.monotonic() - started_at,
        )

    ordered = sorted(pair for group in completed for pair in group)
    return [result for _index, result in ordered]

"""Coordinate subscription changes in the combined bot/admin process.

Never hold a threading lock while awaiting the event loop. Panel-level worker
locks remain responsible for serializing actual HTTP writes.
"""

import asyncio
import threading
from contextlib import asynccontextmanager
from functools import wraps

_guard = threading.Lock()
_active_users: set[int] = set()


def try_acquire(user_id: int) -> bool:
    user_id = int(user_id)
    with _guard:
        if user_id in _active_users:
            return False
        _active_users.add(user_id)
        return True


def release(user_id: int) -> None:
    with _guard:
        _active_users.remove(int(user_id))


async def complete_before_cancelling(task):
    """A cancelled caller must not abandon a still-running write/persistence."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def run_worker(operation, *args):
    return await complete_before_cancelling(
        asyncio.create_task(asyncio.to_thread(operation, *args))
    )


@asynccontextmanager
async def user_operation(user_id: int):
    while not try_acquire(user_id):
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        release(user_id)


def serialize_user_operation(user_id_from_args):
    """Wait for the user's previous operation without blocking Flask's loop.

    Retain the reservation until an already-started operation really finishes,
    even if its caller is cancelled. This also covers worker-thread API calls.
    """

    def decorate(operation):
        @wraps(operation)
        async def wrapped(*args, **kwargs):
            user_id = user_id_from_args(*args, **kwargs)
            if user_id is None:
                return await operation(*args, **kwargs)
            async with user_operation(user_id):
                task = asyncio.create_task(operation(*args, **kwargs))
                return await complete_before_cancelling(task)

        return wrapped

    return decorate

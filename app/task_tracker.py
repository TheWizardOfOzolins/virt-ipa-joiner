"""
Tracks long-running background asyncio tasks so they can be cancelled
cleanly on application shutdown.
"""

import asyncio
from typing import Set

_background_tasks: Set[asyncio.Task] = set()


def create_tracked_task(coro) -> asyncio.Task:
    """Schedule a coroutine as a tracked asyncio task."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def cancel_all_tasks() -> None:
    """Cancel all tracked tasks and wait for them to finish."""
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

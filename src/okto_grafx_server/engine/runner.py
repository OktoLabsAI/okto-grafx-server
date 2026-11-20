"""Offloading synchronous Grafx calls out of the event loop.

``okto_grafx`` is a synchronous embedded library: there is no async connection and no async door.
The integration guide's recipe is followed literally here, including the part that is easy to get
wrong — **a cancelled waiter has not cancelled native disk I/O**. So a cancelled HTTP request keeps
its lane slot until the worker thread really finishes, and shutdown drains the queue before closing
any handle. Inventing a "cancelled" response for work still touching the platter would be a lie.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from ..errors import AppError, too_many

log = logging.getLogger("okto_grafx_server.runner")

T = TypeVar("T")


@dataclass
class RunnerStats:
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    cancelled_waiters: int = 0
    in_flight: int = 0


class EngineRunner:
    """A bounded thread pool with per-database admission and honest cancellation."""

    def __init__(self, *, max_workers: int = 8, queue_capacity: int = 64) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)), thread_name_prefix="grafx-worker"
        )
        self._slots = threading.BoundedSemaphore(max(1, int(queue_capacity)))
        self._stats = RunnerStats()
        self._stats_lock = threading.Lock()
        self._refusing = False

    @property
    def stats(self) -> RunnerStats:
        with self._stats_lock:
            return RunnerStats(
                self._stats.submitted,
                self._stats.completed,
                self._stats.failed,
                self._stats.cancelled_waiters,
                self._stats.in_flight,
            )

    def refuse_new_work(self) -> None:
        self._refusing = True

    async def submit(
        self,
        call: Callable[[], T],
        *,
        admission: asyncio.Semaphore | None = None,
        label: str = "operation",
    ) -> T:
        """Run ``call`` in a worker thread and await it without lying about cancellation."""

        if self._refusing:
            raise AppError(
                "The server is draining and will not admit new work.",
                status=503,
                code="shutting_down",
                retryable=True,
            )
        if admission is not None:
            await admission.acquire()
        acquired = self._slots.acquire(blocking=False)
        if not acquired:
            if admission is not None:
                admission.release()
            raise too_many(
                f"The database worker queue is full ({label}).",
                code="worker_queue_full",
            )
        with self._stats_lock:
            self._stats.submitted += 1
            self._stats.in_flight += 1
        loop = asyncio.get_running_loop()
        task: asyncio.Future[T] = loop.run_in_executor(self._executor, self._guarded, call)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The disk work continues regardless: hold the slot until it settles.
            with self._stats_lock:
                self._stats.cancelled_waiters += 1
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            try:
                if not task.cancelled():
                    task.result()
            except BaseException:
                pass
            raise
        finally:
            self._slots.release()
            if admission is not None:
                admission.release()

    def _guarded(self, call: Callable[[], T]) -> T:
        try:
            return call()
        except BaseException:
            with self._stats_lock:
                self._stats.failed += 1
            raise
        finally:
            with self._stats_lock:
                self._stats.completed += 1
                self._stats.in_flight -= 1

    async def submit_thread(self, call: Callable[[], T]) -> T:
        """Run blocking work on the executor without occupying a semaphore slot.

        Used by WebSocket sessions, which own their own lane and are already bounded by
        ``max_ws_sessions``; parking them on a queue slot would double-count the bound.
        """

        if self._refusing:
            raise AppError(
                "The server is draining and will not admit new work.",
                status=503,
                code="shutting_down",
                retryable=True,
            )
        loop = asyncio.get_running_loop()
        with self._stats_lock:
            self._stats.submitted += 1
            self._stats.in_flight += 1
        try:
            return await loop.run_in_executor(self._executor, self._guarded, call)
        finally:
            pass

    def shutdown(self, *, wait: bool = True) -> None:
        self._refusing = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

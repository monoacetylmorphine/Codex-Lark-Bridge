"""Limit synchronous requests; after caller cancellation, capacity is released by the actual thread completion callback."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import Any, TypeVar

T = TypeVar("T")


class RequestCapacityError(RuntimeError):
    """Synchronous request capacity is full; the caller must fail explicitly rather than continue queueing."""


class BoundedExecutor:
    def __init__(self, capacity: int = 8) -> None:
        """按指定容量创建同步请求线程池及容量信号量，不启动业务请求。"""
        # Source: initial capacity policy for up to 5 concurrent internal tasks, not a load-test result.
        self._slots = threading.BoundedSemaphore(capacity)
        self._pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="lark-http")

    async def call(
        self,
        operation: Callable[..., T],
        *args: Any,
        timeout: float,
        **kwargs: Any,
    ) -> T:
        """复制当前上下文并在线程池中执行同步操作，在指定超时内等待结果。

        容量耗尽时立即抛出 RequestCapacityError，不排队等待名额；超时或取消仅停止调用方等待，
        已运行线程继续占用名额直到实际完成，迟到异常会被消费。
        """
        if not self._slots.acquire(blocking=False):
            raise RequestCapacityError("Lark requests are busy and the in-flight request limit has been reached. Please try again later")
        try:
            context = copy_context()
            future = self._pool.submit(context.run, partial(operation, *args, **kwargs))
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        wrapped = asyncio.wrap_future(future)
        # Consume late exceptions after await cancellation; do not release in-flight capacity early.
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(wrapped)
        except BaseException:
            # Pending work may be cancelled; running threads still occupy capacity.
            future.cancel()
            raise

    def close(self) -> None:
        """停止线程池接收新任务并取消未启动的任务；不等待或强行终止已运行的线程。"""
        self._pool.shutdown(wait=False, cancel_futures=True)


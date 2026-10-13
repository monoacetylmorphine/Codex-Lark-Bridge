"""Limit capacity before thread callbacks create coroutines and consume handler Future exceptions."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import CancelledError, Future
from typing import Any, TypeVar

from codex_bridge.utils.terminal_log import get_logger

EventT = TypeVar("EventT")

logger = get_logger("Event")


class EventDispatcher:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        capacity: int = 32,
        controls: int = 4,
    ) -> None:
        """绑定目标事件循环，分别为普通事件和控制事件创建独立容量限制。"""
        # Source: initial protection limits for small internal deployments, with separate reserved capacity for control events.
        self.loop = loop
        self.normal = threading.BoundedSemaphore(capacity)
        self.controls = threading.BoundedSemaphore(controls)

    def submit(
        self,
        handler: Callable[[EventT], Coroutine[Any, Any, None]],
        data: EventT,
        *,
        control: bool = False,
    ) -> bool:
        """从线程回调向目标事件循环提交异步处理器，成功入队返回 True，容量不足返回 False。

        返回 True 不代表业务处理成功；任务结束时释放名额并消费异常，创建或调度失败则清理后原样抛出。
        """
        slots = self.controls if control else self.normal
        if not slots.acquire(blocking=False):
            logger.error("Inbound event capacity is full: control=%s", control)
            return False
        coroutine = None
        try:
            coroutine = handler(data)
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException:
            if coroutine is not None:
                coroutine.close()
            slots.release()
            raise
        def done(completed: Future[None]) -> None:
            """归还事件容量并消费 Future 结果，忽略取消，记录其他处理异常。"""
            slots.release()
            try:
                completed.result()
            except CancelledError:
                pass
            except Exception:
                logger.exception("Inbound event handling failed: control=%s", control)
        future.add_done_callback(done)
        return True

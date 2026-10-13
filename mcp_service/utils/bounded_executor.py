"""限制同步请求数量；调用方取消后，名额由真实线程完成回调归还。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
import threading


class RequestCapacityError(RuntimeError):
    """同步请求容量已满，调用方应明确失败，不能继续排队。"""


class BoundedExecutor:
    def __init__(self, capacity=8):
        # 来源：内部最多 5 个任务并发的初始容量策略，尚非压测结论。
        self._slots = threading.BoundedSemaphore(capacity)
        self._pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="lark-http")

    async def call(self, operation, *args, timeout, **kwargs):
        if not self._slots.acquire(blocking=False):
            raise RequestCapacityError("飞书请求繁忙，实际在途请求已达上限，请稍后重试")
        try:
            context = copy_context()
            future = self._pool.submit(context.run, partial(operation, *args, **kwargs))
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        wrapped = asyncio.wrap_future(future)
        # await 取消后仍消费迟到的异常，不提前归还实际在途名额。
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(wrapped)
        except BaseException:
            # 尚未开始的工作可取消；已开始的线程仍占用名额。
            future.cancel()
            raise

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


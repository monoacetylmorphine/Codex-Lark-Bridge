"""Inject blocking requests offline to verify actual thread capacity and event admission in two independent services."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import importlib.util
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from bridge.utils import bounded_executor as codex_executor
from bridge.utils.event_dispatcher import EventDispatcher


def mcp_executor():
    path = Path(__file__).resolve().parents[2] / "mcp_service/utils/bounded_executor.py"
    spec = importlib.util.spec_from_file_location("mcp_executor_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RequestLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_keeps_slot_until_worker_really_finishes(self) -> None:
        for module in (codex_executor, mcp_executor()):
            pool = module.BoundedExecutor(1)
            release = threading.Event()
            entered = threading.Event()
            exited = threading.Event()
            def blocked():
                entered.set()
                release.wait(2)
                exited.set()
                raise OSError("Late network error")
            try:
                task = asyncio.create_task(pool.call(blocked, timeout=0.03))
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                with self.assertRaises(TimeoutError):
                    await task
                self.assertFalse(exited.is_set())
                with self.assertRaises(module.RequestCapacityError):
                    await pool.call(lambda: None, timeout=1)
                release.set()
                while not exited.is_set():
                    await asyncio.sleep(0.001)
                await asyncio.sleep(0.01)
                self.assertEqual(await pool.call(lambda: 42, timeout=1), 42)
            finally:
                release.set()
                pool.close()

    async def test_cancellation_and_five_concurrent_workers(self) -> None:
        for module in (codex_executor, mcp_executor()):
            pool = module.BoundedExecutor(5)
            release = threading.Event()
            count = 0
            lock = threading.Lock()
            context = ContextVar("request_context", default="missing")
            context.set("run-id")
            def blocked():
                nonlocal count
                with lock:
                    count += 1
                release.wait(2)
                return context.get()
            tasks = [asyncio.create_task(pool.call(blocked, timeout=1)) for _ in range(5)]
            try:
                while count < 5:
                    await asyncio.sleep(0.001)
                tasks[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[0]
                with self.assertRaises(module.RequestCapacityError):
                    await pool.call(lambda: None, timeout=1)
                release.set()
                self.assertEqual(await asyncio.gather(*tasks[1:]), ["run-id"] * 4)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                pool.close()

    async def test_event_capacity_preserves_control_slot_and_observes_errors(self) -> None:
        dispatcher = EventDispatcher(asyncio.get_running_loop(), capacity=1, controls=1)
        release = asyncio.Event()
        entered = asyncio.Event()
        stopped = asyncio.Event()
        async def handler(data):
            entered.set()
            await release.wait()
            raise OSError("Simulated processing failure")
        async def control(data):
            stopped.set()
        with patch("bridge.utils.event_dispatcher.logger") as logger:
            self.assertTrue(dispatcher.submit(handler, None))
            await entered.wait()
            self.assertFalse(dispatcher.submit(handler, None))
            self.assertTrue(dispatcher.submit(control, None, control=True))
            await asyncio.wait_for(stopped.wait(), 1)
            release.set()
            for _ in range(10):
                await asyncio.sleep(0)
            logger.exception.assert_called_once()
            self.assertTrue(dispatcher.submit(control, None))
            for _ in range(10):
                await asyncio.sleep(0)

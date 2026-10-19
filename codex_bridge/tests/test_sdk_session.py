"""Verify that SDK sessions use user configuration directly."""

from __future__ import annotations
import unittest
from unittest.mock import AsyncMock, patch

from codex_bridge.codex import codex_runtime, thread_manager
from codex_bridge.session import session_codex, session_history
from codex_bridge.codex import codex_execution as codex


class SdkSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_preserves_user_configuration(self) -> None:
        manager = AsyncMock()
        manager._client = None
        with patch.object(codex_runtime, "AsyncCodex", return_value=manager) as factory:
            async with codex.Codex._session(None) as client:
                self.assertIs(client, manager.__aenter__.return_value)
            factory.assert_called_once_with()
        manager.__aexit__.assert_awaited_once()


class ControlSessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import asyncio
        from types import SimpleNamespace
        self.asyncio = asyncio
        self.NS = SimpleNamespace
        self.cls = codex.Codex
        for name in ('_clients', '_processes', '_initializers', '_control_cleanup'):
            self.enterContext(patch.object(self.cls, name, {}))
        self.enterContext(patch.object(self.cls, '_closed_runs', set()))
        self.manager = AsyncMock()
        self.manager._client = SimpleNamespace(_sync=SimpleNamespace(_proc=None))
        self.factory = self.enterContext(patch.object(codex_runtime, 'AsyncCodex', return_value=self.manager))
        self.enterContext(patch.dict(codex_runtime.settings(), cleanupTimeoutSeconds=0.05))
        self.enterContext(patch.object(codex_runtime.logger, 'exception'))

    async def test_control_sessions_have_distinct_ids_and_release_indexes(self) -> None:
        async with self.cls._session(None):
            first = next(iter(self.cls._clients))
            async with self.cls._session(None):
                self.assertEqual(len(self.cls._clients), 2)
        self.assertTrue(first.startswith('control-'))
        for name in ('_clients', '_processes', '_initializers', '_closed_runs', '_control_cleanup'):
            self.assertFalse(getattr(self.cls, name), name)

    async def test_force_close_success_allows_reset_without_replaying_archive(self) -> None:
        self.manager.__aexit__.side_effect = RuntimeError('close failed')
        with patch.object(self.cls, 'force_close', AsyncMock(return_value=True)) as close, \
                patch.object(thread_manager, 'get_user_thread', AsyncMock(return_value='old')), \
                patch.object(thread_manager, 'set_user_thread', AsyncMock()) as save:
            await self.cls.reset_thread('user')
        close.assert_awaited_once()
        self.manager.__aenter__.return_value.thread_archive.assert_awaited_once_with(thread_id='old')
        save.assert_awaited_once_with('user', None)
        self.assertFalse(self.cls._clients)
        self.assertFalse(self.cls._control_cleanup)

    async def test_unconfirmed_close_prevents_reset_and_restore_binding_writes(self) -> None:
        self.manager.__aexit__.side_effect = RuntimeError('close failed')
        with patch.object(self.cls, 'force_close', AsyncMock(return_value=False)), \
                patch.object(thread_manager, 'get_user_thread', AsyncMock(return_value='old')), \
                patch.object(thread_manager, 'set_user_thread', AsyncMock()) as save, \
                patch.object(session_history, 'get_session', AsyncMock(return_value=object())), \
                patch.object(session_codex, '_schedule_session_title'), \
                patch.object(session_history, 'update_session_time', AsyncMock()) as update:
            for operation in (self.cls.reset_thread('user'), self.cls.restore_session('user', 'target')):
                with self.assertRaisesRegex(RuntimeError, 'unverified'):
                    await operation
            save.assert_not_awaited()
            update.assert_not_awaited()
        self.assertEqual(len(self.cls._control_cleanup), 2)

    async def test_repeat_cancellation_during_close_does_not_swallow_cancel(self) -> None:
        entered, release = self.asyncio.Event(), self.asyncio.Event()
        async def close(*args):
            entered.set()
            await release.wait()
        self.manager.__aexit__.side_effect = close
        saved = AsyncMock()
        async def run():
            async with self.cls._session(None):
                pass
            await saved()
        task = self.asyncio.create_task(run())
        await entered.wait()
        task.cancel()
        await self.asyncio.sleep(0)
        task.cancel()
        release.set()
        with self.assertRaises(self.asyncio.CancelledError):
            await task
        saved.assert_not_awaited()
        self.assertFalse(self.cls._clients)

    async def test_cancelled_initialization_is_reaped_by_background_retry(self) -> None:
        from unittest.mock import Mock
        entered, release = self.asyncio.Event(), self.asyncio.Event()
        proc = Mock()
        proc.poll.return_value = 0
        async def initialize():
            entered.set()
            await release.wait()
            self.manager._client._sync._proc = proc
        self.manager.__aenter__.side_effect = initialize
        async def run():
            async with self.cls._session(None):
                self.fail('Business operations must not be submitted after cancellation')
        task = self.asyncio.create_task(run())
        await entered.wait()
        task.cancel()
        with self.assertRaises(self.asyncio.CancelledError):
            await task
        session_id = next(iter(self.cls._control_cleanup))
        release.set()
        await self.cls._initializers[session_id]
        await self.cls.cleanup_control_sessions()
        proc.wait.assert_called_once()
        self.assertFalse(self.cls._control_cleanup)
        self.assertFalse(self.cls._clients)

    async def test_failed_background_cleanup_backs_off_and_expires(self) -> None:
        from codex_bridge.session.session_gateway import RETENTION_SECONDS
        self.cls._control_cleanup['control'] = (100, 100)
        with patch.object(codex_runtime.time, 'monotonic', return_value=100), \
                patch.object(self.cls, 'force_close', AsyncMock(return_value=False)) as close:
            await self.cls.cleanup_control_sessions()
            await self.cls.cleanup_control_sessions()
            close.assert_awaited_once()
        with patch.object(codex_runtime.time, 'monotonic', return_value=100 + RETENTION_SECONDS), \
                patch.object(self.cls, 'discard_expired_run', AsyncMock()) as discard:
            await self.cls.cleanup_control_sessions()
            discard.assert_awaited_once_with('control')
        self.assertFalse(self.cls._control_cleanup)

    async def test_control_close_failure_reaps_real_owned_process(self) -> None:
        import subprocess
        import sys
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.manager._client._sync._proc = proc
        self.manager.__aexit__.side_effect = RuntimeError('normal close failed')
        async def terminate():
            proc.terminate()
            self.manager._client._sync._proc = None
        self.manager.close.side_effect = terminate
        try:
            async with self.cls._session(None):
                pass
            self.assertIsNotNone(proc.poll())
            self.assertFalse(self.cls._clients)
            self.assertFalse(self.cls._processes)
            self.assertFalse(self.cls._control_cleanup)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2)

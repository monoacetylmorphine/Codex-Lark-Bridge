"""Verify real Git initialization, slow-initialization isolation, and process cleanup on timeout or cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from openai_codex.types import TurnStatus
from codex_bridge.codex import codex_execution, codex_runtime, thread_manager
from codex_bridge.session import session_codex, session_history
from codex_bridge.codex import codex_execution as codex
from codex_bridge.codex import codex_workspace as module


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch.object(module, '_initialize_environment', AsyncMock()))
        self.enterContext(patch.object(session_history, "register_session", AsyncMock()))
        self.enterContext(patch.object(session_codex, "_initialize_session_name", AsyncMock()))
        self.enterContext(patch.object(session_codex, "_sync_session_time", AsyncMock()))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.children = []
        self.started = asyncio.Event()
        self.real_create = asyncio.create_subprocess_exec

    async def sleeping_git(self, *args, **kwargs):
        child = await self.real_create(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        self.children.append(child)
        self.started.set()
        return child

    async def asyncTearDown(self) -> None:
        for child in self.children:
            if child.returncode is None:
                child.kill()
                await child.communicate()

    async def test_initializes_once_and_preserves_agents(self) -> None:
        await module.prepare_workspace(self.root, 5)
        self.assertTrue((self.root / '.git').is_dir())
        (self.root / 'AGENTS.md').write_text('Preserve original instructions')
        with patch.object(module, '_initialize_git', AsyncMock()) as init:
            await module.prepare_workspace(self.root, 5)
        init.assert_not_awaited()
        self.assertEqual((self.root / 'AGENTS.md').read_text(), 'Preserve original instructions')

    async def test_git_worktree_file_is_already_initialized(self) -> None:
        (self.root / '.git').write_text('gitdir: /synthetic/worktree')
        with patch.object(module, '_initialize_git', AsyncMock()) as init:
            await module.prepare_workspace(self.root, 5)
        init.assert_not_awaited()

    async def test_slow_initialization_does_not_block_another_session(self) -> None:
        fast = self.root / 'fast'
        fast.mkdir()
        (fast / '.git').mkdir()
        async def stream():
            yield NS(method='turn/completed', payload=NS(turn=NS(
                status=TurnStatus.completed, duration_ms=1)))
        handle = NS(id='turn', stream=stream)
        thread = NS(id='thread', turn=AsyncMock(return_value=handle))
        with patch.object(module.asyncio, 'create_subprocess_exec', self.sleeping_git), \
             patch.dict(codex_execution.CONFIG['storage'], workspaceRoot=str(self.root)), \
             patch.object(thread_manager, 'get_user_thread', AsyncMock(return_value=None)), \
             patch.object(thread_manager, 'set_user_thread', AsyncMock()), \
             patch.object(codex_runtime, 'AsyncCodex') as factory:
            factory.return_value.__aenter__.return_value.thread_start.return_value = thread
            slow = asyncio.create_task(module.prepare_workspace(self.root / 'slow', 10))
            try:
                await asyncio.wait_for(self.started.wait(), 2)
                async def other_session():
                    return [event async for event in codex.Codex.running('fast', 'hello')]
                events = await asyncio.wait_for(other_session(), 1)
                self.assertEqual(events[-1]['type'], 'done')
                self.assertFalse(slow.done())
            finally:
                slow.cancel()
                await asyncio.gather(slow, return_exceptions=True)
        self.assertIsNotNone(self.children[0].returncode)

    async def test_timeout_reaps_child(self) -> None:
        with patch.object(module.asyncio, 'create_subprocess_exec', self.sleeping_git):
            with self.assertRaises(TimeoutError):
                await module.prepare_workspace(self.root, 0.1)
        self.assertIsNotNone(self.children[0].returncode)

    async def test_cancel_during_late_creation_reaps_child(self) -> None:
        release = asyncio.Event()
        async def delayed(*args, **kwargs):
            child = await self.sleeping_git(*args, **kwargs)
            await release.wait()
            return child
        with patch.object(module.asyncio, 'create_subprocess_exec', delayed):
            task = asyncio.create_task(module.prepare_workspace(self.root, 10))
            await asyncio.wait_for(self.started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertIsNotNone(self.children[0].returncode)

    async def test_init_failure_prevents_model_submission(self) -> None:
        with patch.object(thread_manager, 'get_user_thread', AsyncMock(return_value=None)), \
             patch.object(codex_execution, 'prepare_workspace', AsyncMock(side_effect=TimeoutError)), \
             patch.object(codex_runtime, 'AsyncCodex') as factory:
            events = [event async for event in codex.Codex.running('user', 'hello')]
        self.assertEqual(events[0]['type'], 'error')
        factory.assert_not_called()

    async def test_nonzero_git_exit_is_reported(self) -> None:
        async def failing(*args, **kwargs):
            return await self.real_create(sys.executable, '-c', 'raise SystemExit(7)', **kwargs)
        with patch.object(module.asyncio, 'create_subprocess_exec', failing):
            with self.assertRaises(subprocess.CalledProcessError):
                await module.prepare_workspace(self.root, 5)

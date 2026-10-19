"""Verify binding persistence using temporary SQLite files without accessing the real state.db or Codex service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from codex_bridge.codex import codex_execution, codex_runtime, thread_manager
from codex_bridge.session import session_history
from codex_bridge.codex import codex_execution as codex, thread_manager as thread_management


class ThreadManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "nested" / "state.db"
        patcher = patch.object(thread_management, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.enterContext(patch.object(session_history, "DB_PATH", self.db_path))

    async def test_missing_user_creates_database_and_table(self) -> None:
        self.assertIsNone(await thread_management.get_user_thread("missing"))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM user_thread").fetchall(), [])

    async def test_binding_update_and_reset_survive_new_connections(self) -> None:
        await thread_management.set_user_thread("user-1", "thread-1")
        self.assertEqual(await thread_management.get_user_thread("user-1"), "thread-1")
        await thread_management.set_user_thread("user-1", "thread-2")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM user_thread").fetchall(),
                             [("user-1", "thread-2")])
        await thread_management.set_user_thread("user-1", None)
        self.assertIsNone(await thread_management.get_user_thread("user-1"))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM user_thread").fetchall(),
                             [("user-1", None)])

    async def test_parallel_users_and_parameterized_values(self) -> None:
        users = [f"user-{index}" for index in range(10)] + ["'; DROP TABLE user_thread; --"]
        await asyncio.gather(*(
            thread_management.set_user_thread(user, f"thread:{user}") for user in users
        ))
        for user in users:
            self.assertEqual(await thread_management.get_user_thread(user), f"thread:{user}")

    def fail_initialization(self, code, attempts=None):
        """注入 WAL 初始化错误，未失败的操作仍使用真实 SQLite。"""
        original = thread_manager.aiosqlite.Connection.execute
        connections = []

        @asynccontextmanager
        async def failing():
            error = sqlite3.OperationalError("injected initialization failure")
            if code is not None:
                error.sqlite_errorcode = code
            raise error
            yield

        def execute(connection, sql, *args, **kwargs):
            if sql == "PRAGMA journal_mode=WAL" and (attempts is None or len(connections) < attempts):
                connections.append(connection)
                return failing()
            return original(connection, sql, *args, **kwargs)

        self.enterContext(patch.object(thread_manager.aiosqlite.Connection, "execute", execute))
        return connections

    async def test_real_reader_lock_retries_after_release(self) -> None:
        """真实读事务阻止首次切换 WAL，观察到 BUSY 后释放锁，不依赖睡眠碰时序。"""
        self.db_path.parent.mkdir(parents=True)
        reader = self.enterContext(closing(sqlite3.connect(self.db_path)))
        reader.execute("CREATE TABLE hold (value INTEGER)")
        reader.commit()
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM hold").fetchall()
        original = thread_manager.aiosqlite.Connection.execute
        failed = []

        @asynccontextmanager
        async def observe(connection):
            try:
                async with original(connection, "PRAGMA journal_mode=WAL") as cursor:
                    yield cursor
            except sqlite3.OperationalError as error:
                self.assertEqual(error.sqlite_errorcode & 0xff, sqlite3.SQLITE_BUSY)
                failed.append(connection)
                reader.rollback()
                raise

        def execute(connection, sql, *args, **kwargs):
            if sql == "PRAGMA journal_mode=WAL":
                return observe(connection)
            return original(connection, sql, *args, **kwargs)

        with patch.object(thread_manager.aiosqlite.Connection, "execute", execute):
            await asyncio.wait_for(thread_manager.set_user_thread("user", "thread"), timeout=2)
        self.assertEqual(await thread_manager.get_user_thread("user"), "thread")
        self.assertEqual(len(failed), 1)
        with self.assertRaises(ValueError):
            await failed[0].execute("SELECT 1")

    async def test_extended_busy_retries_and_restores_business_timeout(self) -> None:
        connections = self.fail_initialization(sqlite3.SQLITE_BUSY_RECOVERY, attempts=1)
        await thread_manager.set_user_thread("user", "thread")
        self.assertEqual(len(connections), 1)
        with self.assertRaises(ValueError):
            await connections[0].execute("SELECT 1")
        async with thread_manager._connect() as db:
            async with db.execute("PRAGMA busy_timeout") as cursor:
                self.assertEqual((await cursor.fetchone())[0], 30000)

    async def test_initialization_busy_has_a_deadline(self) -> None:
        connections = self.fail_initialization(sqlite3.SQLITE_BUSY)
        with patch.object(thread_manager, "INITIALIZATION_TIMEOUT", 0.02):
            with self.assertRaises(sqlite3.OperationalError):
                await asyncio.wait_for(thread_manager.set_user_thread("user", "thread"), timeout=1)
        self.assertGreaterEqual(len(connections), 1)
        for connection in connections:
            with self.assertRaises(ValueError):
                await connection.execute("SELECT 1")

    async def test_non_busy_initialization_errors_are_not_retried(self) -> None:
        for code in (sqlite3.SQLITE_IOERR, sqlite3.SQLITE_LOCKED, None):
            with self.subTest(code=code):
                connections = self.fail_initialization(code)
                with self.assertRaises(sqlite3.OperationalError):
                    await thread_manager.get_user_thread("user")
                self.assertEqual(len(connections), 1)

    async def test_cancellation_during_initialization_retry_propagates(self) -> None:
        connections = self.fail_initialization(sqlite3.SQLITE_BUSY)
        clock = SimpleNamespace(get_running_loop=asyncio.get_running_loop,
                                sleep=AsyncMock(side_effect=asyncio.CancelledError))
        with patch.object(thread_manager, "asyncio", clock):
            with self.assertRaises(asyncio.CancelledError):
                await thread_manager.set_user_thread("user", "thread")
        self.assertEqual(len(connections), 1)
        with self.assertRaises(ValueError):
            await connections[0].execute("SELECT 1")

    async def test_business_busy_is_not_replayed(self) -> None:
        error = sqlite3.OperationalError("business write busy")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        attempts = 0
        with self.assertRaises(sqlite3.OperationalError) as caught:
            async with thread_manager._connect():
                attempts += 1
                raise error
        self.assertIs(caught.exception, error)
        self.assertEqual(attempts, 1)

    async def test_bindings_and_history_initialize_concurrently(self) -> None:
        users = [f"user-{index}" for index in range(10)]
        await asyncio.gather(
            *(thread_manager.set_user_thread(user, user) for user in users),
            *(session_history.register_session(user, user) for user in users),
        )
        for user in users:
            self.assertEqual(await thread_manager.get_user_thread(user), user)
            self.assertIsNone((await session_history.get_session(user, user)).thread_name)

    async def test_database_failure_is_not_silently_ignored(self) -> None:
        self.db_path.parent.mkdir(parents=True)
        self.db_path.mkdir()
        with self.assertRaises(sqlite3.OperationalError):
            await thread_management.set_user_thread("user-1", "thread-1")

    async def test_new_command_archives_and_persists_reset(self) -> None:
        await thread_management.set_user_thread("user-1", "thread-1")
        with patch.object(codex_runtime, "AsyncCodex") as client_class:
            client = client_class.return_value.__aenter__.return_value
            await codex.Codex.reset_thread("user-1")
        client.thread_archive.assert_awaited_once_with(thread_id="thread-1")
        self.assertIsNone(await thread_management.get_user_thread("user-1"))

    async def test_archive_failure_preserves_binding(self) -> None:
        await thread_management.set_user_thread("user-1", "thread-1")
        with patch.object(codex_runtime, "AsyncCodex") as client_class:
            client = client_class.return_value.__aenter__.return_value
            client.thread_archive.side_effect = RuntimeError("archive failed")
            with self.assertRaises(RuntimeError):
                await codex.Codex.reset_thread("user-1")
        self.assertEqual(await thread_management.get_user_thread("user-1"), "thread-1")

    async def test_reset_database_failure_propagates(self) -> None:
        with patch.object(thread_manager, "get_user_thread", AsyncMock(return_value=None)), patch.object(
            thread_manager, "set_user_thread", AsyncMock(side_effect=sqlite3.OperationalError("locked"))
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await codex.Codex.reset_thread("user-1")

    async def test_prompt_new_is_not_a_control_command(self) -> None:
        self.enterContext(patch.object(thread_manager, "get_user_thread", AsyncMock(return_value=None)))
        with patch.object(codex_runtime, "AsyncCodex") as client_class, patch.object(codex_execution, "prepare_workspace", AsyncMock()), patch.object(
            thread_manager, "set_user_thread", AsyncMock(side_effect=sqlite3.OperationalError("locked"))
        ):
            client = client_class.return_value.__aenter__.return_value
            client.thread_start.return_value.id = "new-thread"
            events = [evt async for evt in codex.Codex.running("user-1", "/new")]
            client.thread_archive.assert_not_awaited()
            client.thread_start.assert_awaited_once()
            self.assertEqual(events[0]["type"], "error")

    async def test_binding_failure_prevents_turn_start(self) -> None:
        await thread_management.set_user_thread("user-1", "thread-1")
        with (
            patch.object(codex_runtime, "AsyncCodex") as client_class,
            patch.object(codex_execution, "prepare_workspace", AsyncMock()),
            patch.object(thread_manager, "set_user_thread", new=AsyncMock(
                side_effect=sqlite3.OperationalError("database is locked"),
            )),
        ):
            client = client_class.return_value.__aenter__.return_value
            thread = client.thread_resume.return_value
            thread.id = "thread-1"
            events = [event async for event in codex.Codex.running("user-1", "hello")]
        client.thread_resume.assert_awaited_once()
        thread.turn.assert_not_called()
        self.assertEqual(events[0]["type"], "error")


if __name__ == "__main__":
    unittest.main()

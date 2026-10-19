"""Real SQLite persistence and mocked SDK integration tests for session history, without external services."""

from __future__ import annotations

from codex_bridge.middleware import gateway_commands
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from openai_codex import LocalImageInput
from openai_codex.types import TurnStatus

from codex_bridge.codex import codex_execution, codex_runtime
from codex_bridge.codex import codex_execution as codex, thread_manager
from codex_bridge.session import session_codex, session_history as history
import test_stop_command as helpers


class IndexEntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.index_path = Path(directory.name) / "session_index.jsonl"
        self.enterContext(patch.object(history, "SESSION_INDEX_PATH", self.index_path))

    def write_index(self, *lines: str) -> None:
        self.index_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def test_returns_codex_name_and_iso_time_for_matching_thread(self) -> None:
        self.write_index(
            json.dumps({"id": "other", "thread_name": "ignore", "updated_at": "2026-10-19T00:00:00Z"}),
            json.dumps({"id": "thread-1", "thread_name": "Codex name", "updated_at": "2026-10-19T00:01:00Z"}),
        )
        self.assertEqual(history._read_session_index_entry("thread-1"),
                         history.SessionIndexEntry("Codex name", 1792368060))

    def test_latest_duplicate_entry_wins_and_blank_name_is_none(self) -> None:
        self.write_index(
            json.dumps({"id": "thread-1", "thread_name": "stale", "updated_at": "2026-10-19T00:00:00Z"}),
            json.dumps({"id": "thread-1", "thread_name": "   ", "updated_at": "2026-10-19T00:00:01Z"}),
        )
        self.assertIsNone(history._read_session_index_entry("thread-1").thread_name)

    def test_missing_file_or_malformed_lines_return_none(self) -> None:
        self.assertEqual(history._read_session_index_entry("thread-1"),
                         history.SessionIndexEntry(None, None))
        self.write_index("{not json}",
                         json.dumps({"id": "thread-1", "thread_name": "ok", "updated_at": "2026-10-19T00:00:01Z"}))
        self.assertEqual(history._read_session_index_entry("thread-1").thread_name, "ok")

    def test_invalid_or_missing_iso_time_is_none(self) -> None:
        self.write_index(
            json.dumps({"id": "thread-1", "thread_name": "ok", "updated_at": "not-a-time"}),
            json.dumps({"id": "thread-2", "thread_name": "missing time"}),
        )
        self.assertIsNone(history._read_session_index_entry("thread-1").updated_at)
        self.assertIsNone(history._read_session_index_entry("thread-2").updated_at)

    async def test_lookup_runs_without_blocking_the_event_loop(self) -> None:
        self.write_index(json.dumps({"id": "thread-1", "thread_name": "Codex name", "updated_at": "2026-10-19T00:01:00Z"}))
        entry = await history.lookup_session_index("thread-1")
        self.assertEqual(entry.thread_name, "Codex name")
        self.assertEqual(entry.updated_at, 1792368060)


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.db_path = Path(directory) / "state.sqlite"
        self.enterContext(patch.object(history, "DB_PATH", self.db_path))
        self.enterContext(patch.object(thread_manager, "DB_PATH", self.db_path))
        self.enterContext(patch.object(codex_execution, "prepare_workspace", AsyncMock()))
        self.factory = self.enterContext(patch.object(codex_runtime, "AsyncCodex"))
        self.client = self.factory.return_value.__aenter__.return_value
        self.metadata = NS(name=None, updated_at=100)
        self.index_entries = {"thread-1": history.SessionIndexEntry("first", 100)}

        async def lookup(thread_id):
            return self.index_entries.get(thread_id, history.SessionIndexEntry(None, None))

        self.enterContext(patch.object(history, "lookup_session_index", lookup))

        async def rename(name):
            self.metadata.name = name

        async def stream():
            self.metadata.updated_at = 200
            yield NS(method="turn/completed", payload=NS(turn=NS(
                status=TurnStatus.completed, duration_ms=1)))

        self.thread = NS(id="thread-1", read=AsyncMock(return_value=NS(thread=self.metadata)),
                         set_name=AsyncMock(side_effect=rename),
                         turn=AsyncMock(return_value=NS(id="turn-1", stream=stream)))
        self.client.thread_start.return_value = self.thread
        self.client.thread_resume.return_value = self.thread
        self.client.thread_unarchive.return_value = self.thread

    async def run_prompt(self, prompt="first"):
        return [event async for event in codex.Codex.running("user", prompt)]

    async def register(self, thread_id="thread-1", name="first", timestamp=100):
        async with history._connect() as db:
            await db.execute(history.REGISTER_SESSION_SQL, ("user", thread_id, name))
            await db.commit()
        if timestamp is not None:
            await history.update_session_time("user", thread_id, timestamp)

    async def test_same_database_preserves_original_table_schema(self) -> None:
        await thread_manager.set_user_thread("user", "old")
        await self.register()
        with sqlite3.connect(self.db_path) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(user_thread)")]
        self.assertEqual(columns, ["user_id", "thread_id"])
        self.assertEqual(await thread_manager.get_user_thread("user"), "old")

    async def test_idempotent_registration_and_same_second_sorting(self) -> None:
        await asyncio.gather(*(self.register(tid, "same", stamp) for tid, stamp in (
            ("a", 100), ("b", 200), ("c", 200))))
        async with history._connect() as db:
            await db.execute(history.REGISTER_SESSION_SQL, ("user", "a", "do not overwrite"))
            await db.execute(history.REGISTER_SESSION_SQL, ("other", "private", "secret"))
            await db.commit()
        records = await history.list_sessions("user")
        self.assertEqual([row.thread_id for row in records], ["c", "b", "a"])
        self.assertEqual(records[-1].thread_name, "same")
        self.assertIsNone(await history.get_session("other", "a"))

    async def test_register_session_uses_index_name_and_is_idempotent(self) -> None:
        with patch.object(history, "lookup_session_index", AsyncMock(
                return_value=history.SessionIndexEntry("from index", 100))):
            await history.register_session("user", "thread-1")
        self.assertEqual((await history.get_session("user", "thread-1")).thread_name, "from index")
        with patch.object(history, "lookup_session_index", AsyncMock(
                return_value=history.SessionIndexEntry("later", 200))):
            await history.register_session("user", "thread-1")
        self.assertEqual((await history.get_session("user", "thread-1")).thread_name, "from index")

    async def test_register_session_without_index_name_stores_none(self) -> None:
        with patch.object(history, "lookup_session_index", AsyncMock(
                return_value=history.SessionIndexEntry(None, None))):
            await history.register_session("user", "thread-1")
        self.assertIsNone((await history.get_session("user", "thread-1")).thread_name)

    async def test_sync_session_index_claims_name_and_time_only_when_missing(self) -> None:
        async with history._connect() as db:
            await db.execute(history.REGISTER_SESSION_SQL, ("user", "thread-1", None))
            await db.commit()
        with patch.object(history, "lookup_session_index", AsyncMock(
                return_value=history.SessionIndexEntry("claimed", 200))):
            record = await history.sync_session_index("user", "thread-1")
        self.assertEqual(record.thread_name, "claimed")
        self.assertEqual(record.updated_at, 200)

    def fail_initialization(self, code, attempts=None):
        """仅注入初始化错误，后续连接仍走真实 SQLite。"""
        original = history.aiosqlite.Connection.execute
        connections = []
        @asynccontextmanager
        async def failing():
            error = sqlite3.OperationalError('injected initialization failure')
            error.sqlite_errorcode = code
            raise error
            yield  # 保持异步上下文管理器协议。
        def execute(connection, sql, *args, **kwargs):
            if sql == 'PRAGMA journal_mode=WAL' and (attempts is None or len(connections) < attempts):
                connections.append(connection)
                return failing()
            return original(connection, sql, *args, **kwargs)
        self.enterContext(patch.object(history.aiosqlite.Connection, 'execute', execute))
        return connections

    async def test_initialization_busy_retries_with_closed_connection(self) -> None:
        connections = self.fail_initialization(sqlite3.SQLITE_BUSY, attempts=1)
        await self.register()
        self.assertEqual((await history.get_session('user', 'thread-1')).thread_name, 'first')
        self.assertEqual(len(connections), 1)
        with self.assertRaises(ValueError):
            await connections[0].execute('SELECT 1')
        async with history._connect() as db:
            async with db.execute('PRAGMA busy_timeout') as cursor:
                self.assertEqual((await cursor.fetchone())[0], 30000)

    async def test_initialization_busy_has_a_deadline(self) -> None:
        connections = self.fail_initialization(sqlite3.SQLITE_BUSY)
        with patch.object(history, 'INITIALIZATION_TIMEOUT', 0.02):
            with self.assertRaises(sqlite3.OperationalError):
                await asyncio.wait_for(self.register(), timeout=1)
        self.assertGreaterEqual(len(connections), 1)

    async def test_initialization_other_errors_are_not_retried(self) -> None:
        connections = self.fail_initialization(sqlite3.SQLITE_IOERR)
        with self.assertRaises(sqlite3.OperationalError):
            await self.register()
        self.assertEqual(len(connections), 1)

    async def test_cancellation_during_initialization_retry_propagates(self) -> None:
        connections = self.fail_initialization(sqlite3.SQLITE_BUSY)
        clock = NS(get_running_loop=asyncio.get_running_loop,
                   sleep=AsyncMock(side_effect=asyncio.CancelledError))
        with patch.object(history, 'asyncio', clock):
            with self.assertRaises(asyncio.CancelledError):
                await self.register()
        self.assertEqual(len(connections), 1)
        with self.assertRaises(ValueError):
            await connections[0].execute('SELECT 1')

    async def test_busy_from_caller_is_not_replayed(self) -> None:
        error = sqlite3.OperationalError('business write busy')
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with self.assertRaisesRegex(sqlite3.OperationalError, 'business write busy'):
            async with history._connect():
                raise error

    async def test_time_does_not_regress_and_name_is_fixed(self) -> None:
        await self.register()
        await history.update_session_time("user", "thread-1", 50)
        with patch.object(history, "lookup_session_index", AsyncMock(
                return_value=history.SessionIndexEntry("replacement", 50))):
            await history.sync_session_index("user", "thread-1")
        self.assertEqual(await history.get_session("user", "thread-1"),
                         history.SessionRecord("user", "thread-1", "first", 100))
        with self.assertRaises(ValueError):
            await history.update_session_time("user", "thread-1", None)

    async def test_name_comes_from_index_and_reuse_only_updates_time(self) -> None:
        self.assertEqual(await self.run_prompt(), [{"type": "done"}])
        self.index_entries["thread-1"] = history.SessionIndexEntry("first", 300)
        self.assertEqual(await self.run_prompt("second"), [{"type": "done"}])
        self.thread.set_name.assert_not_awaited()
        self.client.thread_start.assert_awaited_once()
        self.client.thread_resume.assert_awaited_once()
        records = await history.list_sessions("user")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].thread_name, "first")
        self.assertEqual(records[0].updated_at, 300)
        self.assertEqual(await thread_manager.get_user_thread("user"), "thread-1")

    async def test_name_is_claimed_from_index_on_later_sync(self) -> None:
        self.index_entries = {}
        self.assertEqual(await self.run_prompt(), [{"type": "done"}])
        self.assertIsNone((await history.get_session("user", "thread-1")).thread_name)
        self.index_entries["thread-1"] = history.SessionIndexEntry("Codex name", 200)
        await session_codex._sync_session_time("user", self.thread)
        record = await history.get_session("user", "thread-1")
        self.assertEqual(record.thread_name, "Codex name")
        self.assertEqual(record.updated_at, 200)
        self.thread.set_name.assert_not_awaited()

    async def test_attachment_only_prompt_still_uses_index_name(self) -> None:
        await self.run_prompt([LocalImageInput(path="/tmp/image.png")])
        self.assertEqual((await history.get_session("user", "thread-1")).thread_name, "first")
        self.thread.set_name.assert_not_awaited()

    async def test_old_binding_is_not_backfilled_from_current_prompt(self) -> None:
        await thread_manager.set_user_thread("user", "thread-1")
        self.assertEqual(await self.run_prompt(), [{"type": "done"}])
        self.thread.set_name.assert_not_awaited()
        self.thread.read.assert_not_awaited()
        self.assertEqual(await history.list_sessions("user"), [])

    async def test_reset_keeps_history_and_next_thread_gets_new_name(self) -> None:
        await self.run_prompt()
        await codex.Codex.reset_thread("user")
        self.client.thread_archive.assert_awaited_once_with(thread_id="thread-1")
        self.assertIsNone(await thread_manager.get_user_thread("user"))
        self.assertIsNotNone(await history.get_session("user", "thread-1"))
        self.thread.id = "thread-2"
        self.index_entries["thread-2"] = history.SessionIndexEntry("new name", 400)
        await self.run_prompt("new name")
        self.assertEqual(len(await history.list_sessions("user")), 2)
        self.assertEqual((await history.get_session("user", "thread-2")).thread_name, "new name")

    async def test_restore_unarchives_then_replaces_binding_without_renaming(self) -> None:
        await self.register()
        await thread_manager.set_user_thread("user", "old")
        self.index_entries["thread-1"] = history.SessionIndexEntry("first", 300)
        await codex.Codex.restore_session("user", "thread-1")
        self.client.thread_unarchive.assert_awaited_once_with("thread-1")
        self.thread.set_name.assert_not_awaited()
        self.client.thread_archive.assert_not_awaited()
        self.assertEqual(await thread_manager.get_user_thread("user"), "thread-1")
        self.assertEqual((await history.get_session("user", "thread-1")).updated_at, 300)

    async def test_restore_ownership_and_current_thread_short_circuit(self) -> None:
        await self.register()
        with self.assertRaises(ValueError):
            await codex.Codex.restore_session("other", "thread-1")
        await thread_manager.set_user_thread("user", "thread-1")
        await codex.Codex.restore_session("user", "thread-1")
        self.factory.assert_not_called()

    async def test_restore_failure_preserves_binding(self) -> None:
        await self.register()
        await thread_manager.set_user_thread("user", "old")
        for target, name in ((self.client, "thread_unarchive"), (history, "sync_session_index")):
            with self.subTest(name=name), patch.object(target, name, AsyncMock(side_effect=RuntimeError("offline"))):
                with self.assertRaises(RuntimeError):
                    await codex.Codex.restore_session("user", "thread-1")
            self.assertEqual(await thread_manager.get_user_thread("user"), "old")
        with patch.object(thread_manager, "set_user_thread", AsyncMock(side_effect=OSError("disk"))):
            with self.assertRaises(OSError):
                await codex.Codex.restore_session("user", "thread-1")
        self.assertEqual(await thread_manager.get_user_thread("user"), "old")
        self.client.thread_start.assert_not_awaited()

    async def test_turn_sync_failure_is_logged_without_replaying_turn(self) -> None:
        await self.register()
        await thread_manager.set_user_thread("user", "thread-1")
        # First call is the pre-turn name sync; only the post-turn sync fails.
        with patch.object(history, "sync_session_index",
                          AsyncMock(side_effect=[None, RuntimeError("offline")])), \
                patch.object(session_codex.logger, "exception") as log:
            self.assertEqual(await self.run_prompt(), [{"type": "done"}])
        log.assert_called_once()
        self.thread.turn.assert_awaited_once()

    async def test_history_registration_failure_prevents_binding_and_turn(self) -> None:
        with patch.object(history, "register_session", AsyncMock(side_effect=OSError("disk"))):
            self.assertEqual((await self.run_prompt())[0]["type"], "error")
        self.assertIsNone(await thread_manager.get_user_thread("user"))
        self.thread.turn.assert_not_awaited()

    async def test_named_record_without_time_gets_synced(self) -> None:
        await self.register(timestamp=None)
        await session_codex._sync_session_time("user", self.thread)
        self.assertEqual((await history.get_session("user", "thread-1")).updated_at, 100)
        self.thread.read.assert_not_awaited()

    async def test_steer_claims_index_name_for_unnamed_thread(self) -> None:
        async with history._connect() as db:
            await db.execute(history.REGISTER_SESSION_SQL, ("user", "thread-1", None))
            await db.commit()
        await history.update_session_time("user", "thread-1", 100)
        self.metadata.status = NS(root=NS(type="active"))
        handle = NS(id="turn-1", steer=AsyncMock(return_value=NS(turn_id="turn-1")))
        route = codex_execution.CONFIG["codex"]["models"]["text"]
        live = codex.LiveTurn(self.thread, handle, route["model"], route["provider"], user_id="user")
        with patch.object(codex.Codex, "_live_turns", {"history-run": live}):
            self.assertEqual(await codex.Codex.steer("history-run", "first text"), {"type": "steered"})
            self.assertEqual(await codex.Codex.steer("history-run", "later text"), {"type": "steered"})
        self.thread.set_name.assert_not_awaited()
        self.assertEqual((await history.get_session("user", "thread-1")).thread_name, "first")

    async def test_activity_update_moves_existing_record_to_top(self) -> None:
        await self.register("a", "first", 100)
        await self.register("b", "second", 200)
        await history.update_session_time("user", "a", 300)
        records = await history.list_sessions("user")
        self.assertEqual([row.thread_id for row in records], ["a", "b"])
        self.assertEqual(records[0].thread_name, "first")

    async def test_sync_timeout_does_not_hang_or_advance_time(self) -> None:
        await self.register()
        async def stuck(user_id, thread_id):
            await asyncio.Event().wait()
        with patch.object(history, "sync_session_index", stuck), \
                patch.dict(codex_runtime.settings(), cleanupTimeoutSeconds=0.01), \
                patch.object(session_codex.logger, "exception") as log:
            await asyncio.wait_for(session_codex._sync_session_time("user", self.thread), 1)
        log.assert_called_once()
        self.assertEqual((await history.get_session("user", "thread-1")).updated_at, 100)

    async def test_failed_and_interrupted_turns_refresh_time(self) -> None:
        await self.register()
        await thread_manager.set_user_thread("user", "thread-1")
        expected = 100
        for status in (TurnStatus.failed, TurnStatus.interrupted):
            expected += 1
            self.index_entries["thread-1"] = history.SessionIndexEntry("first", expected)
            async def stream():
                yield NS(method="turn/completed", payload=NS(turn=NS(
                    status=status, error=None, duration_ms=1)))
            self.thread.turn.return_value.stream = stream
            await self.run_prompt()
            self.assertEqual((await history.get_session("user", "thread-1")).updated_at, expected)


class HistoryGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        helpers.StopTests.setUp(self)
        self.runtime.codex.restore_session = AsyncMock()
        self.get_session = self.enterContext(patch.object(gateway_commands, "get_session", AsyncMock(return_value=object())))
        self.get_active = self.enterContext(patch.object(gateway_commands, "get_user_thread", AsyncMock(return_value="old")))

    async def test_options_keep_same_names_distinct(self) -> None:
        records = [history.SessionRecord("user-1", tid, "same", 100) for tid in ("a", "b")]
        with patch.object(gateway_commands, "list_sessions", AsyncMock(return_value=records)) as listing:
            result = await self.commands.history_options(helpers.event())
        listing.assert_awaited_once_with("user-1")
        self.assertEqual([item["value"] for item in result], ["a", "b"])
        self.assertEqual([item["label"] for item in result], ["same", "same"])

    async def test_foreign_thread_does_not_stop_current_run(self) -> None:
        self.get_session.return_value = None
        with patch.object(self.commands, "_stop_chat", AsyncMock()) as stop:
            result = await self.commands.processing_history_restore(helpers.event(), "foreign")
        self.assertEqual(result, {"ok": False, "content": "Failed to restore the previous session"})
        stop.assert_not_awaited()
        self.runtime.codex.restore_session.assert_not_awaited()

    async def test_current_selection_does_not_stop(self) -> None:
        self.get_active.return_value = "chosen"
        with patch.object(self.commands, "_stop_chat", AsyncMock()) as stop:
            result = await self.commands.processing_history_restore(helpers.event(), "chosen")
        self.assertTrue(result["ok"])
        stop.assert_not_awaited()

    async def test_stop_failure_prevents_restore(self) -> None:
        helpers.StopTests.state(self)
        self.runtime.codex.interrupt_and_confirm.return_value = False
        result = await self.commands.processing_history_restore(helpers.event(), "chosen")
        self.assertFalse(result["ok"])
        self.runtime.codex.restore_session.assert_not_awaited()
        self.assertFalse(self.runtime.cache.reset_tasks)

    async def test_restore_failure_returns_frontend_error(self) -> None:
        self.runtime.codex.restore_session.side_effect = RuntimeError("unarchive failed")
        result = await self.commands.processing_history_restore(helpers.event(), "chosen")
        self.assertEqual(result, {"ok": False, "content": "Failed to restore the previous session"})
        self.assertFalse(self.runtime.cache.reset_tasks)

    async def test_new_input_waits_for_restore(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        async def restore(*args):
            entered.set()
            await release.wait()
        self.runtime.codex.restore_session.side_effect = restore
        task = asyncio.create_task(self.commands.processing_history_restore(helpers.event(), "chosen"))
        await asyncio.wait_for(entered.wait(), 1)
        self.router._process_chat_history = AsyncMock()
        message = asyncio.create_task(self.router.processing(helpers.event("hello", message_id="next")))
        try:
            await asyncio.sleep(0)
            self.router._process_chat_history.assert_not_awaited()
        finally:
            release.set()
        result, _ = await asyncio.wait_for(asyncio.gather(task, message), 1)
        self.assertTrue(result["ok"])
        self.runtime.codex.restore_session.assert_awaited_once_with("user-1", "chosen")
        self.router._process_chat_history.assert_awaited_once()
        self.assertFalse(self.runtime.cache.reset_tasks)

    async def test_group_history_uses_existing_chat_binding_scope(self) -> None:
        await self.commands.processing_history_restore(helpers.event(chat_type="group"), "chosen")
        self.get_session.assert_awaited_once_with("chat-1", "chosen")
        self.runtime.codex.restore_session.assert_awaited_once_with("chat-1", "chosen")

    async def test_parallel_selections_are_serialized(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def restore(user_id, thread_id):
            calls.append(thread_id)
            if thread_id == "first":
                entered.set()
                await release.wait()
        self.runtime.codex.restore_session.side_effect = restore
        first = asyncio.create_task(self.commands.processing_history_restore(helpers.event(), "first"))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.commands.processing_history_restore(helpers.event(), "second"))
        try:
            await asyncio.sleep(0)
            self.assertEqual(calls, ["first"])
        finally:
            release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(calls, ["first", "second"])
        self.assertFalse(self.runtime.cache.reset_tasks)


if __name__ == "__main__":
    unittest.main()

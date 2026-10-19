"""Real SQLite persistence, thread_items reads, and mocked SDK/LLM integration tests for session history."""

from __future__ import annotations

from codex_bridge.middleware import gateway_commands
import asyncio
from contextlib import asynccontextmanager
import json
import os
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


def _thread_item(thread_id, turn_id, item_id, created_at_ms, item_json, item_type, rollout_ordinal=0):
    return (thread_id, turn_id, item_id, rollout_ordinal, created_at_ms,
            json.dumps(item_json), item_type, 0, created_at_ms, created_at_ms)


def _create_thread_history(path: Path, rows) -> None:
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE thread_items (
            thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER,
            created_at_ms INTEGER, item_json TEXT, item_type TEXT,
            updated_at_ordinal INTEGER, started_at_ms INTEGER, completed_at_ms INTEGER)""")
        db.executemany("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def _insert_thread_items(path: Path, rows) -> None:
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


def _replace_thread_items(path: Path, rows) -> None:
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM thread_items")
        db.executemany("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?,?,?)", rows)


class ThreadItemReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.thread_history_path = Path(directory) / "thread_history.sqlite"
        self.enterContext(patch.object(history, "THREAD_HISTORY_DB_PATH", self.thread_history_path))
        _create_thread_history(self.thread_history_path, [])

    def test_extract_item_text_keeps_only_user_and_agent_text(self) -> None:
        user = json.dumps({"type": "userMessage", "content": [
            {"type": "text", "text": "  hello "},
            {"type": "image", "source": "s3"},
        ]})
        agent = json.dumps({"type": "agentMessage", "text": "  answer "})
        self.assertEqual(history._extract_item_text(user), "hello")
        self.assertEqual(history._extract_item_text(agent), "answer")
        self.assertEqual(history._extract_item_text(json.dumps({"type": "reasoning", "summary": "x"})), "")
        self.assertEqual(history._extract_item_text("{not json"), "")

    async def test_earliest_turn_picks_min_created_at_and_skips_null(self) -> None:
        _insert_thread_items(self.thread_history_path, [
            _thread_item("t", "later", "i1", 300, {"type": "userMessage", "content": [{"type": "text", "text": "a"}]}, "userMessage"),
            _thread_item("t", "first", "i2", 100, {"type": "userMessage", "content": [{"type": "text", "text": "b"}]}, "userMessage"),
            ("t", "null-turn", "i3", 0, None, json.dumps({"type": "userMessage"}), "userMessage", 0, None, None),
        ])
        self.assertEqual(await history._read_earliest_turn("t"), ("first", 100))

    async def test_earliest_turn_returns_none_when_missing(self) -> None:
        self.assertIsNone(await history._read_earliest_turn("missing"))
        self.thread_history_path.unlink()
        self.assertIsNone(await history._read_earliest_turn("missing"))

    async def test_turn_title_texts_filter_types_and_keep_order(self) -> None:
        _insert_thread_items(self.thread_history_path, [
            _thread_item("t", "turn", "r", 150, {"type": "reasoning", "summary": "think"}, "reasoning", 2),
            _thread_item("t", "turn", "u1", 100, {"type": "userMessage", "content": [{"type": "text", "text": "first"}]}, "userMessage", 0),
            _thread_item("t", "turn", "c", 160, {"type": "commandExecution", "command": "ls"}, "commandExecution", 3),
            _thread_item("t", "turn", "u2", 120, {"type": "userMessage", "content": [{"type": "text", "text": "second"}]}, "userMessage", 1),
            _thread_item("t", "turn", "a", 200, {"type": "agentMessage", "text": "answer"}, "agentMessage", 4),
        ])
        self.assertEqual(await history._read_turn_title_texts("t", "turn"), ["first", "second", "answer"])

    def test_sanitize_title_collapses_whitespace_and_truncates(self) -> None:
        self.assertEqual(history._sanitize_title("  hello\n   world  ", 60), "hello world")
        self.assertIsNone(history._sanitize_title("   ", 60))
        self.assertEqual(history._sanitize_title("abcdef", 3), "abc")


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.db_path = Path(directory) / "state.sqlite"
        self.thread_history_path = Path(directory) / "thread_history.sqlite"
        self.enterContext(patch.object(history, "DB_PATH", self.db_path))
        self.enterContext(patch.object(history, "THREAD_HISTORY_DB_PATH", self.thread_history_path))
        self.enterContext(patch.object(thread_manager, "DB_PATH", self.db_path))
        self.enterContext(patch.object(codex_execution, "prepare_workspace", AsyncMock()))
        self.enterContext(patch.object(session_codex, "_schedule_session_title"))
        self.enterContext(patch.dict(history.CONFIG, {"sessionTitleGen": {
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "baseUrl": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "instructions": "You are a helpful assistant.",
            "userPrompt": "Summarize in a short title.",
            "timeoutSeconds": 15,
            "maxInputChars": 4000,
            "titleMaxLength": 60,
        }}))
        self.factory = self.enterContext(patch.object(codex_runtime, "AsyncCodex"))
        self.client = self.factory.return_value.__aenter__.return_value
        self.metadata = NS(name=None, updated_at=100)

        async def stream():
            yield NS(method="turn/completed", payload=NS(turn=NS(
                status=TurnStatus.completed, duration_ms=1)))

        self.thread = NS(id="thread-1", read=AsyncMock(return_value=NS(thread=self.metadata)),
                         set_name=AsyncMock(),
                         turn=AsyncMock(return_value=NS(id="turn-1", stream=stream)))
        self.client.thread_start.return_value = self.thread
        self.client.thread_resume.return_value = self.thread
        self.client.thread_unarchive.return_value = self.thread
        _create_thread_history(self.thread_history_path, [])

    async def run_prompt(self, prompt="first"):
        return [event async for event in codex.Codex.running("user", prompt)]

    async def register(self, thread_id="thread-1", name=None, timestamp=None):
        async with history._connect() as db:
            await db.execute(
                "INSERT INTO session_history (user_id, thread_id, thread_name) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, thread_id) DO NOTHING",
                ("user", thread_id, name),
            )
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

    async def test_register_session_stores_empty_name_and_is_idempotent(self) -> None:
        await history.register_session("user", "thread-1")
        self.assertIsNone((await history.get_session("user", "thread-1")).thread_name)
        await history.register_session("user", "thread-1")
        self.assertIsNone((await history.get_session("user", "thread-1")).thread_name)

    async def test_idempotent_registration_and_same_second_sorting(self) -> None:
        await asyncio.gather(*(self.register(tid, "same", stamp) for tid, stamp in (
            ("a", 100), ("b", 200), ("c", 200))))
        async with history._connect() as db:
            await db.execute(
                "INSERT INTO session_history (user_id, thread_id, thread_name) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, thread_id) DO NOTHING", ("user", "a", "do not overwrite"))
            await db.execute(
                "INSERT INTO session_history (user_id, thread_id, thread_name) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, thread_id) DO NOTHING", ("other", "private", "secret"))
            await db.commit()
        records = await history.list_sessions("user")
        self.assertEqual([row.thread_id for row in records], ["c", "b", "a"])
        self.assertEqual(records[-1].thread_name, "same")
        self.assertIsNone(await history.get_session("other", "a"))

    async def test_sync_session_time_uses_earliest_turn_created_at_ms(self) -> None:
        await history.register_session("user", "thread-1")
        _insert_thread_items(self.thread_history_path, [
            _thread_item("thread-1", "later", "i1", 300, {"type": "userMessage", "content": [{"type": "text", "text": "a"}]}, "userMessage"),
            _thread_item("thread-1", "first", "i2", 100, {"type": "userMessage", "content": [{"type": "text", "text": "b"}]}, "userMessage"),
        ])
        record = await history.sync_session_time("user", "thread-1")
        self.assertEqual(record.updated_at, 100)

    async def test_sync_session_time_missing_record_returns_none(self) -> None:
        self.assertIsNone(await history.sync_session_time("user", "unknown"))

    async def test_time_does_not_regress(self) -> None:
        await self.register(timestamp=100)
        _replace_thread_items(self.thread_history_path, [
            _thread_item("thread-1", "first", "i1", 50, {"type": "userMessage", "content": [{"type": "text", "text": "b"}]}, "userMessage"),
        ])
        await history.sync_session_time("user", "thread-1")
        self.assertEqual((await history.get_session("user", "thread-1")).updated_at, 100)
        with self.assertRaises(ValueError):
            await history.update_session_time("user", "thread-1", None)

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
        await self.register(name="first")
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

    async def test_new_thread_registers_empty_name_and_reuse_updates_time(self) -> None:
        self.assertEqual(await self.run_prompt(), [{"type": "done"}])
        _replace_thread_items(self.thread_history_path, [
            _thread_item("thread-1", "first", "i1", 300, {"type": "userMessage", "content": [{"type": "text", "text": "a"}]}, "userMessage"),
        ])
        self.assertEqual(await self.run_prompt("second"), [{"type": "done"}])
        self.thread.set_name.assert_not_awaited()
        self.client.thread_start.assert_awaited_once()
        self.client.thread_resume.assert_awaited_once()
        records = await history.list_sessions("user")
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].thread_name)
        self.assertEqual(records[0].updated_at, 300)
        self.assertEqual(await thread_manager.get_user_thread("user"), "thread-1")

    async def test_attachment_only_prompt_still_registers_empty_name(self) -> None:
        await self.run_prompt([LocalImageInput(path="/tmp/image.png")])
        self.assertIsNone((await history.get_session("user", "thread-1")).thread_name)
        self.thread.set_name.assert_not_awaited()

    async def test_old_binding_is_not_backfilled_from_current_prompt(self) -> None:
        await thread_manager.set_user_thread("user", "thread-1")
        self.assertEqual(await self.run_prompt(), [{"type": "done"}])
        self.thread.set_name.assert_not_awaited()
        self.thread.read.assert_not_awaited()
        self.assertEqual(await history.list_sessions("user"), [])

    async def test_reset_keeps_history_and_next_thread_registers_empty(self) -> None:
        await self.run_prompt()
        await codex.Codex.reset_thread("user")
        self.client.thread_archive.assert_awaited_once_with(thread_id="thread-1")
        self.assertIsNone(await thread_manager.get_user_thread("user"))
        self.assertIsNotNone(await history.get_session("user", "thread-1"))
        self.thread.id = "thread-2"
        await self.run_prompt("new name")
        self.assertEqual(len(await history.list_sessions("user")), 2)
        self.assertIsNone((await history.get_session("user", "thread-2")).thread_name)

    async def test_restore_unarchives_then_replaces_binding_without_renaming(self) -> None:
        await self.register(name="first")
        await thread_manager.set_user_thread("user", "old")
        _replace_thread_items(self.thread_history_path, [
            _thread_item("thread-1", "first", "i1", 300, {"type": "userMessage", "content": [{"type": "text", "text": "a"}]}, "userMessage"),
        ])
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
        for target, name in ((self.client, "thread_unarchive"), (history, "sync_session_time")):
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
        with patch.object(history, "sync_session_time",
                          AsyncMock(side_effect=RuntimeError("offline"))), \
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
        _replace_thread_items(self.thread_history_path, [
            _thread_item("thread-1", "first", "i1", 100, {"type": "userMessage", "content": [{"type": "text", "text": "a"}]}, "userMessage"),
        ])
        await session_codex._sync_session_time("user", self.thread)
        self.assertEqual((await history.get_session("user", "thread-1")).updated_at, 100)
        self.thread.read.assert_not_awaited()

    async def test_steer_does_not_touch_history_name(self) -> None:
        await self.register(timestamp=100)
        self.metadata.status = NS(root=NS(type="active"))
        handle = NS(id="turn-1", steer=AsyncMock(return_value=NS(turn_id="turn-1")))
        route = codex_execution.CONFIG["codex"]["models"]["text"]
        live = codex.LiveTurn(self.thread, handle, route["model"], route["provider"], user_id="user")
        with patch.object(codex.Codex, "_live_turns", {"history-run": live}):
            self.assertEqual(await codex.Codex.steer("history-run", "first text"), {"type": "steered"})
        self.thread.set_name.assert_not_awaited()
        self.assertIsNone((await history.get_session("user", "thread-1")).thread_name)

    async def test_activity_update_moves_existing_record_to_top(self) -> None:
        await self.register("a", "first", 100)
        await self.register("b", "second", 200)
        await history.update_session_time("user", "a", 300)
        records = await history.list_sessions("user")
        self.assertEqual([row.thread_id for row in records], ["a", "b"])
        self.assertEqual(records[0].thread_name, "first")

    async def test_sync_timeout_does_not_hang_or_advance_time(self) -> None:
        await self.register(timestamp=100)
        async def stuck(user_id, thread_id):
            await asyncio.Event().wait()
        with patch.object(history, "sync_session_time", stuck), \
                patch.dict(codex_runtime.settings(), cleanupTimeoutSeconds=0.01), \
                patch.object(session_codex.logger, "exception") as log:
            await asyncio.wait_for(session_codex._sync_session_time("user", self.thread), 1)
        log.assert_called_once()
        self.assertEqual((await history.get_session("user", "thread-1")).updated_at, 100)

    async def test_failed_and_interrupted_turns_refresh_time(self) -> None:
        await self.register(timestamp=100)
        await thread_manager.set_user_thread("user", "thread-1")
        expected = 100
        for status in (TurnStatus.failed, TurnStatus.interrupted):
            expected += 1
            _replace_thread_items(self.thread_history_path, [
                _thread_item("thread-1", "first", "i1", expected, {"type": "userMessage", "content": [{"type": "text", "text": "a"}]}, "userMessage"),
            ])
            async def stream():
                yield NS(method="turn/completed", payload=NS(turn=NS(
                    status=status, error=None, duration_ms=1)))
            self.thread.turn.return_value.stream = stream
            await self.run_prompt()
            self.assertEqual((await history.get_session("user", "thread-1")).updated_at, expected)

    async def test_claim_session_title_writes_once(self) -> None:
        await self.register(name=None)
        self.assertTrue(await history.claim_session_title("user", "thread-1", " First title "))
        self.assertFalse(await history.claim_session_title("user", "thread-1", "Second title"))
        self.assertEqual((await history.get_session("user", "thread-1")).thread_name, "First title")

    async def test_ensure_session_title_generates_and_claims_once(self) -> None:
        await history.register_session("user", "thread-1")
        _replace_thread_items(self.thread_history_path, [
            _thread_item("thread-1", "first", "u", 100,
                         {"type": "userMessage", "content": [{"type": "text", "text": "What is a black hole?"}]},
                         "userMessage", 0),
            _thread_item("thread-1", "first", "r", 150, {"type": "reasoning", "summary": "secret reasoning"}, "reasoning", 1),
            _thread_item("thread-1", "first", "c", 160, {"type": "commandExecution", "command": "run secret command"}, "commandExecution", 2),
            _thread_item("thread-1", "first", "a", 200, {"type": "agentMessage", "text": "A black hole is a region of spacetime."}, "agentMessage", 3),
        ])
        client = NS(responses=NS(create=AsyncMock(return_value=NS(output=[
            NS(type="message", content=[NS(type="output_text", text=" Black holes explained ")]),
        ]))))
        @asynccontextmanager
        async def open_client(**kwargs):
            yield client
        with patch.object(history, "AsyncOpenAI", open_client), \
                patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}):
            self.assertTrue(await history.ensure_session_title("user", "thread-1"))
            self.assertFalse(await history.ensure_session_title("user", "thread-1"))
        client.responses.create.assert_awaited_once()
        user_input = client.responses.create.await_args.kwargs["input"]
        self.assertIn("What is a black hole?", user_input)
        self.assertIn("A black hole is a region of spacetime.", user_input)
        self.assertNotIn("secret reasoning", user_input)
        self.assertNotIn("run secret command", user_input)
        self.assertEqual((await history.get_session("user", "thread-1")).thread_name, "Black holes explained")

    async def test_ensure_session_title_skips_when_named_or_empty(self) -> None:
        await self.register(name="Existing")
        self.assertFalse(await history.ensure_session_title("user", "thread-1"))
        await self.register(thread_id="thread-2", name=None)
        _replace_thread_items(self.thread_history_path, [])
        self.assertFalse(await history.ensure_session_title("user", "thread-2"))

    async def test_ensure_session_title_missing_key_or_failure_keeps_null(self) -> None:
        await history.register_session("user", "thread-1")
        _replace_thread_items(self.thread_history_path, [
            _thread_item("thread-1", "first", "u", 100,
                         {"type": "userMessage", "content": [{"type": "text", "text": "hello"}]},
                         "userMessage"),
        ])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            self.assertFalse(await history.ensure_session_title("user", "thread-1"))
        self.assertIsNone((await history.get_session("user", "thread-1")).thread_name)


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

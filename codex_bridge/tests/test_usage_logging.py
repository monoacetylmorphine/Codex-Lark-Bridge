"""SQLite persistence uses temporary files, retains call data, and does not produce CSV."""
from __future__ import annotations
import pytest
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch
from codex_bridge.utils import token_usage as usage

class UsageLoggingTests:

    async def test_migrate_old_table_preserves_rows_and_adds_run_id(self) -> None:
        self.database.parent.mkdir()
        old_schema = usage.CREATE_TABLE_SQL.replace(",\n    runId TEXT NOT NULL DEFAULT ''", '')
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(old_schema)
            db.execute(f"INSERT INTO {usage.TABLE_NAME} (timeStamp, total_tokens) VALUES ('old', 99)")
            db.commit()
        await usage.save_log(dict(usage.DEFAULT_VALUES, runId='run-new', total_tokens=10))
        await usage.save_log(dict(usage.DEFAULT_VALUES, runId='run-new', total_tokens=25))
        with closing(sqlite3.connect(self.database)) as db:
            rows = db.execute(f'SELECT total_tokens, runId FROM {usage.TABLE_NAME} ORDER BY id').fetchall()
        assert rows == [(99, ''), (10, 'run-new'), (25, 'run-new')]

    def setup_method(self) -> None:
        self.directory = Path(self._pytest_stack.enter_context(tempfile.TemporaryDirectory()))
        self.database = self.directory / 'state' / 'usage.sqlite'
        self.csv_path = self.directory / 'exports' / 'usage.csv'
        self._pytest_stack.enter_context(patch.object(usage, 'DB_PATH', self.database))
        self._pytest_stack.enter_context(patch.object(usage, 'logger'))
        self._pytest_stack.enter_context(patch('builtins.print'))

    def test_ensure_keys_fills_defaults_without_mutating_input(self) -> None:
        original = {'userId': 'user', 'input_tokens': 42, 'custom': 'keep'}
        result = usage.ensure_keys(original)
        assert original == {'userId': 'user', 'input_tokens': 42, 'custom': 'keep'}
        assert result == dict(usage.DEFAULT_VALUES, **original)
        assert result is not original

    async def test_save_preserves_numeric_fields_and_unicode(self) -> None:
        record = dict(usage.DEFAULT_VALUES, timeStamp='2026-09-08', userId='user,"one"\nnext', threadId="thread'); DROP TABLE token_usage; --", model='model', input_tokens=11, output_tokens=7, total_tokens=18, taskDuration_ms=123)
        before = record.copy()
        await usage.save_log(record)
        assert record == before
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute(f'SELECT userId, threadId, input_tokens, output_tokens, total_tokens, taskDuration_ms FROM {usage.TABLE_NAME}').fetchone()
        assert row == (record['userId'], record['threadId'], 11, 7, 18, 123)

    async def test_save_partial_record_persists_defaults(self) -> None:
        await usage.save_log({'userId': 'user'})
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute(f'SELECT userId, threadId, total_tokens FROM {usage.TABLE_NAME}').fetchone()
        assert row == ('user', '', 0)

    async def test_database_failure_propagates_and_does_not_export(self) -> None:
        self.database.parent.mkdir()
        self.database.write_bytes(b'not a SQLite database')
        with pytest.raises(sqlite3.DatabaseError):
            await usage.save_log({'userId': 'user'})
        assert not self.csv_path.exists()

    async def test_finalize_updates_only_own_run_without_creating_csv(self) -> None:
        for run in ('one', 'two'):
            await usage.save_log(dict(usage.DEFAULT_VALUES, runId=run, total_tokens=8))
        await usage.finalize_usage('one', 123)
        await usage.finalize_usage('two', None)
        with closing(sqlite3.connect(self.database)) as db:
            rows = db.execute(f'SELECT runId, taskDuration_ms, total_tokens FROM {usage.TABLE_NAME} ORDER BY id').fetchall()
        assert rows == [('one', 123, 8), ('two', 0, 8)]
        assert list(self.directory.rglob('*.csv')) == []

    async def test_finalize_unknown_duration_does_not_create_database(self) -> None:
        await usage.finalize_usage('absent', None)
        assert not self.database.exists()

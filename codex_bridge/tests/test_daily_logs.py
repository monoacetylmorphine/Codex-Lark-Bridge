"""Daily rotation, retries and flush without wall-clock midnight waits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch

from codex_bridge.codex import thread_watchdog as watchdog


class DailyLogTests(unittest.IsolatedAsyncioTestCase):
    def enqueue_days(self, journal):
        # The same UTC date straddles midnight in UTC+8.
        moments = [datetime(2026, 9, 8, 15, 59, 59, tzinfo=timezone.utc),
                   datetime(2026, 9, 8, 16, 0, 0, tzinfo=timezone.utc)]
        with patch.object(watchdog, 'datetime') as clock:
            clock.now.side_effect = lambda tz: moments.pop(0).astimezone(tz)
            journal.record({'event': 'before'})
            journal.record({'event': 'after'})

    def journal(self, directory):
        with patch.dict(watchdog.CONFIG['runtime'], timezoneOffsetHours=8):
            journal = watchdog.RunJournal(directory)
        # Drive the single writer directly so both dates are pending together.
        journal._thread = Mock()
        return journal

    def read(self, root, day):
        return [json.loads(line) for line in (root / f'{day}_logs.jsonl').read_text().splitlines()]

    async def test_midnight_batch_uses_enqueue_dates_and_flush_waits(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'nested'
            journal = self.journal(root)
            self.enqueue_days(journal)
            flushing = asyncio.create_task(journal.flush())
            await asyncio.sleep(0)
            self.assertFalse(flushing.done())
            journal._write_pending()
            await flushing
            self.assertEqual(self.read(root, '2026-09-08'), [{'event': 'before'}])
            self.assertEqual(self.read(root, '2026-09-09'), [{'event': 'after'}])

    async def test_later_file_failure_does_not_replay_successful_day(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            journal = self.journal(root)
            self.enqueue_days(journal)
            real_open = open
            def fail_second(path, *args, **kwargs):
                if Path(path).name == '2026-09-09_logs.jsonl':
                    raise OSError('simulated disk failure')
                return real_open(path, *args, **kwargs)
            with patch('builtins.open', side_effect=fail_second):
                with self.assertRaises(OSError):
                    journal._write_pending()
            self.assertEqual(journal._saved, 1)
            self.assertEqual(len(journal._records), 1)
            # No new clock read on retry, even if it happens on a later date.
            with patch.object(watchdog, 'datetime') as clock:
                journal._write_pending()
                clock.now.assert_not_called()
            await journal.flush()
            self.assertEqual(self.read(root, '2026-09-08'), [{'event': 'before'}])
            self.assertEqual(self.read(root, '2026-09-09'), [{'event': 'after'}])

    async def test_partial_write_rolls_back_before_retry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            journal = self.journal(root)
            self.enqueue_days(journal)
            first = root / '2026-09-08_logs.jsonl'
            first.write_text('{"event": "existing"}\n')
            original = first.read_bytes()
            with first.open('ab') as file:
                def partial_write(payload):
                    file.write(payload[:5])
                    file.flush()
                    raise OSError('simulated partial write')
                wrapped = Mock(wraps=file)
                wrapped.write.side_effect = partial_write
                context = MagicMock()
                context.__enter__.return_value = wrapped
                with patch('builtins.open', return_value=context):
                    with self.assertRaises(OSError):
                        journal._write_pending()
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(journal._saved, 0)
            journal._write_pending()
            await journal.flush()
            self.assertEqual(self.read(root, '2026-09-08'),
                             [{'event': 'existing'}, {'event': 'before'}])
            self.assertEqual(self.read(root, '2026-09-09'), [{'event': 'after'}])

    async def test_failed_records_expire_but_fresh_records_are_written(self) -> None:
        with TemporaryDirectory() as directory:
            journal = self.journal(directory)
            with patch.object(watchdog.time, 'monotonic', return_value=100):
                journal.record({'event': 'expired'})
            with patch.object(watchdog.time, 'monotonic', return_value=100 + watchdog.RETENTION_SECONDS - 1):
                with patch('builtins.open', side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):
                        journal._write_pending()
            self.assertEqual(len(journal._records), 1)
            with patch.object(watchdog.time, 'monotonic', return_value=100 + watchdog.RETENTION_SECONDS):
                journal.record({'event': 'fresh'})
                with patch.object(watchdog.logger, 'error') as report:
                    journal._write_pending()
                    report.assert_called_once()
            self.assertFalse(journal._records)
            self.assertEqual(journal._saved, 1)
            self.assertEqual(journal._discarded, 1)
            records = [json.loads(line) for path in Path(directory).glob('*.jsonl')
                       for line in path.read_text().splitlines()]
            self.assertEqual(records, [{'event': 'fresh'}])
            with self.assertRaisesRegex(RuntimeError, 'not all records were written'):
                await journal.flush()

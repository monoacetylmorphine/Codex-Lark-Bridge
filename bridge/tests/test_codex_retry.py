"""Retries must preserve exceptions, cancellation and bounded backoff."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bridge.codex import codex_execution
from bridge.codex import codex_execution as codex


class CodexRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sleep = self.enterContext(patch.object(codex_execution.asyncio, 'sleep', new_callable=AsyncMock))
        self.retryable = self.enterContext(patch.object(codex_execution, 'is_retryable_error', return_value=True))
        self.enterContext(patch.object(codex_execution.logger, 'warning'))

    async def retry(self, operation, **options):
        defaults = dict(max_attempts=4, initial_delay_s=1, max_delay_s=2, jitter_ratio=0, backoff_multiplier=2)
        defaults.update(options)
        return await codex_execution._retry_on_overload_async(operation, operation_name='test', **defaults)

    async def test_immediate_success_does_not_sleep(self) -> None:
        operation = AsyncMock(return_value=object())
        self.assertIs(await self.retry(operation), operation.return_value)
        operation.assert_awaited_once_with()
        self.sleep.assert_not_awaited()

    async def test_transient_errors_retry_with_capped_exponential_delay(self) -> None:
        operation = AsyncMock(side_effect=[RuntimeError('busy')] * 3 + ['success'])
        self.assertEqual(await self.retry(operation), 'success')
        self.assertEqual(operation.await_count, 4)
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [1, 2, 2])

    async def test_exhaustion_raises_original_exception_without_final_sleep(self) -> None:
        failure = RuntimeError('still busy')
        operation = AsyncMock(side_effect=failure)
        with self.assertRaises(RuntimeError) as caught:
            await self.retry(operation, max_attempts=2)
        self.assertIs(caught.exception, failure)
        self.assertEqual(operation.await_count, 2)
        self.sleep.assert_awaited_once_with(1)

    async def test_nonretryable_error_is_never_replayed(self) -> None:
        self.retryable.return_value = False
        operation = AsyncMock(side_effect=ValueError('invalid'))
        with self.assertRaises(ValueError):
            await self.retry(operation)
        operation.assert_awaited_once_with()
        self.sleep.assert_not_awaited()

    async def test_cancellation_is_propagated_without_retry(self) -> None:
        operation = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.retry(operation)
        operation.assert_awaited_once_with()
        self.sleep.assert_not_awaited()

    async def test_cancelled_backoff_prevents_another_submission(self) -> None:
        operation = AsyncMock(side_effect=RuntimeError('busy'))
        self.sleep.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.retry(operation)
        operation.assert_awaited_once_with()

    async def test_invalid_attempt_budget_fails_before_operation(self) -> None:
        operation = AsyncMock()
        for attempts in (0, -1):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                await self.retry(operation, max_attempts=attempts)
        operation.assert_not_awaited()

    async def test_zero_delay_retries_without_sleep(self) -> None:
        operation = AsyncMock(side_effect=[RuntimeError('busy'), 'ok'])
        self.assertEqual(await self.retry(operation, initial_delay_s=0), 'ok')
        self.sleep.assert_not_awaited()

    async def test_jitter_uses_configured_range(self) -> None:
        operation = AsyncMock(side_effect=[RuntimeError('busy'), 'ok'])
        with patch.object(codex_execution.random, 'uniform', return_value=0.2) as random:
            self.assertEqual(await self.retry(operation, jitter_ratio=0.2), 'ok')
        random.assert_called_once_with(-0.2, 0.2)
        self.sleep.assert_awaited_once_with(1.2)

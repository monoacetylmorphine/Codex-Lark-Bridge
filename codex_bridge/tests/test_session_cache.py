"""Verify lifecycle boundaries without waiting for an actual 24-hour period."""
from __future__ import annotations
import pytest
import asyncio
from codex_bridge.configs.loader import CONFIG
import time
from unittest.mock import AsyncMock, patch
from codex_bridge.session.session_gateway import ActiveCodexRun, SessionCache, RETENTION_SECONDS
import test_stop_command as helpers

class SessionCacheTests:

    def test_idle_cleanup_preserves_new_waiter_and_deduplication(self) -> None:
        cache = SessionCache()
        lock = cache.codex_locks['chat'] = asyncio.Lock()
        cache.chat_generations['chat'] = 7
        cache.remember(cache.received_message_ids, 'chat', 'old')
        with cache.hold('chat'):
            cache.release_idle('chat')
            assert cache.codex_locks['chat'] is lock
            assert cache.chat_generations['chat'] == 7
        assert not cache.codex_locks
        assert not cache.chat_generations
        assert 'old' in cache.received_message_ids['chat']

    def test_old_owner_cannot_release_steered_message_or_new_run(self) -> None:
        cache = SessionCache()
        old = ActiveCodexRun('old', 'chat', frozenset({'m'}))
        new = ActiveCodexRun('new', 'chat', frozenset({'m'}))
        cache.active_runs_by_chat['chat'] = new
        cache.active_runs_by_message_id['m'] = new
        cache.received_at['m'] = 123
        cache.expire_run(old)
        assert cache.active_runs_by_chat['chat'] is new
        assert cache.active_runs_by_message_id['m'] is new
        assert cache.received_at['m'] == 123
        cache.expire_run(new)
        assert not cache.received_at

    def test_duplicate_does_not_extend_retention(self) -> None:
        cache = SessionCache()
        with patch('codex_bridge.session.session_gateway.time.monotonic', return_value=100):
            cache.remember(cache.received_message_ids, 'chat', 'm')
            cache.remember(cache.processed_message_ids, 'chat', 'm')
            cache.recall('m')
            cache.track_reaction('chat', 'm')
        cache.reaction_message_ids['chat'] = {'m': 'r'}
        with patch('codex_bridge.session.session_gateway.time.monotonic', return_value=200):
            cache.remember(cache.received_message_ids, 'chat', 'm')
        cache.prune(100 + RETENTION_SECONDS - 1)
        assert 'm' in cache.received_message_ids['chat']
        cache.prune(100 + RETENTION_SECONDS)
        assert not cache.received_message_ids
        assert not cache.processed_message_ids
        assert not cache.recalled_message_ids
        assert not cache.reaction_message_ids

    async def test_expired_buffer_is_cancelled_without_new_events(self) -> None:
        cache = SessionCache()
        cache.buffered_events['chat-1'] = helpers.event('image')
        task = cache.buffer_tasks['chat-1'] = asyncio.create_task(asyncio.sleep(100))
        cache._buffer_times['chat-1'] = 100
        cache.prune(100 + RETENTION_SECONDS)
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert not cache.buffered_events
        assert not cache.buffer_tasks

class GatewayCacheTests:

    def setup_method(self) -> None:
        helpers.StopTests.setup_method(self)

    async def test_finished_request_releases_runtime_but_rejects_duplicate(self) -> None:
        await self.router.processing(helpers.event('hello', message_id='next'))
        count = self.execution.assemble_input.await_count
        assert count == 1
        for name in ('all_runs', 'codex_locks', 'chat_generations', 'received_at', 'reaction_message_ids', 'active_runs_by_chat', 'active_runs_by_message_id'):
            assert not getattr(self.runtime.cache, name), name
        await self.router.processing(helpers.event('hello', message_id='next'))
        assert self.execution.assemble_input.await_count == count

    async def test_history_failure_and_card_failure_release_request(self) -> None:
        self.router.fetch_history.side_effect = OSError('offline')
        self.runtime.send_card.side_effect = OSError('offline')
        await self.router.processing(helpers.event('hello'))
        for name in ('all_runs', 'codex_locks', 'received_at', 'pending_chat_requests'):
            assert not getattr(self.runtime.cache, name), name

    async def test_unconfirmed_stop_expires_even_when_close_fails(self) -> None:
        state = helpers.StopTests.state(self)
        state.created_at = time.monotonic() - RETENTION_SECONDS
        self.runtime.cache.blocked_chats[state.chat_id] = state
        self.runtime.codex.discard_expired_run = AsyncMock(side_effect=OSError('unconfirmed'))
        await self.runtime._expire_session_cache()
        assert state.expired
        self.runtime.codex.discard_expired_run.assert_awaited_once_with(state.run_id)
        assert not self.runtime.cache.blocked_chats
        assert not self.runtime.cache.active_runs_by_message_id

    async def test_history_page_size_uses_shared_setting(self) -> None:
        with patch.dict(CONFIG['messaging'], historyPageSize=3):
            await self.router.processing(helpers.event('hello'))
        self.router.fetch_history.assert_awaited_once_with(chat_id='chat-1', messages_num=3)

class ReactionLifecycleTests:

    def setup_method(self) -> None:
        helpers.StopTests.setup_method(self)

    async def test_all_reactions_wait_for_card_completion_even_on_failure(self) -> None:
        for failure in (False, True):
            self.runtime.cache.received_message_ids.clear()
            self.runtime.cache.processed_message_ids.clear()
            self.runtime.delete_reaction.reset_mock()
            entered, finish = (asyncio.Event(), asyncio.Event())

            async def send(*args, **kwargs):
                content = kwargs.get('content', args[1] if len(args) > 1 else None)
                if not isinstance(content, str):
                    async for _ in content:
                        pass
                    entered.set()
                    await finish.wait()
                    if failure:
                        raise self.card_module.CardDeliveryError('delivery failed')
            self.runtime.send_card = send
            self.runtime.cache.reaction_message_ids['chat-1'] = {'m1': 'r1', 'm2': 'r2'}
            batch = helpers.batch_from_chat_history(helpers.event('hello', message_id='m2'), [helpers.history('hello', 'm2'), helpers.history('hello', 'm1')])
            task = asyncio.create_task(self.execution._handle_message_batch(batch, 0))
            await asyncio.wait_for(entered.wait(), 1)
            self.runtime.delete_reaction.assert_not_awaited()
            finish.set()
            await asyncio.wait_for(task, 1)
            assert self.runtime.delete_reaction.await_count == 2
            assert not self.runtime.cache.reaction_message_ids
            assert not self.runtime.cache.pending_reactions

    async def test_failed_delete_survives_release_and_background_retries_without_input(self) -> None:
        self.runtime.delete_reaction.return_value = False
        await self.router.processing(helpers.event('hello', message_id='m1'))
        assert not self.runtime.cache.all_runs
        assert not self.runtime.cache.codex_locks
        key = ('chat-1', 'm1', 'reaction-1')
        pending = self.runtime.cache.pending_reactions[key]
        assert 'm1' in self.runtime.cache.reaction_message_ids['chat-1']
        attempts = self.runtime.delete_reaction.await_count
        await self.runtime._retry_reactions()
        assert self.runtime.delete_reaction.await_count == attempts
        pending.due = 0
        self.runtime.delete_reaction.return_value = True
        await self.runtime._retry_reactions()
        assert not self.runtime.cache.pending_reactions
        assert not self.runtime.cache.reaction_message_ids

    async def test_cancel_during_delete_keeps_all_snapshot_records(self) -> None:
        entered = asyncio.Event()

        async def stuck(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.runtime.delete_reaction.side_effect = stuck
        self.runtime.cache.reaction_message_ids['chat-1'] = {'m1': 'r1', 'm2': 'r2'}
        task = asyncio.create_task(self.runtime._clear_reaction('chat-1'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(self.runtime.cache.pending_reactions) == 2
        assert not self.runtime.cache.reactions_being_cleared

    async def test_retry_old_id_cannot_remove_new_reaction(self) -> None:
        self.runtime.cache.queue_reaction('chat-1', 'm1', 'old')
        self.runtime.cache.reaction_message_ids['chat-1'] = {'m1': 'new'}
        await self.runtime._retry_reactions()
        assert self.runtime.cache.reaction_message_ids['chat-1']['m1'] == 'new'
        assert not self.runtime.cache.pending_reactions

    async def test_running_owner_is_not_cleared_by_stop_snapshot(self) -> None:
        worker = asyncio.create_task(asyncio.Event().wait())
        state = ActiveCodexRun('run', 'chat-1', frozenset({'m1'}), task=worker)
        self.runtime.cache.active_runs_by_message_id['m1'] = state
        self.runtime.cache.reaction_message_ids['chat-1'] = {'m1': 'r1'}
        try:
            await self.runtime._clear_reaction('chat-1')
            self.runtime.delete_reaction.assert_not_awaited()
            assert not self.runtime.cache.pending_reactions
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def test_pending_deletion_expiry_and_capacity_are_bounded(self, caplog) -> None:
        from codex_bridge.configs.loader import CONFIG
        cache = self.runtime.cache
        with patch.dict(CONFIG['messaging'], recallCacheMaxEntries=1):
            cache.reaction_message_ids['chat'] = {'m1': 'r1', 'm2': 'r2'}
            cache.queue_reaction('chat', 'm1', 'r1')
            with caplog.at_level('ERROR', logger='codex_bridge'):
                cache.queue_reaction('chat', 'm2', 'r2')
            assert len(cache.pending_reactions) == 1
            key, pending = next(iter(cache.pending_reactions.items()))
            with caplog.at_level('ERROR', logger='codex_bridge'):
                cache.prune(pending.created_at + RETENTION_SECONDS + 1)
            assert not cache.pending_reactions
            assert not cache.reaction_message_ids

    async def test_stop_and_recall_wait_for_output_card_cleanup(self) -> None:
        from types import SimpleNamespace as NS
        for action in ('stop', 'recall'):
            self.runtime.cache.received_message_ids.clear()
            self.runtime.cache.processed_message_ids.clear()
            self.runtime.delete_reaction.reset_mock()
            entered, closing, release = (asyncio.Event(), asyncio.Event(), asyncio.Event())

            async def send(*args, **kwargs):
                content = kwargs.get('content', args[1] if len(args) > 1 else None)
                if isinstance(content, str):
                    return
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closing.set()
                    await release.wait()
            self.runtime.send_card = send
            self.runtime.cache.reaction_message_ids['chat-1'] = {'m1': 'r1'}
            data = helpers.event('hello', message_id='m1')
            batch = helpers.batch_from_chat_history(data, [])
            task = asyncio.create_task(self.execution._handle_message_batch(batch, 0))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                if action == 'stop':
                    await self.commands._stop_chat(helpers.event())
                else:
                    await self.commands.processing_recall(NS(event=NS(recall_type='message_owner', message_id='m1', chat_id='chat-1')))
                await asyncio.wait_for(closing.wait(), 1)
                self.runtime.delete_reaction.assert_not_awaited()
                release.set()
                await asyncio.wait_for(task, 1)
                self.runtime.delete_reaction.assert_awaited_once_with(message_id='m1', reaction_id='r1')
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

from __future__ import annotations
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from codex_bridge.configs.loader import CONFIG
from codex_bridge.middleware.message_collector import is_new_command, batch_from_chat_history
import test_stop_command as helpers

class NewCommandTests:

    def setup_method(self) -> None:
        helpers.StopTests.setup_method(self)

    def test_only_original_text_matches(self) -> None:
        for value in ('/new', ' /NEW\n'):
            assert is_new_command('text', json.dumps({'text': value}))
        for kind, value in [('post', '/new'), ('audio', '/new'), ('text', '/new now'), ('text', 'say /new')]:
            assert not is_new_command(kind, json.dumps({'text': value}))

    async def test_entry_bypasses_assembly_and_duplicate_event(self) -> None:
        message = helpers.event('/new')
        await self.router.processing(message)
        await self.router.processing(message)
        self.runtime.codex.reset_thread.assert_awaited_once_with('user-1')
        self.execution.assemble_input.assert_not_awaited()
        self.router.fetch_history.assert_not_awaited()

    async def test_stop_unconfirmed_does_not_reset(self) -> None:
        state = helpers.StopTests.state(self)
        self.runtime.codex.interrupt_and_confirm.return_value = False
        await self.router.processing(helpers.event('/new'))
        self.runtime.codex.reset_thread.assert_not_awaited()
        assert state.interrupted
        assert self.runtime.send_card.call_args.kwargs['content'] == CONFIG['messages']['stopFailed']

    async def test_reset_failure_is_not_success(self) -> None:
        self.runtime.codex.reset_thread.side_effect = RuntimeError('archive failed')
        await self.router.processing(helpers.event('/new'))
        assert self.runtime.send_card.call_args.kwargs['content'] == CONFIG['messages']['newThreadFailed']
        assert not self.runtime.cache.reset_tasks

    async def test_new_input_waits_for_reset(self) -> None:
        entered, release = (asyncio.Event(), asyncio.Event())

        async def reset(_):
            entered.set()
            await release.wait()
        self.runtime.codex.reset_thread.side_effect = reset
        command = asyncio.create_task(self.router.processing(helpers.event('/new')))
        await asyncio.wait_for(entered.wait(), 1)
        self.router._process_chat_history = AsyncMock()
        message = asyncio.create_task(self.router.processing(helpers.event('hello', message_id='next')))
        await asyncio.sleep(0)
        self.router._process_chat_history.assert_not_awaited()
        release.set()
        await asyncio.wait_for(asyncio.gather(command, message), 1)
        assert self.router._process_chat_history.call_args.args[1] == 1
        assert 'chat-1' not in self.runtime.cache.chat_generations

    async def test_stop_invalidates_input_waiting_for_reset(self) -> None:
        entered, release = (asyncio.Event(), asyncio.Event())

        async def reset(_):
            entered.set()
            await release.wait()
        self.runtime.codex.reset_thread.side_effect = reset
        command = asyncio.create_task(self.router.processing(helpers.event('/new')))
        await asyncio.wait_for(entered.wait(), 1)
        self.router._process_chat_history = AsyncMock()
        message = asyncio.create_task(self.router.processing(helpers.event('hello', message_id='next')))
        await asyncio.sleep(0)
        await self.router.processing(helpers.event('/stop', message_id='halt'))
        release.set()
        await asyncio.wait_for(asyncio.gather(command, message), 1)
        self.router._process_chat_history.assert_not_awaited()

    async def test_direct_types_and_unknown_do_not_flush_buffer(self) -> None:
        self.router._cancel_buffer = AsyncMock()
        self.router._process_chat_history = AsyncMock()
        self.router._buffer_message = AsyncMock()
        msg = helpers.event('hello')
        msg.event.message.message_type = 'video'
        await self.router._route_message(msg, 0)
        self.router._cancel_buffer.assert_not_awaited()
        self.router._process_chat_history.assert_not_awaited()
        msg.event.message.message_type = 'image'
        await self.router._route_message(msg, 0)
        self.router._buffer_message.assert_awaited_once()
        msg.event.message.message_type = 'text'
        await self.router._route_message(msg, 0)
        self.router._process_chat_history.assert_awaited_once()

    def test_unknown_history_is_skipped(self) -> None:
        bad = helpers.history('bad', 'bad')
        bad['msg_type'] = 'video'
        batch = batch_from_chat_history(helpers.event('hello', message_id='next'), [helpers.history('hello', 'next'), bad, helpers.history('old', 'old')])
        assert [m.message_id for m in batch.messages] == ['old', 'next']

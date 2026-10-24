"""Verify /stop, recall, and startup races with mocked Lark and Codex, without reading credentials."""
from __future__ import annotations
import pytest
from codex_bridge.session.session_gateway import ActiveCodexRun
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from codex_bridge.codex import codex_execution, codex_runtime, thread_manager
from codex_bridge.session import session_codex, session_history
from codex_bridge.codex.codex_execution import Codex
from codex_bridge.codex import codex_execution as codex
from codex_bridge.configs.loader import CONFIG
from codex_bridge.middleware import message_router
from codex_bridge.middleware.message_collector import batch_from_chat_history, is_stop_command

def event(text='/stop', chat='chat-1', message_id='stop-1', chat_type='p2p'):
    return NS(event=NS(message=NS(chat_id=chat, chat_type=chat_type, message_id=message_id, message_type='text', content=json.dumps({'text': text}), mentions=[]), sender=NS(sender_id=NS(union_id='user-1'))))

def history(text, message_id):
    return {'message_id': message_id, 'msg_type': 'text', 'sender': {'sender_type': 'user'}, 'body': {'content': json.dumps({'text': text})}}

class TestCommand:

    def test_exact_command_and_configuration(self) -> None:
        for text in ('/stop', '  /STOP\n'):
            assert is_stop_command('text', json.dumps({'text': text}))
        for text in ('Please /stop', '/stop now', '/stopping', '@_user_1 /stop'):
            assert not is_stop_command('text', json.dumps({'text': text}))
        for raw in ('invalid', '[]', 'null', '{"text": 1}'):
            assert not is_stop_command('text', raw)
        assert not is_stop_command('post', '{"text": "/stop"}')
        with patch.dict(CONFIG['messaging'], stopThreadCommand='/halt'):
            assert is_stop_command('text', '{"text": "/HALT"}')
            assert not is_stop_command('text', '{"text": "/stop"}')

    def test_configured_stop_command_excludes_event_and_bounds_history(self) -> None:
        with patch.dict(CONFIG['messaging'], stopThreadCommand='/halt'):
            assert not batch_from_chat_history(event(' /HALT '), []).messages
            items = [history('hello', 'current'), history(' /HALT ', 'halt'), history('old', 'old')]
            result = batch_from_chat_history(event('hello', message_id='current'), items)
            assert [message.message_id for message in result.messages] == ['current']
            result = batch_from_chat_history(event('/stop'), [])
            assert [message.content for message in result.messages] == [{'text': '/stop'}]

    def test_stop_is_excluded_and_bounds_history(self) -> None:
        items = [history('hello', 'new'), history(' /STOP ', 'stop'), history('old', 'old')]
        batch = batch_from_chat_history(event('hello', message_id='new'), items)
        assert [m.message_id for m in batch.messages] == ['new']
        assert not batch_from_chat_history(event(), []).messages

    def test_new_command_still_stands_alone_and_bounds_history(self) -> None:
        batch = batch_from_chat_history(event(' /NEW ', message_id='new'), [history('old', 'old')])
        assert not batch.messages
        batch = batch_from_chat_history(event('hello', message_id='next'), [history('/new', 'new'), history('old', 'old')])
        assert [m.message_id for m in batch.messages] == ['next']

class StopTests:

    def setup_method(self) -> None:

        def module(name, **values):
            result = ModuleType(name)
            result.__dict__.update(values)
            return result
        with patch.dict(sys.modules, {'codex_bridge.services.lark.lark_client': module('codex_bridge.services.lark.lark_client', client=NS())}):
            from codex_bridge.services.lark.lark_message_card import CardDeliveryError, CardReplace, CardSteer, CardStreamSession, CardStreamStopped
            self.card_module = sys.modules['codex_bridge.services.lark.lark_message_card']
            self.card_control_type = CardSteer
        stubs = {'codex_bridge.services.lark.lark_client': module('codex_bridge.services.lark.lark_client', create_websocket_client=None), 'codex_bridge.services.lark.lark_tools': module('codex_bridge.services.lark.lark_tools', getting_chat_history=AsyncMock(return_value=[]), adding_reaction_emoji=AsyncMock(return_value='reaction-1'), delete_reaction_emoji=AsyncMock(return_value=True)), 'codex_bridge.services.lark.lark_message_card': module('codex_bridge.services.lark.lark_message_card', CardReplace=CardReplace, CardStreamSession=CardStreamSession, CardSteer=CardSteer, CardDeliveryError=CardDeliveryError, CardStreamStopped=CardStreamStopped, sending_card=AsyncMock()), 'codex_bridge.middleware.message_assemble': module('codex_bridge.middleware.message_assemble', InputAssemblyError=type('InputAssemblyError', (Exception,), {}), assemble_codex_input=AsyncMock(return_value=NS(codex_input='hello', notices=())))}
        spec = importlib.util.spec_from_file_location('main_stop_tests', Path(__file__).resolve().parents[1] / 'main.py')
        self.g = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {**stubs, spec.name: self.g}):
            spec.loader.exec_module(self.g)
            self.runtime, self.execution, self.commands, self.router = self.g.create_gateway()

        async def running(**kwargs):
            yield {'type': 'started', 'thread_id': 'thread-1', 'turn_id': 'turn-1'}
            yield {'type': 'done'}
        self.runtime.codex = NS(interrupt_and_confirm=AsyncMock(return_value=True), force_close=AsyncMock(return_value=True), completed_status=AsyncMock(return_value=None), forget_run=AsyncMock(), reset_thread=AsyncMock(), running=running, steer=AsyncMock(return_value={'type': 'idle'}))

    def state(self, chat='chat-1', started=True):
        state = ActiveCodexRun('run-' + chat, chat, frozenset({'m1', 'm2'}), codex_started=started)
        for mid in state.message_ids:
            self.runtime.cache.active_runs_by_message_id[chat + mid] = state
        return state

    async def test_stop_bypasses_lock_and_deduplicates_batch(self) -> None:
        state = self.state()
        other = self.state('chat-2')
        lock = await self.runtime._get_codex_lock('chat-1')
        async with lock:
            await asyncio.wait_for(self.router.processing(event(' /STOP ')), 1)
        assert state.interrupted
        assert not other.interrupted
        self.runtime.codex.interrupt_and_confirm.assert_awaited_once_with(state.run_id)
        self.router.fetch_history.assert_not_awaited()
        self.router.add_reaction.assert_not_awaited()
        assert self.runtime.send_card.call_args.kwargs['content'] == CONFIG['messages']['stopRequested']

    async def test_stop_before_turn_start(self) -> None:
        state = self.state(started=False)
        await self.router.processing(event())
        assert not await self.runtime._start_codex_run(state)
        self.runtime.codex.interrupt_and_confirm.assert_not_awaited()

    async def test_stop_closes_reply_stream(self) -> None:
        state = self.state()
        closed = []

        async def events(**kwargs):
            try:
                yield {'type': 'answer', 'content': 'Displayed'}
                yield {'type': 'answer', 'content': 'Content after stop'}
            finally:
                closed.append(True)
        self.runtime.codex.running = events
        stream = self.execution._reply_content(NS(union_id='user-1'), 'hello', state)
        await anext(stream)
        await self.router.processing(event())
        with pytest.raises(self.card_module.CardStreamStopped):
            await anext(stream)
        assert closed == [True]

    async def test_stop_during_reaction_does_not_submit_old_message(self) -> None:
        entered, resume = (asyncio.Event(), asyncio.Event())

        async def reaction(**kwargs):
            entered.set()
            await resume.wait()
            return 'late-reaction'
        self.router.add_reaction.side_effect = reaction
        task = asyncio.create_task(self.router.processing(event('hello', message_id='old')))
        await entered.wait()
        await self.router.processing(event())
        resume.set()
        await task
        self.router.fetch_history.assert_not_awaited()
        self.runtime.delete_reaction.assert_awaited_once_with(message_id='old', reaction_id='late-reaction')

    async def test_repeat_stop_and_idle(self) -> None:
        await self.router.processing(event())
        assert self.runtime.send_card.call_args.kwargs['content'] == CONFIG['messages']['stopIdle']
        state = self.state()
        await self.router.processing(event(message_id='stop-2'))
        await self.router.processing(event(message_id='stop-3'))
        assert state.interrupted
        assert self.runtime.codex.interrupt_and_confirm.await_count == 1

    async def test_interrupt_failure_keeps_output_closed(self) -> None:
        state = self.state()
        self.runtime.codex.interrupt_and_confirm.side_effect = RuntimeError('offline')
        await self.router.processing(event())
        assert await self.runtime._run_was_interrupted(state)
        assert self.runtime.send_card.call_args.kwargs['content'] == CONFIG['messages']['stopFailed']

    async def test_recall_uses_same_interrupt_and_owner_filter(self) -> None:
        state = self.state()
        self.runtime.cache.active_runs_by_message_id['m1'] = state
        recalled = NS(event=NS(message_id='m1', chat_id='chat-1', recall_type='admin'))
        await self.commands.processing_recall(recalled)
        assert not state.interrupted
        recalled.event.recall_type = 'message_owner'
        await self.commands.processing_recall(recalled)
        assert state.interrupted
        self.runtime.codex.interrupt_and_confirm.assert_awaited_once_with(state.run_id)

    async def test_buffer_cancel_and_next_message_works(self) -> None:
        data = event('image')
        data.event.message.message_type = 'image'
        await self.router._buffer_message(data, 0)
        task = self.runtime.cache.buffer_tasks['chat-1']
        await self.router.processing(event())
        await asyncio.gather(task, return_exceptions=True)
        assert 'chat-1' not in self.runtime.cache.buffer_tasks
        assert 'chat-1' not in self.runtime.cache.buffered_events
        self.router.submit = AsyncMock()
        await self.router.processing(event('hello', message_id='next'))
        self.router.submit.assert_awaited_once()
        assert self.router.submit.call_args.args[1] == 0
        assert 'chat-1' not in self.runtime.cache.chat_generations

    async def test_stop_during_history_fetch_discards_old_request(self) -> None:
        fetching, resume = (asyncio.Event(), asyncio.Event())

        async def fetch(**kwargs):
            fetching.set()
            await resume.wait()
            return []
        self.router.fetch_history.side_effect = fetch
        task = asyncio.create_task(self.router.processing(event('hello', message_id='old')))
        await fetching.wait()
        await self.router.processing(event())
        resume.set()
        await task
        self.execution.assemble_input.assert_not_awaited()

    async def test_stop_discards_request_waiting_for_lock(self) -> None:
        batch = batch_from_chat_history(event('hello', message_id='old'), [])
        lock = await self.runtime._get_codex_lock('chat-1')
        async with lock:
            task = asyncio.create_task(self.execution._handle_message_batch(batch, 0))
            await asyncio.sleep(0)
            await self.router.processing(event())
        await task
        self.execution.assemble_input.assert_not_awaited()

    async def test_group_requires_mention_and_replies_to_chat(self) -> None:
        data = event(chat_type='group')
        with patch('codex_bridge.middleware.message_router._is_bot_mentioned', return_value=False):
            await self.router.processing(data)
        self.runtime.send_card.assert_not_awaited()
        with patch('codex_bridge.middleware.message_router._is_bot_mentioned', return_value=True):
            await self.router.processing(data)
        assert self.runtime.send_card.call_args.kwargs['union_id'] == 'chat-1'

    async def test_multimodal_batch_clears_every_reaction(self) -> None:
        self.router.buffer_seconds = lambda: 0
        for text_finishes in (False, True):
            self.runtime.cache.received_message_ids.clear()
            self.runtime.cache.processed_message_ids.clear()
            self.router.add_reaction.side_effect = ['r1', 'r2', 'r3']
            self.runtime.delete_reaction.reset_mock()
            items = []
            for index in range(1, 4):
                mid = f'm{index}'
                data = event('describe', message_id=mid)
                kind = 'text' if text_finishes and index == 3 else 'image'
                data.event.message.message_type = kind
                item = history('describe', mid)
                item['msg_type'] = kind
                items.insert(0, item)
                self.router.fetch_history.return_value = list(items)
                await self.router.processing(data)
            if not text_finishes:
                await self.runtime.cache.buffer_tasks['chat-1']
            assert {tuple(call.kwargs.values()) for call in self.runtime.delete_reaction.await_args_list} == {('m1', 'r1'), ('m2', 'r2'), ('m3', 'r3')}
            assert self.runtime.delete_reaction.await_count == 3
            assert not self.runtime.cache.reaction_message_ids

    async def test_batch_cleanup_preserves_later_message_and_other_chat(self) -> None:
        self.runtime.cache.reaction_message_ids = {'chat-1': {'m1': 'r1', 'm2': 'r2', 'next': 'r3'}, 'chat-2': {'other': 'r4'}}
        batch = batch_from_chat_history(event('hello', message_id='m2'), [history('hello', 'm2'), history('hello', 'm1')])
        await self.execution._handle_message_batch(batch, 0)
        assert self.runtime.delete_reaction.await_count == 2
        assert self.runtime.cache.reaction_message_ids == {'chat-1': {'next': 'r3'}, 'chat-2': {'other': 'r4'}}

    async def test_stop_clears_snapshot_and_preserves_new_reaction(self) -> None:
        self.state()
        self.runtime.cache.reaction_message_ids = {'chat-1': {'m1': 'r1', 'm2': 'r2'}}

        async def interrupt(run_id):
            self.runtime.cache.reaction_message_ids['chat-1']['next'] = 'r3'
            return True
        self.runtime.codex.interrupt_and_confirm.side_effect = interrupt
        await self.router.processing(event())
        assert self.runtime.delete_reaction.await_count == 2
        assert self.runtime.cache.reaction_message_ids == {'chat-1': {'next': 'r3'}}

    async def test_recall_then_batch_cleanup_is_idempotent(self) -> None:
        state = self.state()
        self.runtime.cache.active_runs_by_message_id['m1'] = state
        self.runtime.cache.reaction_message_ids = {'chat-1': {'m1': 'r1', 'm2': 'r2'}}
        await self.commands.processing_recall(NS(event=NS(message_id='m1', chat_id='chat-1', recall_type='message_owner')))
        assert self.runtime.cache.reaction_message_ids == {'chat-1': {'m2': 'r2'}}
        await self.runtime._clear_reaction('chat-1', state.message_ids)
        await self.runtime._clear_reaction('chat-1', state.message_ids)
        assert self.runtime.delete_reaction.await_count == 2
        assert not self.runtime.cache.reaction_message_ids

    async def test_failed_delete_retains_record_and_continues(self) -> None:
        self.runtime.cache.reaction_message_ids = {'chat-1': {'m1': 'r1', 'm2': 'r2', 'm3': 'r3'}}
        self.runtime.delete_reaction.side_effect = [False, RuntimeError('offline'), True]
        await self.runtime._clear_reaction('chat-1')
        assert self.runtime.delete_reaction.await_count == 3
        assert self.runtime.cache.reaction_message_ids == {'chat-1': {'m1': 'r1', 'm2': 'r2'}}
        assert not self.runtime.cache.reactions_being_cleared
        self.runtime.delete_reaction.side_effect = None
        await self.runtime._clear_reaction('chat-1')
        assert not self.runtime.cache.reaction_message_ids

    async def test_concurrent_cleanup_does_not_delete_twice(self) -> None:
        self.runtime.cache.reaction_message_ids = {'chat-1': {'m1': 'r1', 'm2': 'r2'}}
        entered, resume = (asyncio.Event(), asyncio.Event())

        async def delete(message_id, reaction_id):
            if message_id == 'm1':
                entered.set()
                await resume.wait()
            return True
        self.runtime.delete_reaction.side_effect = delete
        task = asyncio.create_task(self.runtime._clear_reaction('chat-1'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.runtime._clear_reaction('chat-1')
        finally:
            resume.set()
            await task
        assert self.runtime.delete_reaction.await_count == 2
        assert not self.runtime.cache.reaction_message_ids
        assert not self.runtime.cache.reactions_being_cleared

    async def test_failed_add_does_not_store_invalid_reaction(self) -> None:
        self.router.add_reaction.return_value = None
        await self.router.processing(event('hello', message_id='m1'))
        self.execution.assemble_input.assert_awaited_once()
        self.runtime.delete_reaction.assert_not_awaited()
        assert not self.runtime.cache.reaction_message_ids

    async def test_batch_failure_still_clears_all_reactions(self) -> None:
        self.runtime.cache.reaction_message_ids = {'chat-1': {'m1': 'r1', 'm2': 'r2'}}
        self.execution.assemble_input.side_effect = RuntimeError('assembly failed')
        batch = batch_from_chat_history(event('hello', message_id='m2'), [history('hello', 'm2'), history('hello', 'm1')])
        await self.execution._handle_message_batch(batch, 0)
        assert self.runtime.delete_reaction.await_count == 2
        assert not self.runtime.cache.reaction_message_ids

    async def test_failed_ogg_transcription_deletes_source_and_sends_lark_notice(self) -> None:
        from codex_bridge.middleware import audio_transcription as audio
        from codex_bridge.middleware.message_collector import CollectedMessage, MessageBatch
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'upload.ogg'
            source.write_bytes(b'audio')
            name = 'assembly_failed_ogg_notification_test'
            spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'middleware/message_assemble.py')
            assembly = importlib.util.module_from_spec(spec)
            stub = ModuleType('codex_bridge.services.lark.lark_tools')
            stub.download_msg_resource = AsyncMock(return_value=str(source))
            with patch.dict(sys.modules, {name: assembly, stub.__name__: stub}):
                spec.loader.exec_module(assembly)
            self.execution.assemble_input = assembly.assemble_codex_input
            self.runtime.codex.running = AsyncMock()
            batch = MessageBatch('chat-1', 'user-1', 'p2p', (CollectedMessage('m1', 'file', {'file_key': 'voice', 'file_name': 'upload.ogg'}, 1),))
            with patch.dict(audio.os.environ, {audio.AUDIO_CONFIG['asr']['apiKeyEnv']: 'test-key'}), patch.object(audio.ASR, '_prepare_segments', AsyncMock(return_value=[source])), patch.object(audio.ASR, '_transcribe_segment', AsyncMock(side_effect=RuntimeError('ASR offline'))), patch.object(audio, 'AsyncOpenAI'):
                await self.execution._handle_message_batch(batch, 0)
            assert not source.exists()
            self.runtime.send_card.assert_awaited_once()
            notification = self.runtime.send_card.await_args.kwargs
            assert notification['union_id'] == 'user-1'
            assert 'upload.ogg' in notification['content']
            assert CONFIG['messages']['transcriptionFailedSuffix'] in notification['content']
            self.runtime.codex.running.assert_not_called()

class BackendInterruptTests:

    def setup_method(self) -> None:
        self._pytest_stack.enter_context(patch.object(session_history, 'register_session', AsyncMock()))
        self._pytest_stack.enter_context(patch.object(session_codex, '_schedule_session_title'))
        self._pytest_stack.enter_context(patch.object(session_codex, '_sync_session_time', AsyncMock()))

    async def test_interrupt_arriving_during_turn_start_is_delivered(self) -> None:
        starting, resume = (asyncio.Event(), asyncio.Event())

        async def events():
            yield NS(method='item/agentMessage/delta', payload=NS(delta='answer', item_id='a'))
        handle = NS(interrupt=AsyncMock(), stream=events)

        async def turn(**kwargs):
            starting.set()
            await resume.wait()
            return handle
        with patch.object(Codex, '_active_turns', {}), patch.object(Codex, '_pending_interrupts', set()), patch.object(Codex, '_turns_guard', asyncio.Lock()), patch.object(thread_manager, 'get_user_thread', AsyncMock(return_value=None)), patch.object(thread_manager, 'set_user_thread', AsyncMock()), patch.object(codex_execution, 'prepare_workspace', AsyncMock()), patch.object(codex_runtime, 'AsyncCodex') as client_class:
            client = client_class.return_value.__aenter__.return_value
            client.thread_start.return_value = NS(id='thread-1', turn=turn)
            stream = Codex.running('user-1', 'hello', run_id='run')
            task = asyncio.create_task(anext(stream))
            try:
                await asyncio.wait_for(starting.wait(), 1)
                assert not await Codex.interrupt('run')
                resume.set()
                await asyncio.wait_for(task, 1)
                handle.interrupt.assert_awaited_once()
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await stream.aclose()
            assert not Codex._active_turns
            assert not Codex._pending_interrupts

    async def test_pending_interrupt_and_handle_cleanup(self) -> None:
        with patch.object(Codex, '_active_turns', {}), patch.object(Codex, '_pending_interrupts', set()), patch.object(Codex, '_turns_guard', asyncio.Lock()):
            assert not await Codex.interrupt('run')
            assert 'run' in Codex._pending_interrupts
            handle = NS(interrupt=AsyncMock())
            Codex._active_turns['run'] = handle
            assert await Codex.interrupt('run')
            handle.interrupt.assert_awaited_once()
            await Codex.forget_run('run')
            assert not Codex._pending_interrupts
            assert not Codex._active_turns

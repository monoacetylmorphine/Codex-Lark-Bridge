"""Verify card requests and gateway stream adaptation without real credentials or Lark access."""
from __future__ import annotations
import pytest
import ast
import asyncio
import importlib
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import aclosing

def response(**data):
    return SimpleNamespace(success=lambda: True, data=SimpleNamespace(**data))

class LarkCardTests:

    def setup_method(self) -> None:
        self.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=Mock(return_value=response(message_id='message-1'))))), cardkit=SimpleNamespace(v1=SimpleNamespace(card=SimpleNamespace(create=Mock(return_value=response(card_id='card-1')), settings=Mock(return_value=response())), card_element=SimpleNamespace(content=Mock(return_value=response())))))
        stub = ModuleType('codex_bridge.services.lark.lark_client')
        stub.client = self.client
        with patch.dict(sys.modules, {'codex_bridge.services.lark.lark_client': stub}):
            self.card = importlib.import_module('codex_bridge.services.lark.lark_message_card')
        self.client_patch = patch.object(self.card, 'client', self.client)
        self.client_patch.start()
        self._pytest_stack.callback(self.client_patch.stop)

    def use_async_api(self, after_call=None):
        """Deterministic offline API for timer tests, without worker-thread jitter."""

        async def call(operation, request):
            result = operation(request)
            if after_call:
                after_call(operation, request)
            if not result.success():
                raise self.card.CardRequestError(result.msg, code=result.code)
            return result
        self._pytest_stack.enter_context(patch.object(self.card, '_call', side_effect=call))

    async def test_static_card_and_recipient_routing(self) -> None:
        for recipient, kind in [('on_user', 'union_id'), ('oc_group', 'chat_id')]:
            assert await self.card.sending_card(recipient, '**Hello**') == 'message-1'
            request = self.client.im.v1.message.create.call_args.args[0]
            assert ('receive_id_type', kind) in request.queries
            assert request.request_body.msg_type == 'interactive'
            body = json.loads(request.request_body.content)
            assert body['schema'] == '2.0'
            assert body['header']['template'] == 'blue'
            assert not body['config']['streaming_mode']
            assert body['body']['elements'][0]['content'] == '**Hello**'
        self.client.cardkit.v1.card.create.assert_not_called()

    async def test_markdown_images_are_sent_as_bare_urls(self) -> None:
        content = 'Generated: ![Preview](https://example.com/image.jpeg?x=1&y=2)\nRegular link: [Details](https://example.com/detail)\nEscaped syntax: \\![Example](https://example.com/example.png)'
        await self.card.sending_card('on_user', content)
        request = self.client.im.v1.message.create.call_args.args[0]
        body = json.loads(request.request_body.content)
        assert body['body']['elements'][0]['content'] == 'Generated: https://example.com/image.jpeg?x=1&y=2\nRegular link: [Details](https://example.com/detail)\nEscaped syntax: \\![Example](https://example.com/example.png)'

    async def test_stream_converts_image_only_after_markdown_is_complete(self) -> None:

        async def chunks():
            yield 'Generated: ![Preview](https://example.com/image.jpeg?x=1'
            yield '&y=2)'
        with patch.object(self.card, 'UPDATE_INTERVAL', 0):
            await self.card.sending_card('on_user', chunks())
        created = json.loads(self.client.cardkit.v1.card.create.call_args.args[0].request_body.data)
        assert created['body']['elements'][0]['content'] == 'Generated: ![Preview](https://example.com/image.jpeg?x=1'
        write = self.client.cardkit.v1.card_element.content.call_args.args[0]
        assert write.request_body.content == 'Generated: https://example.com/image.jpeg?x=1&y=2'

    async def test_stream_sends_before_end_and_flushes_tail(self) -> None:

        async def chunks():
            yield 'Hel'
            self.client.im.v1.message.create.assert_called_once()
            yield 'lo'
            self.client.cardkit.v1.card_element.content.assert_called_once()
            yield '!'
        with patch.object(self.card, 'UPDATE_INTERVAL', 0):
            result = await self.card.sending_card('on_user', chunks())
        assert result == 'message-1'
        writes = self.client.cardkit.v1.card_element.content.call_args_list
        assert [c.args[0].request_body.content for c in writes] == ['Hello', 'Hello!']
        close = self.client.cardkit.v1.card.settings.call_args.args[0].request_body
        assert [c.args[0].request_body.sequence for c in writes] + [close.sequence] == [1, 2, 3]
        assert not json.loads(close.settings)['config']['streaming_mode']
        assert len({c.args[0].request_body.uuid for c in writes} | {close.uuid}) == 3

    async def test_coalescing_keeps_final_text(self) -> None:

        async def chunks():
            for chunk in ['a', 'b', 'c']:
                yield chunk
        with patch.object(self.card, 'UPDATE_INTERVAL', 999):
            await self.card.sending_card('on_user', chunks())
        write = self.client.cardkit.v1.card_element.content
        write.assert_called_once()
        assert write.call_args.args[0].request_body.content == 'abc'

    async def test_empty_stream_creates_no_message(self) -> None:

        async def chunks():
            yield ''
        assert await self.card.sending_card('on_user', chunks()) is None
        assert await self.card.sending_card('on_user', '') is None
        self.client.cardkit.v1.card.create.assert_not_called()
        self.client.im.v1.message.create.assert_not_called()

    async def test_recall_discards_pending_text(self) -> None:

        async def chunks():
            yield 'Displayed'
            yield 'Must not be displayed'
            raise self.card.CardStreamStopped()
        with patch.object(self.card, 'UPDATE_INTERVAL', 999):
            await self.card.sending_card('on_user', chunks())
        self.client.cardkit.v1.card_element.content.assert_not_called()
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_producer_failure_closes_stream(self) -> None:

        async def chunks():
            yield 'Partial result'
            raise ValueError('producer failed')
        with pytest.raises(ValueError, match='producer failed'):
            await self.card.sending_card('on_user', chunks())
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_api_failure_is_reported_and_stream_closed(self) -> None:
        self.client.cardkit.v1.card_element.content.return_value = SimpleNamespace(success=lambda: False, code=999, msg='denied', get_log_id=lambda: 'log-1')

        async def chunks():
            yield 'a'
            yield 'b'
        with pytest.raises(RuntimeError, match='code=999'):
            await self.card.sending_card('on_user', chunks())
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_cancellation_closes_stream(self) -> None:

        async def chunks():
            yield 'a'
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await self.card.sending_card('on_user', chunks())
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_http_wait_has_a_deadline(self) -> None:
        from codex_bridge.configs.loader import CONFIG
        release = threading.Event()

        def blocked(request):
            release.wait(1)
            return response()
        try:
            with patch.object(self.card, 'settings', return_value={'cardRequestTimeoutSeconds': 0.01}):
                with pytest.raises(self.card.CardRequestError) as caught:
                    await self.card._call(blocked, None)
                assert isinstance(caught.value.__cause__, TimeoutError)
        finally:
            release.set()

    async def test_reasoning_is_replaced_by_answer_in_same_card(self) -> None:
        source = ast.parse((Path(__file__).resolve().parents[1] / 'middleware/gateway_execution.py').read_text())
        fn = next((n for n in ast.walk(source) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_reply_content'))

        async def recalled(state):
            return False

        async def events(**kwargs):
            yield {'type': 'reasoning', 'content': 'Reasoning 1'}
            yield {'type': 'reasoning', 'content': 'Reasoning 2'}
            write = self.client.cardkit.v1.card_element.content
            assert write.call_args.args[0].request_body.content == 'Reasoning 1Reasoning 2'
            with patch.object(self.card, 'UPDATE_INTERVAL', 999):
                yield {'type': 'reasoning', 'content': 'Buffered reasoning'}
                yield {'type': 'answer', 'content': ''}
                yield {'type': 'usage', 'content': 'Token usage'}
                yield {'type': 'answer', 'content': 'Answer 1'}
                assert write.call_args.args[0].request_body.content == 'Answer 1'
                yield {'type': 'answer', 'content': 'Answer 2'}
                yield {'type': 'usage', 'content': 'Final usage'}
                yield {'type': 'done'}
        namespace = dict(aclosing=aclosing, Codex=SimpleNamespace(running=events), _run_was_interrupted=recalled, CardStreamStopped=self.card.CardStreamStopped, CardReplace=self.card.CardReplace, CONFIG={'messages': {'codexFailure': 'failed'}})
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'gateway_execution.py', 'exec'), namespace)
        adapter = SimpleNamespace(runtime=SimpleNamespace(codex=namespace.get('Codex'), _run_was_interrupted=namespace['_run_was_interrupted']))
        stream = namespace['_reply_content'](adapter, SimpleNamespace(union_id='on_user'), 'prompt', SimpleNamespace(run_id='run-1'))
        with patch.object(self.card, 'UPDATE_INTERVAL', 0):
            await self.card.sending_card('on_user', stream)
        self.client.im.v1.message.create.assert_called_once()
        self.client.cardkit.v1.card.create.assert_called_once()
        writes = self.client.cardkit.v1.card_element.content.call_args_list
        assert [c.args[0].request_body.content for c in writes] == ['Reasoning 1Reasoning 2', 'Answer 1', 'Answer 1Answer 2']
        close = self.client.cardkit.v1.card.settings.call_args.args[0].request_body
        assert json.loads(close.settings)['config']['summary']['content'] == 'Answer 1Answer 2'
        assert [c.args[0].request_body.sequence for c in writes] + [close.sequence] == [1, 2, 3, 4]

    async def test_gateway_adapter_streams_errors_and_stops_on_recall(self) -> None:
        source = ast.parse((Path(__file__).resolve().parents[1] / 'middleware/gateway_execution.py').read_text())
        fn = next((n for n in ast.walk(source) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_reply_content'))
        state = SimpleNamespace(interrupted=False, run_id='run-1')
        closed = []

        async def events(**kwargs):
            try:
                yield {'type': 'answer', 'content': 'Body'}
                yield {'type': 'error', 'content': 'Error'}
            finally:
                closed.append(True)

        async def recalled(state):
            return state.interrupted
        namespace = dict(aclosing=aclosing, Codex=SimpleNamespace(running=events), _run_was_interrupted=recalled, CardStreamStopped=self.card.CardStreamStopped, CardReplace=self.card.CardReplace, CONFIG={'messages': {'codexFailure': 'failed'}})
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'gateway_execution.py', 'exec'), namespace)
        adapter = SimpleNamespace(runtime=SimpleNamespace(codex=namespace.get('Codex'), _run_was_interrupted=namespace['_run_was_interrupted']))
        stream = namespace['_reply_content'](adapter, SimpleNamespace(union_id='on_user'), 'prompt', state)
        assert [chunk async for chunk in stream] == [self.card.CardReplace('Body'), '\n\nError']
        stream = namespace['_reply_content'](adapter, SimpleNamespace(union_id='on_user'), 'prompt', state)
        assert await anext(stream) == self.card.CardReplace('Body')
        state.interrupted = True
        with pytest.raises(self.card.CardStreamStopped):
            await anext(stream)
        assert len(closed) == 2

    async def test_tail_flushes_during_silence_without_cancelling_source(self) -> None:
        flushed, finish = (asyncio.Event(), asyncio.Event())
        cancelled = []

        def after(operation, request):
            if operation is self.client.cardkit.v1.card_element.content:
                flushed.set()
        self.use_async_api(after)

        async def chunks():
            yield 'Reasoning'
            yield 'Tail'
            try:
                await finish.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        with patch.object(self.card, 'UPDATE_INTERVAL', 0.02):
            task = asyncio.create_task(self.card.sending_card('on_user', chunks()))
            try:
                await asyncio.wait_for(flushed.wait(), 1)
                assert not task.done()
                assert not cancelled
                write = self.client.cardkit.v1.card_element.content.call_args.args[0]
                assert write.request_body.content == 'ReasoningTail'
            finally:
                finish.set()
                await asyncio.wait_for(task, 1)

    async def test_nine_minute_rotation_closes_while_silent_and_continues_on_demand(self) -> None:
        assert self.card.STREAM_LIFETIME == 540
        closed, resume, second, finish = (asyncio.Event() for _ in range(4))

        def after(operation, request):
            if operation is self.client.cardkit.v1.card.settings:
                closed.set()
            if operation is self.client.im.v1.message.create and operation.call_count == 2:
                second.set()
        self.use_async_api(after)
        self.client.cardkit.v1.card.create.side_effect = [response(card_id='first'), response(card_id='second')]
        self.client.im.v1.message.create.side_effect = [response(message_id='m1'), response(message_id='m2')]

        async def chunks():
            yield 'Old card'
            yield 'Tail'
            await resume.wait()
            yield 'New content'
            await finish.wait()
        with patch.object(self.card, 'STREAM_LIFETIME', 0.05), patch.object(self.card, 'UPDATE_INTERVAL', 999):
            task = asyncio.create_task(self.card.sending_card('on_user', chunks()))
            try:
                await asyncio.wait_for(closed.wait(), 1)
                self.client.im.v1.message.create.assert_called_once()
                assert not task.done()
                write = self.client.cardkit.v1.card_element.content.call_args.args[0]
                assert write.card_id == 'first'
                assert write.request_body.content == 'Old cardTail'
                resume.set()
                await asyncio.wait_for(second.wait(), 1)
            finally:
                resume.set()
                finish.set()
                result = await asyncio.wait_for(task, 1)
        assert result == 'm2'
        bodies = [json.loads(c.args[0].request_body.data) for c in self.client.cardkit.v1.card.create.call_args_list]
        assert bodies[1]['body']['elements'][0]['content'] == 'New content'
        assert bodies[1]['header']['title']['content'] == 'Codex'
        closes = self.client.cardkit.v1.card.settings.call_args_list
        assert [c.args[0].card_id for c in closes] == ['first', 'second']
        assert [c.args[0].request_body.sequence for c in closes] == [2, 1]

    async def test_expiry_without_further_text_does_not_create_empty_card(self) -> None:
        closed = asyncio.Event()
        self.use_async_api(lambda op, req: closed.set() if op is self.client.cardkit.v1.card.settings else None)

        async def chunks():
            yield 'Only this chunk'
            await asyncio.wait_for(closed.wait(), 1)
        with patch.object(self.card, 'STREAM_LIFETIME', 0.02):
            await asyncio.wait_for(self.card.sending_card('on_user', chunks()), 1)
        self.client.cardkit.v1.card.create.assert_called_once()
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_closed_stream_recovers_unsent_suffix_and_full_replacement(self) -> None:
        self.use_async_api()
        for chunk, expected in [('Continuation', 'Continuation'), (self.card.CardReplace('Old text answer'), 'Old text answer')]:
            self.client.cardkit.v1.card.create.reset_mock()
            self.client.cardkit.v1.card.create.side_effect = [response(card_id='old'), response(card_id='new')]
            self.client.cardkit.v1.card_element.content.return_value = SimpleNamespace(success=lambda: False, code=300309, msg='streaming mode is closed')

            async def chunks():
                yield 'Old text'
                yield chunk
            await self.card.sending_card('on_user', chunks())
            body = json.loads(self.client.cardkit.v1.card.create.call_args.args[0].request_body.data)
            assert body['body']['elements'][0]['content'] == expected

    async def test_delivery_failure_drains_upstream_before_reporting(self) -> None:
        self.use_async_api()
        self.client.cardkit.v1.card_element.content.return_value = SimpleNamespace(success=lambda: False, code=999, msg='unavailable')
        completed = []

        async def chunks():
            yield 'one'
            yield 'two'
            yield 'three'
            completed.append(True)
        with patch.object(self.card, 'UPDATE_INTERVAL', 0):
            with pytest.raises(self.card.CardDeliveryError):
                await self.card.sending_card('on_user', chunks())
        assert completed == [True]
        self.client.cardkit.v1.card_element.content.assert_called_once()
        self.client.im.v1.message.create.assert_called_once()

    async def test_cancellation_cleans_pending_read_and_closes_card(self) -> None:
        waiting, cleaned = (asyncio.Event(), asyncio.Event())
        self.use_async_api()

        async def chunks():
            try:
                yield 'one'
                waiting.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        task = asyncio.create_task(self.card.sending_card('on_user', chunks()))
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        self.client.cardkit.v1.card.settings.assert_called_once()

    async def test_commentary_and_tools_do_not_hide_following_reasoning(self) -> None:
        source = ast.parse((Path(__file__).resolve().parents[1] / 'middleware/gateway_execution.py').read_text())
        fn = next((n for n in ast.walk(source) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_reply_content'))

        async def recalled(state):
            return False

        async def events():
            yield {'type': 'reasoning', 'content': 'Reasoning 1', 'item_id': 'r1'}
            yield {'type': 'answer', 'phase': 'commentary', 'content': 'Progress', 'item_id': 'a1'}
            yield {'type': 'progress', 'content': 'Agent executing commandExecution tool\n', 'item_id': 'tool'}
            yield {'type': 'usage', 'content': 'Usage'}
            yield {'type': 'reasoning', 'content': 'Reasoning 2', 'item_id': 'r2'}
            yield {'type': 'reasoning', 'content': 'Continuation', 'item_id': 'r2'}
            yield {'type': 'answer', 'phase': 'final_answer', 'content': 'Final answer', 'item_id': 'a2'}
            yield {'type': 'usage', 'content': 'Final usage must not overwrite the answer'}
            yield {'type': 'progress', 'content': 'hook must not overwrite the answer'}
        namespace = dict(aclosing=aclosing, _run_was_interrupted=recalled, CardStreamStopped=self.card.CardStreamStopped, CardReplace=self.card.CardReplace)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'gateway_execution.py', 'exec'), namespace)
        adapter = SimpleNamespace(runtime=SimpleNamespace(codex=namespace.get('Codex'), _run_was_interrupted=namespace['_run_was_interrupted']))
        chunks = [c async for c in namespace['_reply_content'](adapter, None, None, None, events=events())]
        assert chunks == ['Reasoning 1', '\n\nProgress', '\n\nAgent executing commandExecution tool\n', '\n\nReasoning 2', 'Continuation', self.card.CardReplace('Final answer')]
        await self.card.sending_card('user-1', namespace['_reply_content'](adapter, None, None, None, events=events()))
        updates = self.client.cardkit.v1.card_element.content.call_args_list
        assert all(('Usage' not in call.args[0].request_body.content for call in updates))
        assert updates[-1].args[0].request_body.content == 'Final answer'

class CardSteerTests:
    """Control/HTTP races with the actual sender and bounded source prefetch."""
    setup_method = LarkCardTests.setup_method
    use_async_api = LarkCardTests.use_async_api

    async def start_controlled(self, *, cancelled=lambda: False):
        from codex_bridge.configs.loader import CONFIG
        queue = asyncio.Queue()
        opened = asyncio.Event()
        self.use_async_api(lambda op, req: opened.set() if op is self.client.im.v1.message.create else None)
        self.client.cardkit.v1.card.create.side_effect = [response(card_id=f'c{i}') for i in range(1, 10)]
        session = self.card.CardStreamSession(cancelled=cancelled)

        async def source():
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                yield {'type': 'text', 'content': chunk}

        async def content():
            async with aclosing(session.events(source())) as events:
                async for event in events:
                    yield (event['control'] if event['type'] == 'card_control' else event['content'])

        async def send():
            async with aclosing(content()) as stream:
                return await self.card.sending_card('on_user', stream, session=session)
        task = asyncio.create_task(send())

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._pytest_async_cleanups.append(cleanup)
        await queue.put('old')
        await asyncio.wait_for(opened.wait(), 1)
        return (session, queue, task, CONFIG['messages']['steerAccepted'])

    async def test_steer_barrier_pauses_timer_and_discards_old_buffer(self) -> None:
        with patch.object(self.card, 'UPDATE_INTERVAL', 0.04):
            session, queue, task, text = await self.start_controlled()
            await queue.put('unsent')
            async with asyncio.timeout(1):
                while session.accumulated != 'oldunsent':
                    await asyncio.sleep(0)
            async with session.steering(text) as barrier:
                await asyncio.sleep(0.06)
                self.client.cardkit.v1.card_element.content.assert_not_called()
                barrier.decide(True)
                assert await barrier.applied
            await queue.put('new')
            await queue.put(None)
            await asyncio.wait_for(task, 1)
        writes = self.client.cardkit.v1.card_element.content.call_args_list
        assert [(c.args[0].card_id, c.args[0].request_body.content) for c in writes] == [('c2', 'new')]

    async def test_failed_steer_preserves_old_buffer(self) -> None:
        with patch.object(self.card, 'UPDATE_INTERVAL', 999):
            session, queue, task, text = await self.start_controlled()
            await queue.put('tail')
            async with session.steering(text):
                pass
            await queue.put(None)
            await asyncio.wait_for(task, 1)
        self.client.cardkit.v1.card.create.assert_called_once()
        assert self.client.cardkit.v1.card_element.content.call_args.args[0].request_body.content == 'oldtail'

    async def test_accepted_steer_with_eof_closes_placeholder_as_finished(self) -> None:
        from codex_bridge.configs.loader import CONFIG
        session, queue, task, text = await self.start_controlled()
        async with session.steering(text) as barrier:
            await queue.put(None)
            await asyncio.sleep(0)
            barrier.decide(True)
            assert await barrier.applied
        await asyncio.wait_for(task, 1)
        assert self.client.cardkit.v1.card.create.call_count == 2
        assert self.client.cardkit.v1.card_element.content.call_args.args[0].request_body.content == CONFIG['messages']['steerCompleted']

    async def test_closed_session_never_waits_for_missing_writer(self) -> None:
        session, queue, task, text = await self.start_controlled()
        await queue.put(None)
        await task
        async with asyncio.timeout(1), session.steering(text) as barrier:
            assert not await barrier.ready
            assert not await barrier.applied

    async def test_sender_cancellation_releases_pending_steer(self) -> None:
        session, queue, task, text = await self.start_controlled()
        async with session.steering(text) as barrier:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert not await asyncio.wait_for(barrier.applied, 1)
        assert session.closed

    async def test_steer_after_card_expired_creates_immediate_new_card(self) -> None:
        with patch.object(self.card, 'STREAM_LIFETIME', 0.03):
            session, queue, task, text = await self.start_controlled()
            async with asyncio.timeout(1):
                while not self.client.cardkit.v1.card.settings.called:
                    await asyncio.sleep(0.005)
            async with session.steering(text) as barrier:
                barrier.decide(True)
                assert await barrier.applied
            assert self.client.cardkit.v1.card.create.call_count == 2
            await queue.put('new')
            await queue.put(None)
            await task

    async def test_delivery_failure_still_releases_later_steer_barrier(self) -> None:
        session, queue, task, text = await self.start_controlled()
        self.client.cardkit.v1.card.create.side_effect = self.card.CardRequestError('offline')
        for _ in range(2):
            async with session.steering(text) as barrier:
                barrier.decide(True)
                assert not await asyncio.wait_for(barrier.applied, 1)
        await queue.put(None)
        with pytest.raises(self.card.CardDeliveryError):
            await task
        assert self.client.cardkit.v1.card.create.call_count == 2

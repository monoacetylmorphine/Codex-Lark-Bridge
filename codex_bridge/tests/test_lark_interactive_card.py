"""Offline private-chat history card regression using real SDK models and mocked external requests and restoration."""
from __future__ import annotations
import pytest
from codex_bridge.configs.loader import CONFIG
from codex_bridge.middleware import gateway_commands
from codex_bridge.codex import thread_manager
import asyncio
import json
import sys
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
from codex_bridge.services.lark import lark_interactive_card as cards
from codex_bridge.middleware.message_collector import batch_from_chat_history, is_history_command
from codex_bridge.codex import codex_execution as codex, thread_manager
from codex_bridge.session import session_history
import test_stop_command as helpers

def callback(card, *, selected='thread-1', action='activate_history', user='user-1', **changes):
    payload = {'operator': {'union_id': user}, 'context': {'open_chat_id': card.chat_id, 'open_message_id': card.message_id}, 'action': {'tag': 'button', 'name': 'activate_history', 'value': {'action': action, 'ticket': card.token, 'revision': card.revision}, 'form_value': {cards.SELECT_NAME: selected}}}
    payload.update(changes)
    return P2CardActionTrigger({'event': payload})

def options(count=1):
    return [{'label': 'Same-name session', 'value': f'thread-{index + 1}', 'updated_at': None} for index in range(count)]

class InteractiveCardTests:

    def setup_method(self) -> None:
        self.store = cards.HistoryCardStore()
        self.card = self.store.create('user-1', 'chat-1', options(cards.PAGE_SIZE + 3))
        self.card.message_id = 'message-1'

    def test_form_requires_selection_and_confirmation(self) -> None:
        body = cards.build_history_card(self.card)
        assert body['schema'] == '2.0'
        form = body['body']['elements'][1]
        select, button = form['elements']
        assert form['tag'] == 'form'
        assert select['required']
        assert 'behaviors' not in select
        assert 'initial_option' not in select
        assert len(select['options']) == cards.PAGE_SIZE
        assert len({item['value'] for item in select['options']}) == cards.PAGE_SIZE
        assert button['form_action_type'] == 'submit'
        assert 'action_type' not in button
        assert 'confirm' in button
        assert 'stop the current' in button['confirm']['text']['content']
        self.card.page = 1
        assert len(cards.build_history_card(self.card)['body']['elements'][1]['elements'][0]['options']) == 3

    def test_empty_history_has_no_controls(self) -> None:
        self.card.options = ()
        assert 'select_static' not in json.dumps(cards.build_history_card(self.card))

    def test_identity_scope_expiry_and_replay(self) -> None:
        assert self.store.resolve(callback(self.card)) is self.card
        for data in (callback(self.card, user='other'), callback(self.card, user=None), callback(self.card, context={'open_chat_id': 'group', 'open_message_id': 'message-1'}), callback(self.card, context={'open_chat_id': 'chat-1', 'open_message_id': 'forwarded'})):
            with pytest.raises(ValueError):
                self.store.resolve(data)
        self.card.finished = True
        with pytest.raises(ValueError):
            self.store.resolve(callback(self.card))
        self.card.finished = False
        self.card.created_at -= cards.CARD_TTL_SECONDS
        with pytest.raises(ValueError):
            self.store.resolve(callback(self.card))

    def test_only_confirmed_visible_form_option_is_accepted(self) -> None:
        assert cards.confirmed_thread_id(callback(self.card), self.card) == 'thread-1'
        for selected in (None, [], 'foreign', f'thread-{cards.PAGE_SIZE + 1}'):
            with pytest.raises(ValueError):
                cards.confirmed_thread_id(callback(self.card, selected=selected), self.card)
        data = callback(self.card)
        data.event.action.tag = 'select_static'
        data.event.action.option = 'thread-1'
        with pytest.raises(ValueError):
            cards.confirmed_thread_id(data, self.card)

    async def test_send_and_update_use_real_sdk_requests(self) -> None:
        client = NS(im=NS(v1=NS(message=NS(create=Mock(), patch=Mock()))))
        stub = ModuleType('codex_bridge.services.lark.lark_client')
        stub.client = client
        call = AsyncMock(return_value=NS(success=lambda: True, data=NS(message_id='sent')))
        with patch.dict(sys.modules, {stub.__name__: stub}), patch.object(cards, 'call_lark', call):
            body = cards.build_history_card(self.card)
            assert await cards.send_interactive_card('user-1', body) == 'sent'
            request = call.call_args.args[1]
            assert ('receive_id_type', 'union_id') in request.queries
            assert request.request_body.receive_id == 'user-1'
            assert json.loads(request.request_body.content) == body
            await cards.update_interactive_card('sent', cards.status_card('Activated'))
            assert call.call_args.args[1].message_id == 'sent'
            call.return_value = NS(success=lambda: False, code=123, get_log_id=lambda: 'log')
            with pytest.raises(RuntimeError, match='123'):
                await cards.send_interactive_card('user-1', body)

class HistoryInteractionGatewayTests:

    def setup_method(self) -> None:
        helpers.StopTests.setup_method(self)
        self.send = self._pytest_stack.enter_context(patch.object(cards, 'send_interactive_card', AsyncMock(return_value='message-1')))
        self.update = self._pytest_stack.enter_context(patch.object(cards, 'update_interactive_card', AsyncMock()))
        self.listing = self._pytest_stack.enter_context(patch.object(gateway_commands, 'list_sessions', AsyncMock(return_value=[NS(thread_id='thread-1', thread_name='History name', updated_at=None)])))
        self.real_restore = self.commands.processing_history_restore
        self.restore = self._pytest_stack.enter_context(patch.object(self.commands, 'processing_history_restore', AsyncMock(return_value={'ok': True, 'thread_id': 'thread-1'})))
        self.card = self.commands.history_cards.create('user-1', 'chat-1', options())
        self.card.message_id = 'message-1'

    async def test_private_command_bypasses_model_and_group_unchanged(self) -> None:
        await self.router.processing(helpers.event(' /HISTORY '))
        self.listing.assert_awaited_once_with('user-1')
        self.send.assert_awaited_once()
        self.router.fetch_history.assert_not_awaited()
        self.router.add_reaction.assert_not_awaited()
        self.restore.assert_not_awaited()
        self.send.reset_mock()
        await self.commands.processing_history(helpers.event('/history', chat_type='group'))
        self.send.assert_not_awaited()
        route = AsyncMock()
        with patch.object(self.router, '_route_message', route), patch('codex_bridge.middleware.message_router._is_bot_mentioned', return_value=True):
            await self.router.processing(helpers.event('/history', chat_type='group', message_id='group-1'))
        route.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_confirm_activates_once_with_trusted_context(self) -> None:
        data = callback(self.card)
        await self.commands.processing_history_action(data)
        await self.commands.processing_history_action(data)
        self.restore.assert_awaited_once()
        trusted, thread_id = self.restore.call_args.args
        assert trusted.event.message.chat_type == 'p2p'
        assert trusted.event.message.chat_id == 'chat-1'
        assert trusted.event.sender.sender_id.union_id == 'user-1'
        assert thread_id == 'thread-1'
        self.update.assert_awaited_once()
        assert 'Activated' in json.dumps(self.update.call_args.args[1], ensure_ascii=False)

    async def test_cancel_unknown_select_and_foreign_callbacks_do_not_restore(self) -> None:
        dispatcher = Mock()
        reply = self.commands.dispatch_history_action(dispatcher, callback(self.card, action='cancel'))
        assert 'No action' in reply.toast.content
        dispatcher.submit.assert_not_called()
        for data in (callback(self.card, user='other'), callback(self.card, selected='foreign')):
            await self.commands.processing_history_action(data)
        data = callback(self.card)
        data.event.action.tag = 'select_static'
        await self.commands.processing_history_action(data)
        self.restore.assert_not_awaited()
        assert not self.card.finished

    async def test_failure_consumes_confirmation_and_delivery_failure_does_not_restore_twice(self) -> None:
        self.restore.return_value = {'ok': False}
        self.update.side_effect = RuntimeError('network')
        data = callback(self.card)
        await self.commands.processing_history_action(data)
        await self.commands.processing_history_action(data)
        self.restore.assert_awaited_once()
        assert 'Activation failed' in json.dumps(self.send.call_args.args[1], ensure_ascii=False)
        assert self.card.finished
        assert not self.card.busy

    async def test_concurrent_confirmation_is_not_replayed(self) -> None:
        started, release = (asyncio.Event(), asyncio.Event())

        async def restore(*args):
            started.set()
            await release.wait()
            return {'ok': True}
        self.restore.side_effect = restore
        task = asyncio.create_task(self.commands.processing_history_action(callback(self.card)))
        await started.wait()
        await self.commands.processing_history_action(callback(self.card))
        release.set()
        await task
        self.restore.assert_awaited_once()

    async def test_pagination_rejects_old_revision_and_does_not_restore(self) -> None:
        self.card.options = tuple(options(cards.PAGE_SIZE + 1))
        data = callback(self.card, action='history_page')
        data.event.action.value['page'] = 1
        await self.commands.processing_history_action(data)
        updated = self.commands.history_cards.cards[self.card.token]
        assert updated.page == 1
        assert updated.revision == 1
        await self.commands.processing_history_action(data)
        self.update.assert_awaited_once()
        self.restore.assert_not_awaited()

    async def test_query_error_not_reported_as_empty_history(self) -> None:
        self.listing.side_effect = RuntimeError('database')
        await self.commands.processing_history(helpers.event('/history'))
        text = json.dumps(self.send.call_args.args[1], ensure_ascii=False)
        assert 'Failed to load or send session history' in text
        assert 'No session history' not in text

    async def test_history_card_limits_sorted_database_records_without_deleting_history(self) -> None:
        directory = self._pytest_stack.enter_context(TemporaryDirectory())
        self._pytest_stack.enter_context(patch.object(session_history, 'DB_PATH', Path(directory) / 'state.sqlite'))
        self._pytest_stack.enter_context(patch.object(gateway_commands, 'list_sessions', session_history.list_sessions))
        expected = []
        for index in range(36):
            thread_id = f'history-{index:02d}'
            updated_at = index * 7 % 10 if index else None
            await session_history.register_session('user-1', thread_id)
            if updated_at is not None:
                await session_history.update_session_time('user-1', thread_id, updated_at)
            expected.append((updated_at if updated_at is not None else -1, thread_id))
        await session_history.register_session('other', 'foreign')
        await session_history.update_session_time('other', 'foreign', 999)
        expected_ids = [item[1] for item in sorted(expected, reverse=True)]
        for limit in (30, 5, 40):
            with patch.dict(CONFIG['messaging'], sessionHistoryLimit=limit), patch.object(cards, 'PAGE_SIZE', limit):
                await self.commands.processing_history(helpers.event('/history'))
                body = self.send.call_args.args[1]
                values = body['body']['elements'][1]['elements'][0]['options']
                assert [item['value'] for item in values] == expected_ids[:limit]
                assert 'Next' not in json.dumps(body, ensure_ascii=False)
        legacy = dict(CONFIG['messaging'])
        legacy.pop('sessionHistoryLimit')
        with patch.dict(CONFIG['messaging'], legacy, clear=True):
            await self.commands.processing_history(helpers.event('/history'))
        assert len(self.send.call_args.args[1]['body']['elements'][1]['elements'][0]['options']) == 30
        assert len(await session_history.list_sessions('user-1')) == 36

    async def test_confirm_to_real_database_and_unarchive_failure_preserves_binding(self) -> None:
        directory = self._pytest_stack.enter_context(TemporaryDirectory())
        db_path = Path(directory) / 'state.sqlite'
        self._pytest_stack.enter_context(patch.object(session_history, 'DB_PATH', db_path))
        self._pytest_stack.enter_context(patch.object(thread_manager, 'DB_PATH', db_path))
        await session_history.register_session('user-1', 'thread-1')
        await session_history.update_session_time('user-1', 'thread-1', 100)
        sdk = NS(thread_unarchive=AsyncMock(return_value=NS(id='thread-1', read=AsyncMock(return_value=NS(thread=NS(updated_at=200))))))

        @asynccontextmanager
        async def session(run_id):
            yield sdk
        self._pytest_stack.enter_context(patch.object(codex.Codex, '_session', session))
        self.runtime.codex.restore_session = codex.Codex.restore_session
        self._pytest_stack.enter_context(patch.object(self.commands, 'processing_history_restore', self.real_restore))
        for failure in ('unarchive', 'database', None):
            await thread_manager.set_user_thread('user-1', 'old')
            self.card.finished = False
            sdk.thread_unarchive.reset_mock()
            sdk.thread_unarchive.side_effect = RuntimeError('unarchive') if failure == 'unarchive' else None
            binding = patch.object(thread_manager, 'set_user_thread', AsyncMock(side_effect=OSError('disk'))) if failure == 'database' else nullcontext()
            with binding:
                await self.commands.processing_history_action(callback(self.card))
            sdk.thread_unarchive.assert_awaited_once_with('thread-1')
            assert await thread_manager.get_user_thread('user-1') == ('old' if failure else 'thread-1')
            assert 'chat-1' not in self.runtime.cache.reset_tasks
            text = json.dumps(self.update.call_args.args[1], ensure_ascii=False)
            assert ('Activation failed' if failure else 'Activated') in text

    def test_callback_capacity_and_expiry_return_error_toast(self) -> None:
        dispatcher = Mock()
        dispatcher.submit.return_value = False
        assert self.commands.dispatch_history_action(dispatcher, callback(self.card)).toast.type == 'error'
        dispatcher.submit.reset_mock()
        self.card.created_at -= cards.CARD_TTL_SECONDS
        assert 'expired' in self.commands.dispatch_history_action(dispatcher, callback(self.card)).toast.content
        dispatcher.submit.assert_not_called()

class TestHistoryCommand:

    def test_exact_command_and_private_history_boundary(self) -> None:
        assert is_history_command('text', '{"text":" /HISTORY "}')
        for raw in ('[]', 'null', 'bad', '{"text":"please /history"}'):
            assert not is_history_command('text', raw)
        assert not batch_from_chat_history(helpers.event('/history'), []).messages
        items = [helpers.history('hello', 'next'), helpers.history('/history', 'history'), helpers.history('old', 'old')]
        batch = batch_from_chat_history(helpers.event('hello', message_id='next'), items)
        assert [item.message_id for item in batch.messages] == ['next']
        batch = batch_from_chat_history(helpers.event('/history', chat_type='group'), [])
        assert batch.messages[0].content == {'text': '/history'}

"""Offline verification of gateway assembly, event routing, and startup/shutdown without external services."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace as NS
import tomllib
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch
import pytest
import test_stop_command as helpers

class GatewayStartupTests:

    def setup_method(self) -> None:
        helpers.StopTests.setup_method(self)

    async def test_instances_share_one_cache_without_cross_gateway_state(self) -> None:
        first = (self.runtime, self.execution, self.commands, self.router)
        second = self.g.create_gateway()
        for runtime, execution, commands, router in (first, second):
            assert execution.runtime is runtime
            assert commands.runtime is runtime
            assert execution.cache is runtime.cache
            assert commands.cache is runtime.cache
            assert router.cache is runtime.cache
            with patch.object(router, '_cancel_buffer', AsyncMock()) as cancel:
                await commands.cancel_buffer('chat')
            cancel.assert_awaited_once_with('chat')
        self.runtime.cache.blocked_chats['chat'] = object()
        self.commands.history_cards.create('user', 'chat', [])
        assert not second[0].cache.blocked_chats
        assert not second[2].history_cards.cards
        assert self.runtime.cache.codex_locks_guard is not second[0].cache.codex_locks_guard

    async def test_main_routes_events_and_cancels_maintenance_on_disconnect(self) -> None:
        builder = Mock()
        callbacks = {}

        def registration(name):

            def register(callback):
                callbacks[name] = callback
                return builder
            return register
        for name in ('im_message_receive_v1', 'card_action_trigger', 'im_message_recalled_v1', 'im_message_message_read_v1', 'im_message_reaction_created_v1', 'im_message_reaction_deleted_v1', 'im_chat_access_event_bot_p2p_chat_entered_v1'):
            getattr(builder, 'register_p2_' + name).side_effect = registration(name)
        dispatcher = NS(submit=Mock(return_value=True))
        notices = NS(submit=Mock(return_value=True))
        started, cancelled = ([], [])

        async def maintain(operation):
            started.append(operation)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(operation)
        state = NS(interrupted=False, probe=NS(stop_reason=None))
        self.runtime.cache.all_runs['active'] = state
        self.runtime.codex.cleanup_control_sessions = AsyncMock()
        websocket = NS(start=Mock())

        async def run_websocket(start):
            start()
            await asyncio.sleep(0)
            for content in ('hello', '/stop', '/new', '/history'):
                callbacks['im_message_receive_v1'](helpers.event(content))
            recalled = NS(event=NS())
            callbacks['im_message_recalled_v1'](recalled)
            assert callbacks['card_action_trigger'](recalled) is card_result
            dispatcher.submit.return_value = False
            callbacks['im_message_receive_v1'](helpers.event('overflow'))
        card_result = object()
        with patch.object(self.g, '_bot_identity') as identity, patch.object(self.g, 'create_gateway', return_value=(self.runtime, self.execution, self.commands, self.router)), patch.object(self.g, 'EventDispatcher', side_effect=[dispatcher, notices]), patch.object(self.g.lark.EventDispatcherHandler, 'builder', return_value=builder), patch.object(self.g, 'create_websocket_client', return_value=websocket) as connect, patch.object(self.g, 'run_websocket', side_effect=run_websocket), patch.object(self.g, 'write_health'), patch.object(self.g, 'heartbeat', AsyncMock()), patch.object(self.runtime, 'maintain', side_effect=maintain), patch.object(self.runtime, '_interrupt_run', AsyncMock(return_value=True)) as stop, patch.object(self.commands, 'dispatch_history_action', return_value=card_result) as action:
            await self.g.main()
        identity.assert_called_once_with(required=True)
        connect.assert_called_once_with(builder.build.return_value)
        websocket.start.assert_called_once()
        assert len(started) == 4
        assert len(started) == len(cancelled) and all((item in cancelled for item in started))
        assert [c.kwargs['control'] for c in dispatcher.submit.call_args_list] == [False, True, True, True, True, False]
        assert dispatcher.submit.call_args_list[4].args[0] == self.commands.processing_recall
        action.assert_called_once()
        notices.submit.assert_called_once()
        assert state.interrupted
        assert state.probe.stop_reason == 'shutdown'
        stop.assert_awaited_once_with(state)

    async def test_history_cards_are_pruned_by_commands_maintenance(self) -> None:
        card = self.commands.history_cards.create('user', 'chat', [])
        from codex_bridge.services.lark.lark_interactive_card import CARD_TTL_SECONDS
        card.created_at -= CARD_TTL_SECONDS
        await self.commands.prune_history()
        assert not self.commands.history_cards.cards

    def test_console_and_docker_point_to_existing_main_module(self) -> None:
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / 'pyproject.toml').read_text())
        assert project['project']['scripts']['codex_bridge'] == 'codex_bridge.main:cli'
        assert 'CMD ["python", "-m", "codex_bridge.main"]' in (root / 'Dockerfile').read_text()
        assert (root / 'main.py').is_file()
        assert not (root / 'gateway.py').exists()

class HealthTests:

    def test_missing_stale_disconnected_and_dead_process_fail_closed(self):
        from codex_bridge.utils.health import healthy, write_health
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'health.json'
            assert not healthy(path)
            write_health(True, path)
            assert healthy(path)
            for state in ({'pid': os.getpid(), 'time': time.monotonic() - 30, 'ready': True}, {'pid': os.getpid(), 'time': time.monotonic(), 'ready': False}, {'pid': -1, 'time': time.monotonic(), 'ready': True}, {}):
                path.write_text(json.dumps(state))
                assert not healthy(path)
            path.write_text('invalid')
            assert not healthy(path)

    def test_sdk_connection_state_is_required(self):
        from codex_bridge.utils.health import connected
        assert not connected(NS())
        assert not connected(NS(_conn=None))
        assert not connected(NS(_conn=NS(state=NS(name='CLOSED'))))
        assert connected(NS(_conn=NS(state=NS(name='OPEN'))))

    async def test_websocket_exceptions_reach_main(self):
        from codex_bridge.utils.health import run_websocket

        def fail():
            raise ValueError('connection failed')
        with pytest.raises(ValueError, match='connection failed'):
            await run_websocket(fail)

    async def test_sigterm_closes_admission_and_requests_cleanup(self):
        fixture = GatewayStartupTests()
        fixture._pytest_stack = ExitStack()
        fixture.setup_method()
        self._pytest_stack.callback(fixture._pytest_stack.close)
        fixture.runtime.codex.cleanup_control_sessions = AsyncMock()
        gateway = fixture.g
        builder = Mock()
        for name in ('im_message_receive_v1', 'card_action_trigger', 'im_message_recalled_v1', 'im_message_message_read_v1', 'im_message_reaction_created_v1', 'im_message_reaction_deleted_v1', 'im_chat_access_event_bot_p2p_chat_entered_v1'):
            getattr(builder, 'register_p2_' + name).return_value = builder
        loop = asyncio.get_running_loop()
        handlers = {}

        async def wait_for_stop(start):
            handlers[signal.SIGTERM]()
            handlers[signal.SIGTERM]()
            await asyncio.sleep(0)
        dispatcher = NS(submit=Mock(return_value=True))
        with patch.object(gateway, '_bot_identity'), patch.object(gateway, 'EventDispatcher', return_value=dispatcher), patch.object(gateway.lark.EventDispatcherHandler, 'builder', return_value=builder), patch.object(gateway, 'create_gateway', return_value=(fixture.runtime, fixture.execution, fixture.commands, fixture.router)), patch.object(gateway, 'create_websocket_client', return_value=NS(start=Mock())), patch.object(gateway, 'run_websocket', side_effect=wait_for_stop), patch.object(gateway, 'write_health') as write, patch.object(gateway, 'heartbeat', AsyncMock()), patch.object(loop, 'add_signal_handler', side_effect=lambda sig, fn: handlers.update({sig: fn})), patch.object(loop, 'remove_signal_handler'):
            await gateway.main()
            write.assert_called_with(False)
            callback = builder.register_p2_im_message_receive_v1.call_args.args[0]
            callback(helpers.event('after shutdown'))
            dispatcher.submit.assert_not_called()

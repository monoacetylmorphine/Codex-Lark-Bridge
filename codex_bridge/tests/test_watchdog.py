"""Offline deadline, stop confirmation and process shutdown tests."""
from __future__ import annotations
import pytest
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from codex_bridge.codex import codex_execution, codex_runtime, thread_manager
from codex_bridge.session import session_codex, session_history
from codex_bridge.codex.codex_execution import Codex, LiveTurn
from codex_bridge.codex.thread_watchdog import RunProbe, settings, should_log_event, summarize_event
from codex_bridge.configs.loader import CONFIG, _load_config
import test_stop_command as helpers

class TestProbe:

    def test_hard_deadline_survives_activity_tools_and_steer(self) -> None:
        probe = RunProbe('run', 'chat', frozenset({'m'}), received_at=0)
        probe.phase = 'running'
        probe.last_activity = 899
        probe.tools.add('long-tool')
        probe.message_ids |= {'steered-message'}
        assert probe.expired(900) == 'run_timeout'

    def test_stream_activity_tracks_state_and_filters_deltas(self) -> None:
        probe = RunProbe('run', 'chat', frozenset({'m'}), last_activity=0)
        item_data = {'id': 'tool', 'type': 'commandExecution', 'aggregatedOutput': 'Done\n'}
        item = NS(id='tool', type='commandExecution', model_dump=Mock(return_value=item_data))
        probe.activity(NS(method='item/started', payload=NS(item=NS(root=item))))
        assert probe.tools == {'tool'}
        for method in ('item/reasoning/textDelta', 'item/reasoning/summaryTextDelta', 'item/agentMessage/delta', 'item/commandExecution/outputDelta'):
            assert not should_log_event(method)
            probe.activity(NS(method=method))
        assert probe.last_activity > 0
        assert probe.event_count == 5
        assert probe.last_event is None
        probe.activity(NS(method='item/completed', payload=NS(item=NS(root=item))))
        assert not probe.tools
        assert should_log_event('item/completed')
        assert probe.last_event == 'item/completed'
        item.model_dump.assert_not_called()

    def test_large_payloads_are_absent_from_console_summaries(self) -> None:
        content = 'PRIVATE_PAYLOAD' * 100000
        items = [NS(id='cmd', type='commandExecution', status='completed', exit_code=0, aggregated_output=content, command=content), NS(id='img', type='imageGeneration', status='completed', result=content, revised_prompt=content, saved_path=NS(root='/tmp/image.png'), transparent_background=False), NS(id='msg', type='agentMessage', text=content, phase='commentary'), NS(id='tool', type='mcpToolCall', result=content)]
        for item in items:
            event = NS(method='item/completed', payload=NS(item=NS(root=item)))
            summary = summarize_event(event)
            assert 'PRIVATE_PAYLOAD' not in json.dumps(summary)
            assert len(json.dumps(summary)) < 500
        assert items[1].result == content
        assert summarize_event(NS(method='item/completed', payload=NS(item=NS(root=items[1]))))['item']['savedPath'] == '/tmp/image.png'

    def test_turn_summary_omits_nested_items_and_preserves_retry_error(self) -> None:
        error = NS(message='Reconnecting... 2/5', additional_details='request timed out')
        event = NS(method='error', payload=NS(error=error, will_retry=True))
        summary = summarize_event(event)
        assert summary['willRetry']
        assert summary['error']['message'] == error.message
        turn = NS(id='turn', status='failed', duration_ms=42, error=error, items=[NS(result='PRIVATE_PAYLOAD')])
        summary = summarize_event(NS(method='turn/completed', payload=NS(turn=turn)))
        assert 'PRIVATE_PAYLOAD' not in json.dumps(summary)
        assert summary['turn']['status'] == 'failed'
        error.additional_details = 'x' * 10000
        assert len(summarize_event(event)['error']['additionalDetails']) == 501

    def test_startup_idle_and_terminal_rules(self) -> None:
        for origin in (0.0, 100.0, 1000000.0):
            clock = NS(monotonic=Mock(return_value=origin))
            with patch('codex_bridge.codex.thread_watchdog.time', clock), patch.dict(settings(), maxRunSeconds=1000, startupTimeoutSeconds=10, idleTimeoutSeconds=5, finalizationTimeoutSeconds=30):
                probe = RunProbe('r', 'c', frozenset(), received_at=origin, phase_at=origin, last_activity=origin)
                probe.phase = 'starting'
                assert probe.expired(origin + 10) == 'startup_timeout'
                probe.phase = 'running'
                assert probe.expired(origin + 5) == 'idle_timeout'
                probe.tools.add('tool')
                assert probe.expired(origin + 5) is None
                clock.monotonic.return_value = origin + 995
                probe.finish('completed')
                clock.monotonic.return_value = origin + 1000
                probe.finish('run_timeout')
                assert probe.terminal == 'completed'
                assert probe.cleanup_at == origin + 995
                assert probe.expired() is None
                assert probe.expired(origin + 1024.999) is None
                assert probe.expired(origin + 1025) == 'cleanup_timeout'

    def test_schema_and_invalid_deadlines(self) -> None:
        import jsonschema
        jsonschema.validate(CONFIG, json.loads((Path(__file__).resolve().parents[1] / 'configs/config_schema.json').read_text()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for value in (0, -1, 1.5, True, '10', float('inf')):
                config = json.loads(json.dumps(CONFIG))
                config['codex']['watchdog']['maxRunSeconds'] = value
                path.write_text(json.dumps(config))
                with pytest.raises(RuntimeError):
                    _load_config(path)

class ConfirmationTests:

    def setup_method(self) -> None:
        for name in ('_active_turns', '_live_turns', '_clients', '_processes', '_initializers'):
            self._pytest_stack.enter_context(patch.object(Codex, name, {}))
        self._pytest_stack.enter_context(patch.object(Codex, '_pending_interrupts', set()))
        self._pytest_stack.enter_context(patch.object(Codex, '_closed_runs', set()))
        self._pytest_stack.enter_context(patch.dict(settings(), interruptGraceSeconds=0.03, cleanupTimeoutSeconds=0.3))

    async def test_expiry_discards_indexes_after_failed_close_and_handles_late_init(self) -> None:
        release = asyncio.Event()
        proc = NS(poll=lambda: None, kill=Mock())
        manager = NS(_client=NS(_sync=NS(_proc=None)))

        async def initialize():
            await release.wait()
            manager._client._sync._proc = proc
        task = asyncio.create_task(initialize())
        Codex._clients['expired'] = manager
        Codex._initializers['expired'] = task
        Codex._pending_interrupts.add('expired')
        Codex._closed_runs.add('expired')
        with patch.object(Codex, 'force_close', AsyncMock(return_value=False)):
            await Codex.discard_expired_run('expired')
        for name in ('_clients', '_initializers', '_pending_interrupts', '_closed_runs'):
            assert 'expired' not in getattr(Codex, name)
        release.set()
        await task
        await asyncio.sleep(0)
        proc.kill.assert_called_once()

    async def test_interrupt_ack_is_not_stop_confirmation(self) -> None:
        idle = asyncio.Event()
        order = []

        async def interrupt():
            order.append('interrupt')

        async def read():
            order.append('read')
            await idle.wait()
            return NS(thread=NS(status=NS(root=NS(type='idle'))))
        live = LiveTurn(NS(id='thread', read=read), NS(interrupt=interrupt), 'm', 'p')
        Codex._live_turns['run'] = live
        task = asyncio.create_task(Codex.interrupt_and_confirm('run'))
        await asyncio.sleep(0)
        assert order == ['interrupt', 'read']
        assert not task.done()
        idle.set()
        assert await task

    async def test_stuck_status_and_stuck_interrupt_escalate(self) -> None:

        async def stuck():
            await asyncio.Event().wait()
        for stuck_rpc in ('read', 'interrupt'):
            read = stuck if stuck_rpc == 'read' else AsyncMock()
            interrupt = stuck if stuck_rpc == 'interrupt' else AsyncMock()
            Codex._live_turns['r'] = LiveTurn(NS(read=read), NS(interrupt=interrupt), 'm', 'p')
            with patch.object(Codex, 'force_close', AsyncMock(return_value=False)) as close:
                assert not await Codex.interrupt_and_confirm('r')
                close.assert_awaited_once_with('r')

    async def test_real_sdk_close_terminates_only_owned_process(self) -> None:
        from openai_codex import AsyncCodex
        owned = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.PIPE)
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.PIPE)
        client = AsyncCodex()
        client._client._sync._proc = owned
        Codex._clients['r'] = client
        try:
            assert await Codex.force_close('r')
            assert owned.poll() is not None
            assert other.poll() is None
        finally:
            for proc in (owned, other):
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=2)
                if proc.stdin:
                    proc.stdin.close()

    async def test_close_hang_kills_captured_process(self) -> None:
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])

        async def close():
            await asyncio.Event().wait()
        Codex._clients['r'] = NS(close=close, _client=NS(_sync=NS(_proc=proc)))
        try:
            assert await Codex.force_close('r')
            assert proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2)

    async def test_cancelled_initialization_retains_late_process_for_stop_retry(self) -> None:
        entered, release = (asyncio.Event(), asyncio.Event())
        process = Mock()
        process.poll.return_value = 0

        class DelayedClient:

            def __init__(self):
                self._client = NS(_sync=NS(_proc=None))

            async def __aenter__(self):
                entered.set()
                await release.wait()
                self._client._sync._proc = process
                return self

            async def close(self):
                if self._client._sync._proc is not None:
                    self._client._sync._proc.terminate()
                    self._client._sync._proc = None

            async def __aexit__(self, *args):
                await self.close()

        async def run():
            async with Codex._session('late'):
                pytest.fail('cancelled initialization must not submit a turn')
        with patch('codex_bridge.codex.codex_runtime.AsyncCodex', DelayedClient):
            task = asyncio.create_task(run())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not await Codex.force_close('late')
            release.set()
            await Codex._initializers['late']
            assert await Codex.force_close('late')
            process.terminate.assert_called_once()

    async def test_interrupted_and_unexpected_eof_never_emit_done(self) -> None:
        from codex_bridge.codex import codex_execution as codex
        from openai_codex.types import TurnStatus
        for final_status in (TurnStatus.interrupted, None):

            async def events():
                if final_status is not None:
                    yield NS(method='turn/completed', payload=NS(turn=NS(status=final_status, duration_ms=1)))
            handle = NS(id='turn', stream=events)
            thread = NS(id='thread', turn=AsyncMock(return_value=handle))
            with patch.object(codex_runtime, 'AsyncCodex') as factory, patch.object(session_history, 'register_session', AsyncMock()), patch.object(session_codex, '_schedule_session_title'), patch.object(session_codex, '_sync_session_time', AsyncMock()), patch.object(thread_manager, 'get_user_thread', AsyncMock(return_value=None)), patch.object(thread_manager, 'set_user_thread', AsyncMock()), patch.object(codex_execution, 'prepare_workspace', AsyncMock()), patch.object(codex_execution, 'SavingLog', AsyncMock()):
                factory.return_value.__aenter__.return_value.thread_start.return_value = thread
                result = [event async for event in Codex.running('user', 'hello', 'result')]
            assert [event['type'] for event in result] == ['interrupted'] if final_status else ['error']

class GatewayWatchdogTests:

    def setup_method(self) -> None:
        helpers.StopTests.setup_method(self)
        self._pytest_stack.enter_context(patch.dict(settings(), maxRunSeconds=0.06, startupTimeoutSeconds=0.04, checkIntervalSeconds=0.005, cleanupTimeoutSeconds=0.1))
        self.runtime.codex.completed_status = AsyncMock(return_value=None)
        self.cards = []

        async def card(union_id, content, *, session=None):
            if isinstance(content, str):
                self.cards.append(content)
            else:
                try:
                    async for chunk in content:
                        pass
                except self.card_module.CardStreamStopped:
                    pass
        self.runtime.send_card.side_effect = card

    def batch(self):
        return helpers.batch_from_chat_history(helpers.event('hello', message_id='m1'), [])

    async def test_terminal_stream_hang_releases_without_changing_model_result(self) -> None:
        self._pytest_stack.enter_context(patch.dict(settings(), finalizationTimeoutSeconds=0.03))
        captured = []

        async def running(**kwargs):
            state = self.runtime.cache.all_runs[kwargs['run_id']]
            captured.append(state)
            yield {'type': 'started'}
            state.probe.finish('completed')
            await asyncio.Event().wait()
        self.runtime.codex.running = running
        await asyncio.wait_for(self.execution._handle_message_batch(self.batch(), 0), 1)
        state = captured[0]
        assert state.probe.terminal == 'completed'
        assert state.probe.cleanup_timed_out
        assert state.finished.is_set()
        assert not self.runtime.cache.active_runs_by_chat
        assert not self.runtime.cache.all_runs
        self.runtime.codex.force_close.assert_awaited_once()
        assert state.chat_id not in self.runtime.cache.blocked_chats
        assert CONFIG['messages']['cleanupTimeout'] in self.cards

        async def healthy(**kwargs):
            yield {'type': 'started'}
            yield {'type': 'done'}
        self.runtime.codex.running = healthy
        following = helpers.batch_from_chat_history(helpers.event('Next message', message_id='next'), [])
        await asyncio.wait_for(self.execution._handle_message_batch(following, 0), 1)
        assert not self.runtime.cache.all_runs

    async def test_cancel_resistant_delivery_is_quarantined_and_waiters_wake(self) -> None:
        self._pytest_stack.enter_context(patch.dict(settings(), finalizationTimeoutSeconds=0.03))
        release = asyncio.Event()
        captured = []

        async def card(union_id, content, *, session=None):
            if isinstance(content, str):
                return
            captured.append(next(iter(self.runtime.cache.all_runs.values())))
            async for _ in content:
                pass
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
        self.runtime.send_card.side_effect = card
        worker = None
        try:
            await asyncio.wait_for(self.execution._handle_message_batch(self.batch(), 0), 1)
            state = captured[0]
            worker = state.task
            assert state.finished.is_set()
            assert state.detached
            assert self.runtime.cache.blocked_chats[state.chat_id] is state
            assert not self.runtime.cache.active_runs_by_chat
            await self.commands.processing_stop(helpers.event())
            assert state.chat_id in self.runtime.cache.blocked_chats
        finally:
            release.set()
            if worker is not None:
                await asyncio.wait_for(worker, 1)

    async def test_stop_confirmation_hang_cannot_disable_cleanup_deadline(self) -> None:
        self._pytest_stack.enter_context(patch.dict(settings(), finalizationTimeoutSeconds=0.03))

        async def running(**kwargs):
            yield {'type': 'started'}
            await asyncio.Event().wait()

        async def confirm(run_id):
            await asyncio.Event().wait()
        self.runtime.codex.running = running
        self.runtime.codex.interrupt_and_confirm.side_effect = confirm
        await asyncio.wait_for(self.execution._handle_message_batch(self.batch(), 0), 1)
        assert not self.runtime.cache.active_runs_by_chat
        assert not self.runtime.cache.all_runs

    async def test_force_close_hang_is_bounded_and_release_is_idempotent(self) -> None:
        self._pytest_stack.enter_context(patch.dict(settings(), finalizationTimeoutSeconds=0.02, cleanupTimeoutSeconds=0.02))
        captured = []

        async def running(**kwargs):
            state = self.runtime.cache.all_runs[kwargs['run_id']]
            captured.append(state)
            yield {'type': 'started'}
            state.probe.finish('completed')
            await asyncio.Event().wait()

        async def close(run_id):
            await asyncio.Event().wait()
        self.runtime.codex.running = running
        self.runtime.codex.force_close.side_effect = close
        await asyncio.wait_for(self.execution._handle_message_batch(self.batch(), 0), 1)
        state = captured[0]
        self.runtime._release_run(state)
        assert state.finished.is_set()
        assert self.runtime.cache.blocked_chats[state.chat_id] is state

    async def test_silent_start_and_silent_stream_are_stopped_and_released(self) -> None:
        for started in (False, True):
            self.runtime.cache.processed_message_ids.clear()
            self.cards.clear()

            async def running(**kwargs):
                if started:
                    yield {'type': 'started'}
                await asyncio.Event().wait()
            self.runtime.codex.running = running
            await asyncio.wait_for(self.execution._handle_message_batch(self.batch(), 0), 1)
            assert self.cards == [CONFIG['messages']['taskTimeout']]
            assert not self.runtime.cache.active_runs_by_chat
            assert not self.runtime.cache.active_runs_by_message_id
            assert not self.runtime.cache.all_runs
            assert not self.runtime.cache.codex_locks
            assert not self.runtime.cache.received_at

    async def test_card_wait_does_not_disable_watchdog(self) -> None:
        entered = asyncio.Event()
        original = self.runtime.send_card.side_effect

        async def card(union_id, content, *, session=None):
            if isinstance(content, str):
                await original(union_id, content)
            else:
                entered.set()
                await asyncio.Event().wait()
        self.runtime.send_card.side_effect = card
        await asyncio.wait_for(self.execution._handle_message_batch(self.batch(), 0), 1)
        assert entered.is_set()
        assert self.cards == [CONFIG['messages']['taskTimeout']]

    async def test_stop_card_waits_for_confirmation_and_deduplicates(self) -> None:
        state = helpers.StopTests.state(self)
        release = asyncio.Event()

        async def confirm(run_id):
            await release.wait()
            return True
        self.runtime.codex.interrupt_and_confirm.side_effect = confirm
        first = asyncio.create_task(self.commands.processing_stop(helpers.event()))
        second = asyncio.create_task(self.commands.processing_stop(helpers.event(message_id='stop2')))
        await asyncio.sleep(0)
        assert not self.cards
        release.set()
        await asyncio.gather(first, second)
        assert self.cards == [CONFIG['messages']['stopRequested']]
        self.runtime.codex.interrupt_and_confirm.assert_awaited_once_with(state.run_id)

    async def test_unconfirmed_stop_blocks_new_work_and_allows_stop_retry(self) -> None:
        state = helpers.StopTests.state(self)
        self.runtime.codex.interrupt_and_confirm.return_value = False
        await self.commands.processing_stop(helpers.event())
        assert state.chat_id in self.runtime.cache.blocked_chats
        await self.execution._handle_message_batch(self.batch(), 1)
        self.execution.assemble_input.assert_not_awaited()
        self.runtime.codex.interrupt_and_confirm.return_value = True
        await self.commands.processing_stop(helpers.event(message_id='retry'))
        assert not self.runtime.cache.blocked_chats

    async def test_completed_run_during_card_drain_is_not_failed(self) -> None:
        self.runtime.codex.completed_status.return_value = 'completed'

        async def card(union_id, content, *, session=None):
            if not isinstance(content, str):
                await asyncio.sleep(0.09)
        self.runtime.send_card.side_effect = card
        await asyncio.wait_for(self.execution._handle_message_batch(self.batch(), 0), 1)
        self.runtime.codex.interrupt_and_confirm.assert_not_awaited()
        assert not self.cards

    async def test_delivery_failure_does_not_mark_model_failed_or_interrupt_it(self) -> None:

        async def card(union_id, content, *, session=None):
            async for chunk in content:
                pass
            raise self.card_module.CardDeliveryError('delivery unavailable')
        self.runtime.send_card.side_effect = card
        await asyncio.wait_for(self.execution._handle_message_batch(self.batch(), 0), 1)
        self.runtime.codex.interrupt_and_confirm.assert_not_awaited()
        assert not self.runtime.cache.all_runs

    async def test_old_history_does_not_backdate_new_run(self) -> None:
        self.runtime.cache.processed_message_ids['chat-1'] = {'old': None}
        self.runtime.cache.received_at['old'] = 0
        batch = helpers.batch_from_chat_history(helpers.event('hello', message_id='m1'), [helpers.history('hello', 'm1'), helpers.history('previous', 'old')])
        await asyncio.wait_for(self.execution._handle_message_batch(batch, 0), 1)
        self.runtime.codex.interrupt_and_confirm.assert_not_awaited()
        assert not self.cards

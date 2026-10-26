"""Verify model routing at thread submission without model or network calls."""
from __future__ import annotations
from types import SimpleNamespace as NS
from pathlib import Path
from unittest.mock import AsyncMock, patch
from openai_codex import LocalImageInput, MentionInput, TextInput
from openai_codex.types import TurnStatus
from codex_bridge.codex import codex_execution, codex_runtime, thread_manager
from codex_bridge.session import session_codex, session_history
from codex_bridge.codex import codex_execution as codex
from codex_bridge.configs.loader import CONFIG

class ModelRoutingTests:

    def setup_method(self) -> None:
        self._pytest_stack.enter_context(patch.object(session_history, 'register_session', AsyncMock()))
        self._pytest_stack.enter_context(patch.object(session_codex, '_schedule_session_title'))
        self._pytest_stack.enter_context(patch.object(session_codex, '_sync_session_time', AsyncMock()))

    async def test_unarchived_thread_reapplies_workspace_environment(self) -> None:

        async def stream():
            yield NS(method='turn/completed', payload=NS(turn=NS(status=TurnStatus.completed, duration_ms=1)))
        thread = NS(id='archived', turn=AsyncMock(return_value=NS(id='turn', stream=stream)))
        with patch.object(thread_manager, 'get_user_thread', AsyncMock(return_value='archived')), patch.object(thread_manager, 'set_user_thread', AsyncMock()), patch.object(codex_execution, 'prepare_workspace', AsyncMock()), patch.object(codex_execution, 'save_log', AsyncMock()), patch.object(codex_runtime, 'AsyncCodex') as factory:
            client = factory.return_value.__aenter__.return_value
            client.thread_resume.side_effect = [RuntimeError('archived'), thread]
            client.thread_unarchive.return_value = thread
            events = [event async for event in codex.Codex.running('user', 'hello')]
            assert events == [{'type': 'done'}]
            assert client.thread_resume.await_count == 2
            assert client.thread_resume.await_args_list[0].kwargs['config'] == client.thread_resume.await_args_list[1].kwargs['config']
            client.thread_start.assert_not_awaited()

    async def test_text_and_attachment_routes_for_new_and_resumed_threads(self) -> None:
        routes = {key: {'model': key + '-model', 'provider': key + '-provider'} for key in ('text', 'image', 'multimodal')}
        texts = [TextInput(text='first'), TextInput(text='second')]
        image = LocalImageInput(path='/tmp/image.png')
        document = MentionInput(name='report.pdf', path='/tmp/report.pdf')
        cases = [('hello', 'text'), ('', 'text'), ([], 'multimodal'), ([image], 'image'), ([document], 'multimodal'), ([texts[0]], 'text'), (texts, 'text'), ([*texts, image], 'image'), ([*texts, document], 'multimodal'), ([*texts, document, image], 'image')]

        async def stream():
            yield NS(method='turn/completed', payload=NS(turn=NS(status=TurnStatus.completed, duration_ms=1)))
        for existing in (None, 'thread'):
            for prompt, route in cases:
                handle = NS(id='turn', stream=stream)
                thread = NS(id='thread', turn=AsyncMock(return_value=handle))
                with patch.dict(CONFIG['codex']['models'], routes), patch.object(thread_manager, 'get_user_thread', AsyncMock(return_value=existing)), patch.object(thread_manager, 'set_user_thread', AsyncMock()), patch.object(codex_execution, 'prepare_workspace', AsyncMock()), patch.object(codex_execution, 'save_log', AsyncMock()), patch.object(codex_runtime, 'AsyncCodex') as factory:
                    client = factory.return_value.__aenter__.return_value
                    client.thread_start.return_value = thread
                    client.thread_resume.return_value = thread
                    events = [event async for event in codex.Codex.running('user', prompt)]
                    submit = client.thread_resume if existing else client.thread_start
                    assert submit.await_args.kwargs['model'] == routes[route]['model']
                    assert submit.await_args.kwargs['model_provider'] == routes[route]['provider']
                    environment = submit.await_args.kwargs['config']
                    workspace = (Path(CONFIG['storage']['workspaceRoot']) / 'user').resolve()
                    assert environment['shell_environment_policy.set.VIRTUAL_ENV'] == str(workspace / '.venv')
                    assert environment['shell_environment_policy.set.PATH'].startswith(str(workspace / '.venv/bin'))
                    thread.turn.assert_awaited_once_with(input=prompt)
                    assert events == [{'type': 'done'}]

from __future__ import annotations
import pytest
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType
from unittest.mock import AsyncMock, Mock, patch
from codex_bridge.configs.loader import CONFIG

def load_audio():
    spec = importlib.util.spec_from_file_location('audio_timeout_test_module', Path(__file__).resolve().parents[1] / 'middleware/audio_transcription.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
audio = load_audio()

class AudioTimeoutTests:

    async def test_cancel_reaps_real_child(self) -> None:
        real_spawn = asyncio.create_subprocess_exec
        children = []
        spawned = asyncio.Event()

        async def spawn(*a, **kw):
            child = await real_spawn(*a, **kw)
            children.append(child)
            spawned.set()
            return child
        with patch.object(audio.asyncio, 'create_subprocess_exec', side_effect=spawn):
            task = asyncio.create_task(audio.ASR._run([sys.executable, '-c', 'import time; time.sleep(60)'], 'test'))
            await asyncio.wait_for(spawned.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
        assert children[0].returncode is not None

    async def test_cancel_during_creation_reaps_late_child(self) -> None:
        real_spawn = asyncio.create_subprocess_exec
        creating, release = (asyncio.Event(), asyncio.Event())
        children = []

        async def spawn(*a, **kw):
            creating.set()
            await release.wait()
            child = await real_spawn(*a, **kw)
            children.append(child)
            return child
        with patch.object(audio.asyncio, 'create_subprocess_exec', side_effect=spawn):
            task = asyncio.create_task(audio.ASR._run([sys.executable, '-c', 'import time; time.sleep(60)'], 'test'))
            await creating.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
        assert children[0].returncode is not None

    async def test_uncooperative_child_is_killed(self) -> None:
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready",flush=True); time.sleep(60)', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(child.stdout.readline(), 1)
            await asyncio.wait_for(audio.ASR._reap(child), 4)
            assert child.returncode == -9
        finally:
            if child.returncode is None:
                child.kill()
                await child.communicate()

    async def test_conversion_budget_is_shared_and_cleans_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.ogg'
            source.write_bytes(b'audio')
            converted = Path(directory) / 'converted'
            converted.mkdir()
            outputs = []

            async def convert(_source, output, **kwargs):
                outputs.append(output)
                await asyncio.sleep(0.06)
                output.write_bytes(b'converted')
            with patch.dict(audio.os.environ, {CONFIG['audio']['asr']['apiKeyEnv']: 'fake'}), patch.dict(audio.AUDIO_CONFIG['limits'], conversionTimeoutSeconds=0.1), patch.object(audio, 'MAX_AUDIO_DURATION_SECONDS', 1), patch.object(audio.ASR, '_probe_duration', AsyncMock(return_value=3)), patch.object(audio.ASR, '_convert_audio', side_effect=convert), patch.object(audio.tempfile, 'mkdtemp', return_value=str(converted)), patch.object(audio, 'AsyncOpenAI') as api:
                with pytest.raises(audio.AudioConversionTimeout) as error:
                    await audio.ASR().transfer(source)
                assert 'Audio conversion failed' in str(error.value)
                api.assert_not_called()
                assert len(outputs) == 2
                assert not converted.exists()
                assert not source.exists()

    async def test_success_and_nonzero_exit(self) -> None:
        result = await audio.ASR._run([sys.executable, '-c', 'print("ok")'], 'test')
        assert result.stdout.strip() == 'ok'
        with pytest.raises(audio.AudioProcessingError):
            await audio.ASR._run([sys.executable, '-c', 'raise SystemExit(2)'], 'test')

    async def test_timeout_notice_is_not_model_input(self) -> None:
        from codex_bridge.middleware.message_collector import MessageBatch, CollectedMessage
        tools = ModuleType('codex_bridge.services.lark.lark_tools')
        tools.download_msg_resource = AsyncMock(return_value='/tmp/voice.ogg')
        spec = importlib.util.spec_from_file_location('codex_bridge.middleware.assembly_audio_test', Path(__file__).resolve().parents[1] / 'middleware/message_assemble.py')
        assembly = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'codex_bridge.services.lark.lark_tools': tools, 'codex_bridge.middleware.audio_transcription': audio, spec.name: assembly}):
            spec.loader.exec_module(assembly)
        batch = MessageBatch('chat', 'user', 'p2p', (CollectedMessage('m1', 'audio', {'file_key': 'voice'}, 1), CollectedMessage('m2', 'text', {'text': 'summarize'}, 2)))
        with patch.object(audio.ASR, 'transfer', AsyncMock(side_effect=audio.AudioConversionTimeout('Audio conversion failed: exceeded 9 minutes'))):
            result = await assembly.assemble_codex_input(batch)
        assert result.codex_input is None
        assert 'exceeded 9 minutes' in ' '.join(result.notices)

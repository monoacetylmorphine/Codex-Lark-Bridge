"""Audio normalization boundaries and API cleanup, without ffmpeg or network."""
from __future__ import annotations
import base64, pytest
import asyncio, pytest
import json, pytest
from pathlib import Path
import tempfile, pytest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from codex_bridge.middleware import audio_transcription as audio

class AudioTranscriptionTests:

    def setup_method(self) -> None:
        self.directory = Path(self._pytest_stack.enter_context(tempfile.TemporaryDirectory()))
        self.source = self.directory / 'voice.ogg'
        self.source.write_bytes(b'audio bytes')

    async def test_probe_accepts_numeric_duration_and_rejects_invalid_responses(self) -> None:
        run = self._pytest_stack.enter_context(patch.object(audio.ASR, '_run', new_callable=AsyncMock))
        run.return_value = NS(stdout='{"format":{"duration":"12.5"}}')
        assert await audio.ASR._probe_duration(self.source) == 12.5
        for raw in ('{', '{}', '{"format":null}', '{"format":{"duration":"bad"}}', *[json.dumps({'format': {'duration': value}}) for value in (0, -1, 'NaN', 'Infinity')]):
            with pytest.raises(audio.AudioProcessingError):
                run.return_value = NS(stdout=raw)
                await audio.ASR._probe_duration(self.source)

    async def test_conversion_options_preserve_paths_and_segment_bounds(self) -> None:
        output = self.directory / 'output with spaces.m4a'
        with patch.object(audio.ASR, '_run', new_callable=AsyncMock) as run:
            await audio.ASR._convert_audio(self.source, output, start=0, duration=2.5)
        args = run.await_args.args[0]
        assert args[args.index('-ss') + 1] == '0'
        assert args[args.index('-t') + 1] == '2.5'
        assert args[args.index('-i') + 1] == str(self.source)
        assert args[-1] == str(output)
        assert args[args.index('-ar') + 1] == str(audio.AUDIO_CONFIG['output']['sampleRateHz'])
        assert args[args.index('-ac') + 1] == str(audio.AUDIO_CONFIG['output']['channels'])

    async def test_conversion_can_disable_faststart_and_segment_flags(self) -> None:
        with patch.dict(audio.AUDIO_CONFIG['output'], fastStart=False), patch.object(audio.ASR, '_run', new_callable=AsyncMock) as run:
            await audio.ASR._convert_audio(self.source, self.directory / 'out.m4a')
        args = run.await_args.args[0]
        for flag in ('-ss', '-t', '-movflags'):
            assert flag not in args

    def limits(self, duration=10, target=10, maximum=10):
        self._pytest_stack.enter_context(patch.object(audio, 'MAX_AUDIO_DURATION_SECONDS', duration))
        self._pytest_stack.enter_context(patch.object(audio, 'TARGET_AUDIO_BYTES', target))
        self._pytest_stack.enter_context(patch.object(audio, 'MAX_AUDIO_BYTES', maximum))
        self._pytest_stack.enter_context(patch.object(audio, 'TARGET_AUDIO_BITRATE', 8))
        self._pytest_stack.enter_context(patch.dict(audio.AUDIO_CONFIG['limits'], minimumSegmentSeconds=1))

    async def test_exact_duration_and_size_limits_accept_one_normalized_file(self) -> None:
        self.limits()

        async def convert(source, output, **kwargs):
            output.write_bytes(b'x' * 10)
        with patch.object(audio.ASR, '_probe_duration', AsyncMock(return_value=10)), patch.object(audio.ASR, '_convert_audio', side_effect=convert) as conversion:
            segments = await audio.ASR._prepare_segments(self.source, self.directory)
        assert [p.name for p in segments] == ['normalized.' + audio.AUDIO_CONFIG['output']['container']]
        conversion.assert_awaited_once()
        assert segments[0].stat().st_size == 10

    async def test_duration_and_byte_limits_split_without_gaps_or_overlaps(self) -> None:
        for max_duration, target in ((4, 100), (100, 4)):
            self.limits(duration=max_duration, target=target, maximum=100)
            calls = []

            async def convert(source, output, **kwargs):
                calls.append(kwargs)
                output.write_bytes(b'x')
            with patch.object(audio.ASR, '_probe_duration', AsyncMock(return_value=9)), patch.object(audio.ASR, '_convert_audio', side_effect=convert):
                segments = await audio.ASR._prepare_segments(self.source, self.directory)
            assert calls == [{'start': 0.0, 'duration': 4}, {'start': 4.0, 'duration': 4}, {'start': 8.0, 'duration': 1}]
            assert len(segments) == 3
            assert all((path.is_file() for path in segments))

    async def test_oversized_output_retries_same_offset_with_smaller_segments(self) -> None:
        self.limits(duration=4, target=100, maximum=4)
        calls = []

        async def convert(source, output, **kwargs):
            calls.append(kwargs)
            output.write_bytes(b'x' * (5 if kwargs['duration'] > 2 else 2))
        with patch.object(audio.ASR, '_probe_duration', AsyncMock(return_value=5)), patch.object(audio.ASR, '_convert_audio', side_effect=convert):
            segments = await audio.ASR._prepare_segments(self.source, self.directory)
        assert calls == [{'start': 0.0, 'duration': 4}, {'start': 0.0, 'duration': 2}, {'start': 2.0, 'duration': 2}, {'start': 4.0, 'duration': 1}]
        assert len(segments) == 3
        assert all((p.stat().st_size <= 4 for p in segments))

    async def test_oversized_normalized_file_is_removed_before_segmentation(self) -> None:
        self.limits()

        async def convert(source, output, **kwargs):
            output.write_bytes(b'x' * (11 if not kwargs else 1))
        with patch.object(audio.ASR, '_probe_duration', AsyncMock(return_value=5)), patch.object(audio.ASR, '_convert_audio', side_effect=convert):
            segments = await audio.ASR._prepare_segments(self.source, self.directory)
        assert len(segments) == 1
        assert segments[0].name.startswith('segment-')
        assert not (self.directory / ('normalized.' + audio.AUDIO_CONFIG['output']['container'])).exists()

    async def test_unsplittable_output_fails_and_removes_rejected_piece(self) -> None:
        self.limits(duration=1, target=100, maximum=1)
        outputs = []

        async def convert(source, output, **kwargs):
            outputs.append(output)
            output.write_bytes(b'xx')
        with patch.object(audio.ASR, '_probe_duration', AsyncMock(return_value=2)), patch.object(audio.ASR, '_convert_audio', side_effect=convert), pytest.raises(audio.AudioProcessingError):
            await audio.ASR._prepare_segments(self.source, self.directory)
        assert len(outputs) == 1
        assert not outputs[0].exists()

    def test_response_extraction_ignores_nonmessage_and_nontext_items(self) -> None:
        response = NS(output=[NS(type='reasoning'), NS(type='message', content=[NS(type='other'), NS(type='output_text', text='  Hello, café\n')])])
        text = audio.ASR._extract_text(response)
        assert text == 'Hello, café'
        for response in (NS(), NS(output=[]), NS(output=[NS(type='message', content=[])])):
            with pytest.raises(audio.AudioProcessingError):
                audio.ASR._extract_text(response)

    async def test_segment_api_request_contains_original_audio_bytes(self) -> None:
        client = NS(responses=NS(create=AsyncMock(return_value=NS(output=[NS(type='message', content=[NS(type='output_text', text='recognized')])]))))
        assert await audio.ASR()._transcribe_segment(client, self.source) == 'recognized'
        request = client.responses.create.await_args.kwargs
        assert request['model'] == audio.AUDIO_CONFIG['asr']['model']
        url = request['input'][0]['content'][0]['audio_url']
        assert base64.b64decode(url.split(',', 1)[1]) == self.source.read_bytes()

    async def test_missing_source_and_api_key_fail_before_temporary_directory(self) -> None:
        with patch.object(audio.tempfile, 'mkdtemp') as mkdir, patch.object(audio, 'AsyncOpenAI') as api:
            with pytest.raises(FileNotFoundError):
                await audio.ASR().transfer(self.directory / 'missing')
            with patch.dict(audio.os.environ, {audio.AUDIO_CONFIG['asr']['apiKeyEnv']: ''}):
                with pytest.raises(audio.AudioProcessingError):
                    await audio.ASR().transfer(self.source)
        mkdir.assert_not_called()
        api.assert_not_called()
        assert not self.source.exists()

    async def test_transfer_keeps_order_skips_empty_segments_and_cleans_up(self) -> None:
        await self.check_transfer_cleanup(None)

    async def test_transfer_cleans_up_after_api_failure(self) -> None:
        await self.check_transfer_cleanup(RuntimeError('ASR offline'))

    async def test_transfer_preserves_non_ogg_originals_on_success_and_failure(self) -> None:
        for suffix in ('.m4a', '.mp3', '.wav'):
            for failure in (None, RuntimeError('ASR offline')):
                self.source = self.directory / ('upload' + suffix)
                self.source.write_bytes(b'original audio')
                await self.check_transfer_cleanup(failure)
                assert self.source.read_bytes() == b'original audio'

    async def test_transfer_deletes_uppercase_ogg_and_empty_transcription(self) -> None:
        self.source = self.directory / 'upload.OGG'
        self.source.write_bytes(b'audio')
        await self.check_transfer_cleanup(None, texts=['', '', ''])

    async def test_transfer_deletes_ogg_after_cancellation(self) -> None:
        await self.check_transfer_cleanup(asyncio.CancelledError())

    async def test_cleanup_failure_does_not_mask_result_or_asr_error(self) -> None:
        for failure in (None, RuntimeError('ASR offline')):
            with patch.object(Path, 'unlink', side_effect=PermissionError('cannot delete')), patch.object(audio.logger, 'warning') as warning:
                await self.check_transfer_cleanup(failure, deletion_failed=True)
                warning.assert_called_once()

    async def test_conversion_and_temporary_directory_failures_delete_ogg(self) -> None:
        for stage in ('directory', 'conversion'):
            self.source.write_bytes(b'audio')
            failing_stage = patch.object(audio.tempfile, 'mkdtemp', side_effect=OSError('disk error')) if stage == 'directory' else patch.object(audio.ASR, '_prepare_segments', AsyncMock(side_effect=audio.AudioProcessingError('invalid audio')))
            with failing_stage, patch.dict(audio.os.environ, {audio.AUDIO_CONFIG['asr']['apiKeyEnv']: 'test-key'}):
                with pytest.raises((OSError, audio.AudioProcessingError)):
                    await audio.ASR().transfer(self.source)
            assert not self.source.exists()

    async def check_transfer_cleanup(self, failure, *, texts=None, deletion_failed=False):
        converted = self.directory / 'converted'
        converted.mkdir()
        segments = [converted / f'{i}.m4a' for i in range(3)]
        for segment in segments:
            segment.write_bytes(b'audio')
        with patch.dict(audio.os.environ, {audio.AUDIO_CONFIG['asr']['apiKeyEnv']: 'test-key'}), patch.object(audio.tempfile, 'mkdtemp', return_value=str(converted)), patch.object(audio.ASR, '_prepare_segments', AsyncMock(return_value=segments)), patch.object(audio.ASR, '_transcribe_segment', AsyncMock(side_effect=failure if failure is not None else texts or ['first', '', 'last'])) as transcribe, patch.object(audio, 'AsyncOpenAI') as factory:
            if failure:
                with pytest.raises(type(failure)):
                    await audio.ASR().transfer(self.source)
            else:
                assert await audio.ASR().transfer(self.source) == ('' if texts is not None else 'first\nlast')
                assert [call.args[1] for call in transcribe.await_args_list] == segments
            factory.return_value.__aexit__.assert_awaited_once()
        assert not converted.exists()
        assert self.source.exists() == (deletion_failed or self.source.suffix.lower() != '.ogg')

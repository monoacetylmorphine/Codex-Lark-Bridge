"""Convert and transcribe downloaded audio before submitting it to Codex."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from bridge.configs.loader import CONFIG
from bridge.utils.terminal_log import get_logger

logger = get_logger("Audio")


AUDIO_CONFIG: dict[str, Any] = CONFIG["audio"]
MAX_AUDIO_BYTES = AUDIO_CONFIG["limits"]["maxBytes"]
MAX_AUDIO_DURATION_SECONDS = AUDIO_CONFIG["limits"]["maxDurationSeconds"]
TARGET_AUDIO_BYTES = AUDIO_CONFIG["limits"]["targetBytes"]
TARGET_AUDIO_BITRATE = AUDIO_CONFIG["output"]["bitrateBps"]


class AudioProcessingError(RuntimeError):
    """Audio conversion or transcription failure."""


class AudioConversionTimeout(AudioProcessingError):
    """Timeout of the complete audio conversion pipeline."""


class ASR:
    @staticmethod
    def _encode_audio(file_path: Path) -> str:
        """读取音频文件的全部字节并返回 Base64 文本；文件读取异常向外传播。"""
        with file_path.open("rb") as read_file:
            return base64.b64encode(read_file.read()).decode("utf-8")

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        """对尚未退出的音频子进程先 terminate，短暂等待后必要时 kill，并回收进程输出。"""
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.communicate(), timeout=2)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()

    @staticmethod
    async def _run(command: list[str], action: str) -> subprocess.CompletedProcess[str]:
        """执行音频命令并返回解码后的 CompletedProcess，非零退出码转为 AudioProcessingError。

        取消或其他异常时清理已创建或迟到的子进程，完成清理后重新抛出异常；超时由调用方控制。
        """
        # Shield creation so cancellation cannot orphan a late-created child.
        creating = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        ))
        process = None
        try:
            process = await asyncio.shield(creating)
            stdout, stderr = await process.communicate()
        except BaseException:
            cleaning = asyncio.create_task(ASR._cleanup_process(process, creating))
            # Cleanup owns the process even if another stop arrives meanwhile.
            while not cleaning.done():
                try:
                    await asyncio.shield(cleaning)
                except asyncio.CancelledError:
                    continue
            cleaning.result()
            raise
        result = subprocess.CompletedProcess(command, process.returncode,
                                             stdout.decode(errors="replace"), stderr.decode(errors="replace"))
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] or ["Unknown error"]
            raise AudioProcessingError(f"{action} failed: {detail[0]}")
        return result

    @staticmethod
    async def _cleanup_process(
        process: asyncio.subprocess.Process | None,
        creating: asyncio.Task[asyncio.subprocess.Process],
    ) -> None:
        """取消可能先于进程创建完成，清理任务必须接管迟到的子进程。"""
        child = process
        if child is None:
            try:
                child = await creating
            except Exception:
                return
        await ASR._reap(child)

    @classmethod
    async def _probe_duration(cls, file_path: Path) -> float:
        """调用配置的 ffprobe 获取音频时长，返回正的有限秒数；无效结果抛出 AudioProcessingError。"""
        result = await cls._run(
            [
                AUDIO_CONFIG["tools"]["ffprobe"], "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(file_path),
            ],
            "Read audio duration",
        )
        try:
            duration = float(json.loads(result.stdout)["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AudioProcessingError("Unable to read audio duration") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise AudioProcessingError("Invalid audio duration")
        return duration

    @classmethod
    async def _convert_audio(
        cls,
        input_path: Path,
        output_path: Path,
        *,
        start: float | None = None,
        duration: float | None = None,
    ) -> None:
        """按 audio.output 配置调用 ffmpeg 转换音频，支持可选的起始秒数与片段时长。

        采样率、声道、编码及码率均来自配置；输出路径由调用方指定，已有目标文件允许覆盖。
        """
        command = [AUDIO_CONFIG["tools"]["ffmpeg"], "-v", "error", "-y"]
        if start is not None:
            command.extend(["-ss", str(start)])
        command.extend(["-i", str(input_path)])
        if duration is not None:
            command.extend(["-t", str(duration)])
        command.extend([
            "-vn", "-ac", str(AUDIO_CONFIG["output"]["channels"]),
            "-ar", str(AUDIO_CONFIG["output"]["sampleRateHz"]),
            "-c:a", AUDIO_CONFIG["output"]["codec"],
            "-b:a", str(AUDIO_CONFIG["output"]["bitrateBps"]),
        ])
        if AUDIO_CONFIG["output"]["fastStart"]:
            command.extend(["-movflags", "+faststart"])
        command.append(str(output_path))
        await cls._run(command, "ffmpeg audio conversion")

    @classmethod
    async def _prepare_segments(cls, input_path: Path, directory: Path) -> list[Path]:
        """按配置的容器、码率和时长及字节上限生成音频文件，返回按时间排序的路径列表。

        先尝试整段转换；需要分段时按目标字节数估算时长，实际文件超限则缩短分段重试。
        低于最小分段时长仍无法满足限制时抛出 AudioProcessingError。
        """
        duration = await cls._probe_duration(input_path)
        container = AUDIO_CONFIG["output"]["container"]
        normalized = directory / f"normalized.{container}"
        estimated_bytes = duration * TARGET_AUDIO_BITRATE / 8
        if (
            duration <= MAX_AUDIO_DURATION_SECONDS
            and estimated_bytes <= TARGET_AUDIO_BYTES
        ):
            await cls._convert_audio(input_path, normalized)
            if normalized.stat().st_size <= MAX_AUDIO_BYTES:
                return [normalized]
            normalized.unlink(missing_ok=True)

        bytes_based_seconds = TARGET_AUDIO_BYTES * 8 / TARGET_AUDIO_BITRATE
        segment_seconds = min(MAX_AUDIO_DURATION_SECONDS, bytes_based_seconds)
        segments: list[Path] = []
        start = 0.0
        index = 1
        while start < duration:
            piece_duration = min(segment_seconds, duration - start)
            segment = directory / f"segment-{index:04d}.{container}"
            await cls._convert_audio(
                input_path,
                segment,
                start=start,
                duration=piece_duration,
            )
            if segment.stat().st_size > MAX_AUDIO_BYTES:
                segment.unlink(missing_ok=True)
                segment_seconds /= 2
                if segment_seconds < AUDIO_CONFIG["limits"]["minimumSegmentSeconds"]:
                    raise AudioProcessingError("Unable to split audio into chunks smaller than 25 MB")
                continue
            segments.append(segment)
            start += piece_duration
            index += 1
        return segments

    @staticmethod
    def _extract_text(response: object) -> str:
        """返回响应的 message 项中首个 output_text 文本并去除首尾空白；找不到时抛出 AudioProcessingError。"""
        for item in getattr(response, "output", []):
            if getattr(item, "type", None) != "message":
                continue
            for content in getattr(item, "content", []):
                if getattr(content, "type", None) == "output_text":
                    return str(content.text).strip()
        raise AudioProcessingError("No transcription text found in the API response")

    async def _transcribe_segment(self, client: AsyncOpenAI, path: Path) -> str:
        """在线程中编码音频分段，调用配置的 Responses 模型并提取转写文本。"""
        base64_file = await asyncio.to_thread(self._encode_audio, path)
        response = await client.responses.create(
            model=AUDIO_CONFIG["asr"]["model"],
            instructions=AUDIO_CONFIG["asr"]["instructions"],
            input=[{
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "audio_url": (
                            f"data:audio/{AUDIO_CONFIG['output']['container']};base64,"
                            + base64_file
                        ),
                    },
                    {"type": "input_text", "text": AUDIO_CONFIG["asr"]["userPrompt"]},
                ],
            }],
        )
        return self._extract_text(response)

    async def transfer(self, file_path: str | Path) -> str:
        """转换并按需切分音频，逐段请求转写，按原始顺序用换行连接非空文本。

        输入文件不存在时抛出 FileNotFoundError，凭据缺失或转换失败时抛出音频处理异常。
        转换及切分阶段受配置超时限制，该限制不覆盖后续模型请求。进入处理流程后，
        finally 清理临时目录，并在成功、失败或取消时尝试删除原始 OGG 文件；其他格式保留。
        """
        input_path = Path(file_path).expanduser().resolve()
        if not input_path.is_file():
            raise FileNotFoundError(f"Audio file does not exist: {input_path}")

        temporary_directory: Path | None = None
        try:
            api_key_env = AUDIO_CONFIG["asr"]["apiKeyEnv"]
            api_key = os.getenv(api_key_env)
            if not api_key:
                raise AudioProcessingError(f"{api_key_env} is not configured")

            temporary_directory = Path(tempfile.mkdtemp(prefix=AUDIO_CONFIG["asr"]["temporaryDirectoryPrefix"]))
            try:
                async with asyncio.timeout(AUDIO_CONFIG["limits"]["conversionTimeoutSeconds"]):
                    segments = await self._prepare_segments(input_path, temporary_directory)
            except TimeoutError as exc:
                minutes = AUDIO_CONFIG["limits"]["conversionTimeoutSeconds"] / 60
                raise AudioConversionTimeout(
                    CONFIG["messages"]["audioConversionTimeout"].format(minutes=f"{minutes:g}")
                ) from exc
            async with AsyncOpenAI(
                api_key=api_key,
                base_url=AUDIO_CONFIG["asr"]["baseUrl"],
            ) as client:
                logger.debug("Audio transcription started")
                texts = []
                for segment in segments:
                    text = await self._transcribe_segment(client, segment)
                    if text:
                        texts.append(text)
            logger.debug("Audio transcription finished")
            return "\n".join(texts)
        finally:
            if temporary_directory is not None:
                shutil.rmtree(temporary_directory, ignore_errors=True)
            # Clean up by original extension: delete OGG files on success, failure, or cancellation; retain other formats.
            if input_path.suffix.lower() == ".ogg":
                try:
                    input_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to delete the original OGG file: path=%s", input_path, exc_info=True)

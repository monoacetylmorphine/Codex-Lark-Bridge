"""Parse Lark messages and assemble Codex turn input."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openai_codex import LocalImageInput, MentionInput, TextInput

from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger

logger = get_logger("Assembly")

from codex_bridge.middleware.audio_transcription import ASR, AudioConversionTimeout
from codex_bridge.services.lark.lark_tools import download_msg_resource
from codex_bridge.middleware.message_collector import MessageBatch
from codex_bridge.middleware.resource_validator import ResourceValidationError


CodexInputItem = TextInput | LocalImageInput | MentionInput
CodexRunInput = str | list[CodexInputItem]

SUPPORTED_IMAGE_EXTENSIONS = frozenset(CONFIG["resources"]["acceptedExtensions"]["image"])
SUPPORTED_FILE_EXTENSIONS = frozenset(CONFIG["resources"]["acceptedExtensions"]["document"])
AUDIO_EXTENSIONS = frozenset(CONFIG["resources"]["acceptedExtensions"]["audio"])


class InputAssemblyError(Exception):
    """Input error that can be displayed directly to Lark users."""

    def __init__(self, user_message: str) -> None:
        """保存可向用户展示的输入错误文案，并初始化异常消息。"""
        super().__init__(user_message)
        self.user_message = user_message


@dataclass(frozen=True)
class AssemblyResult:
    codex_input: CodexRunInput | None
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class _TextPart:
    text: str


@dataclass(frozen=True)
class _ResourcePart:
    kind: Literal["image", "file", "audio"]
    message_id: str
    resource_key: str
    display_name: str


_InputPart = _TextPart | _ResourcePart


async def assemble_codex_input(batch: MessageBatch) -> AssemblyResult:
    """下载、校验并转写批次资源，按消息顺序组装 Codex 输入及处理提示，不直接提交模型请求。

    部分附件可用时保留可用附件和文本；附件全部不可用时返回 codex_input=None，避免仅提交文本。
    不支持的消息类型、附件数量超限或无有效内容时抛出 InputAssemblyError；单段文本返回字符串。
    """
    if batch.unsupported_message_types:
        message_types = ", ".join(sorted(batch.unsupported_message_types))
        raise InputAssemblyError(
            f"Message types {message_types} are not supported. Send text, images, rich text, files, or audio instead."
        )

    parts, parse_rejections = _normalize_messages(batch)
    resource_parts = [part for part in parts if isinstance(part, _ResourcePart)]
    limit = CONFIG["messaging"]["historyPageSize"]
    if len(resource_parts) + len(parse_rejections) > limit:
        # Per user requirement, history pagination and the per-batch attachment limit share one parameter.
        # Reject the entire batch when the limit is exceeded to avoid silent truncation that mismatches the task description and attachments.
        raise InputAssemblyError(f"Each batch accepts at most {limit} attachments in total, including images, files, and audio. Please send separate batches.")
    downloaded, download_rejections = await _download_resources(
        batch.union_id,
        resource_parts,
    )

    accepted: dict[_ResourcePart, Path] = {}
    format_rejections: list[str] = []
    for part, path in downloaded.items():
        if _is_supported(part, path):
            accepted[part] = path
        else:
            format_rejections.append(path.name or part.display_name)

    transcriptions, transcription_rejections = await _transcribe_audio(accepted)
    for part, path in list(accepted.items()):
        if part.kind == "audio" or path.suffix.lower() in AUDIO_EXTENSIONS:
            accepted.pop(part)

    rejected_names = _unique(
        [
            *parse_rejections,
            *download_rejections,
            *format_rejections,
            *transcription_rejections,
        ]
    )
    resource_count = len(resource_parts) + len(parse_rejections)

    # If a message had attachments but none can be used, its accompanying task
    # text must not be sent alone because that would change the user's request.
    if resource_count and not accepted and not transcriptions:
        names = ", ".join(rejected_names) or "the submitted attachments"
        return AssemblyResult(
            codex_input=None,
            notices=(
                f"Failed to process these attachments: {names}. Neither attachments nor the text task were submitted. Resolve the issue and resend them with the task description.",
            ),
        )

    items = _build_input_items(parts, accepted, transcriptions)
    if not items:
        raise InputAssemblyError(CONFIG["messages"]["emptyInput"])

    notices: tuple[str, ...] = ()
    if rejected_names:
        names = ", ".join(rejected_names)
        notices = (
            f"These attachments cannot be used as input and were skipped: {names}. Convert their formats and resend them. "
            "The remaining supported content will continue to be processed.",
        )

    if len(items) == 1 and isinstance(items[0], TextInput):
        return AssemblyResult(codex_input=items[0].text, notices=notices)
    return AssemblyResult(codex_input=items, notices=notices)


def _normalize_messages(batch: MessageBatch) -> tuple[list[_InputPart], list[str]]:
    """按 sequence 将文本、图片、文件、音频及富文本拆为输入片段，返回片段与解析拒绝说明。"""
    parts: list[_InputPart] = []
    rejected: list[str] = []

    for message in sorted(batch.messages, key=lambda item: item.sequence):
        content = message.content

        if message.message_type == "text":
            _append_text(parts, content.get("text"))

        elif message.message_type == "image":
            _append_resource(
                parts,
                rejected,
                kind="image",
                message_id=message.message_id,
                resource_key=content.get("image_key"),
                display_name=f"Image {message.message_id}",
            )

        elif message.message_type == "file":
            _append_resource(
                parts,
                rejected,
                kind="file",
                message_id=message.message_id,
                resource_key=content.get("file_key"),
                display_name=content.get("file_name") or f"File {message.message_id}",
            )

        elif message.message_type == "audio":
            # Lark audio payloads contain file_key and duration. Audio is sent
            # immediately (without the image/file collection window). It is
            # downloaded now and transcribed before Codex input is assembled.
            _append_resource(
                parts,
                rejected,
                kind="audio",
                message_id=message.message_id,
                resource_key=content.get("file_key"),
                display_name=content.get("file_name") or f"Audio {message.message_id}",
            )

        elif message.message_type == "post":
            _append_text(parts, content.get("title"))
            rows = content.get("content_v2") or content.get("content") or []
            for row in rows:
                if not isinstance(row, list):
                    continue
                for node in row:
                    if not isinstance(node, dict):
                        continue
                    if node.get("tag") == "text":
                        _append_text(parts, node.get("text"))
                    elif node.get("tag") == "img":
                        _append_resource(
                            parts,
                            rejected,
                            kind="image",
                            message_id=message.message_id,
                            resource_key=node.get("image_key"),
                            display_name=f"Rich-text image {message.message_id}",
                        )
            _append_post_files(parts, rejected, message.message_id, content.get("files"))

    return parts, rejected


def _append_post_files(
    parts: list[_InputPart],
    rejected: list[str],
    message_id: str,
    files: object,
) -> None:
    """校验富文本 files 列表并追加文件片段；无效条目、文件夹或缺少资源标识时追加拒绝说明。"""

    if files is None:
        return
    if not isinstance(files, list):
        rejected.append(f"Rich-text attachment {message_id}: invalid files format")
        return
    for index, item in enumerate(files, start=1):
        fallback_name = f"Rich-text attachment {message_id} item {index}"
        if not isinstance(item, dict):
            rejected.append(f"{fallback_name}: invalid attachment format")
            continue
        name = item.get("file_name")
        display_name = name.strip() if isinstance(name, str) and name.strip() else fallback_name
        is_folder = item.get("is_folder", False)
        if is_folder is True:
            rejected.append(f"{display_name}: folders are not supported. Send files individually")
            continue
        if is_folder is not False:
            rejected.append(f"{display_name}: invalid is_folder format")
            continue
        _append_resource(
            parts, rejected,
            kind="file",
            message_id=message_id,
            resource_key=item.get("file_key"),
            display_name=display_name,
        )


def _append_text(parts: list[_InputPart], value: object) -> None:
    """将非空字符串去除首尾空白后追加为文本片段，忽略其他值。"""
    if isinstance(value, str) and value.strip():
        parts.append(_TextPart(text=value.strip()))


def _append_resource(
    parts: list[_InputPart],
    rejected: list[str],
    *,
    kind: Literal['image', 'file', 'audio'],
    message_id: str,
    resource_key: object,
    display_name: str,
) -> None:
    """为非空字符串资源标识追加资源片段，否则记录缺少资源标识的提示。"""
    if isinstance(resource_key, str) and resource_key:
        parts.append(_ResourcePart(kind, message_id, resource_key, display_name))
    else:
        rejected.append(f"{display_name}{CONFIG['messages']['missingResourceKeySuffix']}")


async def _download_resources(
    union_id: str,
    resources: list[_ResourcePart],
) -> tuple[dict[_ResourcePart, Path], list[str]]:
    """并发下载资源，返回成功资源的绝对路径映射和拒绝说明列表。

    单项下载或校验异常转为对应提示，不丢弃其他成功资源；外层取消仍向调用方传播。
    """
    if not resources:
        return {}, []

    tasks = [
        asyncio.create_task(
            download_msg_resource(
                union_id=union_id,
                message_id=part.message_id,
                resource_key=part.resource_key,
                resource_type="image" if part.kind == "image" else "file",
            )
        )
        for part in resources
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    downloaded: dict[_ResourcePart, Path] = {}
    rejected: list[str] = []
    for part, result in zip(resources, results):
        if isinstance(result, ResourceValidationError):
            rejected.append(f"{part.display_name} ({result})")
        elif isinstance(result, BaseException):
            logger.error("Resource download error: name=%s", part.display_name, exc_info=result)
            rejected.append(f"{part.display_name}{CONFIG['messages']['downloadFailedSuffix']}")
        elif not result:
            rejected.append(f"{part.display_name}{CONFIG['messages']['downloadFailedSuffix']}")
        else:
            downloaded[part] = Path(result).resolve()

    return downloaded, rejected


def _is_supported(part: _ResourcePart, path: Path) -> bool:
    """根据资源类别和扩展名判断是否进入后续处理；语音消息交由音频流程继续校验和转换。"""
    extension = path.suffix.lower()
    if part.kind == "image":
        return extension in SUPPORTED_IMAGE_EXTENSIONS
    if part.kind == "file":
        return extension in SUPPORTED_FILE_EXTENSIONS or extension in AUDIO_EXTENSIONS
    # Lark voice messages are downloaded as OGG and normalized by the audio
    # middleware regardless of the generated file name.
    return True


async def _transcribe_audio(
    accepted: dict[_ResourcePart, Path],
) -> tuple[dict[_ResourcePart, str], list[str]]:
    """并发转写已接受的音频资源，返回非空转写映射及失败说明，保留转换超时的具体提示。"""
    audio_parts = [
        (part, path)
        for part, path in accepted.items()
        if part.kind == "audio" or path.suffix.lower() in AUDIO_EXTENSIONS
    ]
    if not audio_parts:
        return {}, []

    tasks = [asyncio.create_task(ASR().transfer(path)) for _, path in audio_parts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    transcriptions: dict[_ResourcePart, str] = {}
    rejected: list[str] = []
    for (part, _), result in zip(audio_parts, results):
        if isinstance(result, AudioConversionTimeout):
            rejected.append(f"{part.display_name}: {result}")
        elif isinstance(result, BaseException):
            logger.error("Resource transcription error: name=%s", part.display_name, exc_info=result)
            rejected.append(f"{part.display_name}{CONFIG['messages']['transcriptionFailedSuffix']}")
        elif not result.strip():
            rejected.append(f"{part.display_name}{CONFIG['messages']['emptyTranscriptionSuffix']}")
        else:
            transcriptions[part] = result.strip()
    return transcriptions, rejected


def _build_input_items(
    parts: list[_InputPart],
    accepted: dict[_ResourcePart, Path],
    transcriptions: dict[_ResourcePart, str],
) -> list[CodexInputItem]:
    """按片段原始顺序生成 TextInput、LocalImageInput 或 MentionInput，跳过未接受的资源。"""
    items: list[CodexInputItem] = []
    for part in parts:
        if isinstance(part, _TextPart):
            items.append(TextInput(text=part.text))
        elif part in transcriptions:
            items.append(TextInput(text=transcriptions[part]))
        elif part in accepted:
            path = accepted[part]
            if part.kind == "image":
                items.append(LocalImageInput(path=str(path)))
            else:
                items.append(MentionInput(name=path.name, path=str(path)))
    return items


def _unique(values: list[str]) -> list[str]:
    """按首次出现顺序去重字符串列表。"""
    return list(dict.fromkeys(values))

"""Validate resources downloaded from Lark before writing them to disk."""

from __future__ import annotations

import io
import json
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.parse import unquote

from codex_bridge.configs.loader import CONFIG


ResourceType = Literal["image", "file"]

GENERIC_MIME_TYPES: set[str] = {"", "application/octet-stream", "binary/octet-stream"}
IMAGE_FORMATS: set[str] = {"jpeg", "png", "gif", "webp", "bmp"}

TEXT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".csv", ".jsonl", ".py", ".js", ".ts", ".html",
    ".xml", ".yml", ".yaml", ".toml", ".sh", ".rtf",
}
OFFICE_TYPES: dict[str, str] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
OFFICE_MIMES: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}
# Source: initial resource budget for project metadata validation, not an Office format or platform limit.
MAX_OFFICE_METADATA_BYTES = 1024 * 1024

FORMAT_EXTENSIONS: dict[str, str] = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "bmp": ".bmp",
    "pdf": ".pdf",
    "mp4": ".mp4",
    "m4a": ".m4a",
    "zip": ".zip",
    "ole": ".bin",
    "json": ".json",
    "text": ".txt",
    "ogg": ".ogg",
    "opus": ".opus",
    "wav": ".wav",
    "mp3": ".mp3",
}

EXTENSION_FORMATS: dict[str, str] = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".gif": "gif",
    ".webp": "webp",
    ".bmp": "bmp",
    ".pdf": "pdf",
    ".mp4": "mp4",
    ".m4a": "m4a",
    ".zip": "zip",
    ".docx": "zip",
    ".xlsx": "zip",
    ".pptx": "zip",
    ".doc": "ole",
    ".xls": "ole",
    ".ppt": "ole",
    ".json": "json",
    ".txt": "text",
    ".ogg": "ogg",
    ".opus": "opus",
    ".wav": "wav",
    ".mp3": "mp3",
}
EXTENSION_FORMATS.update(dict.fromkeys(TEXT_EXTENSIONS, "text"))

MIME_FORMATS: dict[str, str] = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "application/pdf": "pdf",
    "video/mp4": "mp4",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/msword": "ole",
    "application/vnd.ms-excel": "ole",
    "application/vnd.ms-powerpoint": "ole",
    "application/json": "json",
    "text/json": "json",
    "text/plain": "text",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
}
MIME_FORMATS.update(dict.fromkeys(OFFICE_MIMES, "zip"))
MIME_FORMATS.update(dict.fromkeys(("text/csv", "text/markdown", "text/html", "text/xml",
                                 "application/xml", "application/javascript",
                                 "application/x-ndjson", "application/jsonl"), "text"))

STRICT_FORMATS: set[str] = {
    "jpeg", "png", "gif", "webp", "bmp", "pdf", "mp4", "m4a", "zip", "ole",
    "json", "text", "ogg", "opus", "wav", "mp3",
}


class ResourceValidationError(ValueError):
    """Reject further processing when resource bytes or metadata are untrusted."""


@dataclass(frozen=True)
class ValidatedResource:
    data: bytes
    file_name: str
    mime_type: str
    detected_format: str
    extension: str


def read_resource_bytes(stream: BinaryIO) -> bytes:
    """从支持 getvalue 的 SDK 响应或普通二进制流读取字节；结果不是 bytes 时抛出 ResourceValidationError。"""
    getvalue = getattr(stream, "getvalue", None)
    data = getvalue() if callable(getvalue) else stream.read()
    if not isinstance(data, bytes):
        raise ResourceValidationError("Message resource response is not binary content")
    return data


def validate_downloaded_resource(
    *,
    data: bytes,
    resource_key: str,
    resource_type: ResourceType,
    file_name: str | None,
    headers: Mapping[str, object] | None = None,
) -> ValidatedResource:
    """校验资源字节及响应元数据，返回包含原始字节、安全文件名、MIME、检测格式和扩展名的 ValidatedResource。

    检查长度、格式声明及签名的一致性，并对支持的 Office 容器校验元数据；
    不可信或不匹配的资源抛出 ResourceValidationError。本函数不写磁盘，也不判断模型是否接受该扩展名。
    """
    if resource_type not in {"image", "file"}:
        raise ResourceValidationError(f"Invalid resource type: {resource_type}")
    if not isinstance(data, bytes) or not data:
        raise ResourceValidationError("Downloaded resource is empty")

    normalized_headers = {
        str(key).lower(): str(value)
        for key, value in (headers or {}).items()
        if value is not None
    }
    _validate_content_length(data, normalized_headers.get("content-length"))

    mime_type = _normalize_mime(normalized_headers.get("content-type"))
    safe_original_name = _safe_name(file_name)
    original_extension = Path(safe_original_name).suffix.lower()
    signature_format = _detect_signature(data)
    mime_format = MIME_FORMATS.get(mime_type)
    extension_format = EXTENSION_FORMATS.get(original_extension)
    office_extension = (original_extension if original_extension in OFFICE_TYPES
                        else OFFICE_MIMES.get(mime_type))
    if office_extension:
        if mime_type in OFFICE_MIMES and OFFICE_MIMES[mime_type] != office_extension:
            raise ResourceValidationError("Office extension does not match the MIME type")
        _validate_office(data, office_extension)

    detected_format = _resolve_format(
        data=data,
        resource_type=resource_type,
        signature_format=signature_format,
        mime_format=mime_format,
        extension_format=extension_format,
        mime_type=mime_type,
        original_extension=original_extension,
    )

    if resource_type == "image" and detected_format not in IMAGE_FORMATS:
        raise ResourceValidationError(
            f"Resource is declared as an image, but its detected format is {detected_format or 'unknown'}"
        )

    _validate_mime_consistency(mime_type, mime_format, detected_format)
    extension = _choose_extension(
        detected_format,
        mime_type,
        original_extension,
    )
    if office_extension:
        extension = office_extension
    base_name = Path(safe_original_name).stem if safe_original_name else ""
    if not base_name:
        base_name = _safe_stem(resource_key)
    final_name = f"{base_name}{extension}"

    return ValidatedResource(
        data=data,
        file_name=final_name,
        mime_type=mime_type or "application/octet-stream",
        detected_format=detected_format,
        extension=extension,
    )


def _resolve_format(
    *,
    data: bytes,
    resource_type: ResourceType,
    signature_format: str | None,
    mime_format: str | None,
    extension_format: str | None,
    mime_type: str,
    original_extension: str,
) -> str:
    """综合扩展名、MIME 和字节签名确定格式，并校验文本及 JSON 内容。

    已知二进制格式要求签名匹配；未知普通文件可保留扩展名推断结果，是否支持由上层决定。
    """
    if original_extension in TEXT_EXTENSIONS or original_extension == ".json":
        if signature_format:
            raise ResourceValidationError("Text extension does not match the binary file signature")
        text = _validate_plain_text(data)
        if original_extension == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                raise ResourceValidationError("Invalid JSON content format") from exc
            return "json"
        return "text"
    # Binary signatures are the strongest source of truth.
    if signature_format:
        if signature_format == "ogg" and mime_format == "opus":
            return "opus"
        if signature_format == "mp4" and (
            mime_format == "m4a" or extension_format == "m4a"
        ):
            # M4A is an audio profile of the same ISO BMFF container used by
            # MP4. MIME and the original extension provide the distinction.
            return "m4a"
        return signature_format

    claimed_format = mime_format or extension_format
    if claimed_format == "json":
        try:
            json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResourceValidationError("JSON MIME type or extension does not match the actual content") from exc
        return "json"

    if claimed_format == "text":
        _validate_plain_text(data)
        return "text"

    # Supported binary formats must have their expected signature. This blocks
    # renamed executables or archives from being trusted as documents/media.
    if claimed_format in STRICT_FORMATS:
        raise ResourceValidationError(
            f"{mime_type or original_extension or resource_type} does not match the file signature"
        )

    # Unknown file formats may still be saved under their original extension;
    # the upper assembly layer decides whether Codex supports that extension.
    if resource_type == "file" and original_extension:
        return extension_format or original_extension.removeprefix(".")

    raise ResourceValidationError("Unable to identify the resource format from its MIME type, filename, and file signature")


def _detect_signature(data: bytes) -> str | None:
    """根据已支持的文件头特征返回格式名称，无法识别时返回 None；不验证完整文件内容。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mp4"
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        return "zip"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if data.startswith(b"OggS"):
        return "ogg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"ID3") or (
        len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
    ):
        return "mp3"
    return None


def _validate_mime_consistency(
    mime_type: str,
    mime_format: str | None,
    detected_format: str,
) -> None:
    """检查已知 MIME 与检测格式是否相容，允许通用 MIME 及规定的容器兼容组合，否则抛出校验异常。"""
    if mime_type in GENERIC_MIME_TYPES or mime_format is None:
        return
    compatible = (
        mime_format == detected_format
        or mime_format == "text" and detected_format == "json"
        or {mime_format, detected_format} <= {"ogg", "opus"}
        or {mime_format, detected_format} <= {"mp4", "m4a"}
    )
    if not compatible:
        raise ResourceValidationError(
            f"MIME type {mime_type} does not match file signature {detected_format}"
        )


def _choose_extension(detected_format: str, mime_type: str, original_extension: str) -> str:
    """优先保留与检测格式一致的原扩展名，其次使用格式映射；无可用扩展名时抛出校验异常。"""
    # Preserve the extension supplied by Lark whenever it agrees with the
    # validated content. Only repair the name when metadata and bytes disagree.
    if EXTENSION_FORMATS.get(original_extension) == detected_format:
        return original_extension
    if detected_format in FORMAT_EXTENSIONS:
        return FORMAT_EXTENSIONS[detected_format]
    if original_extension:
        return original_extension
    raise ResourceValidationError(
        f"Unable to determine a safe file extension for {mime_type or detected_format}"
    )


def _validate_plain_text(data: bytes) -> str:
    """拒绝不允许的二进制控制字符并按 UTF-8（可带 BOM）解码，失败时抛出 ResourceValidationError。"""
    # Preserve the existing UTF-8 policy; do not guess encodings or modify the original bytes.
    if any(value < 32 and value not in (9, 10, 12, 13) for value in data):
        raise ResourceValidationError("Text file contains binary control characters")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResourceValidationError("Text file is not valid UTF-8") from exc


def _validate_office(data: bytes, extension: str) -> None:
    """读取 ZIP 目录及有大小限制的元数据 XML，检查主文档关系、部件属性和声明类型。

    拒绝重复部件、异常路径、实体声明及格式不匹配；不解析主文档正文，不向磁盘解压或执行内容。
    底层容器及元数据读取异常转换为 ResourceValidationError。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ResourceValidationError("Office container contains duplicate parts")

            def metadata(name: str) -> ET.Element:
                """有界读取并解析指定元数据 XML，按显式 BOM 支持 UTF-16，拒绝空字节及实体声明。"""
                info = archive.getinfo(name)
                if info.file_size > MAX_OFFICE_METADATA_BYTES:
                    raise ResourceValidationError("Office metadata exceeds the validation size limit")
                with archive.open(info) as stream:
                    raw = stream.read(MAX_OFFICE_METADATA_BYTES + 1)
                if len(raw) > MAX_OFFICE_METADATA_BYTES:
                    raise ResourceValidationError("Office metadata exceeds the validation size limit")
                # An explicit BOM allows UTF-16; do not guess encodings, and reject entity declarations and null bytes.
                encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                text = raw.decode(encoding)
                if "\x00" in text:
                    raise ResourceValidationError("Invalid Office metadata encoding")
                if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                    raise ResourceValidationError("Entity declarations are not allowed in Office metadata")
                return ET.fromstring(text)

            types = metadata("[Content_Types].xml")
            relationships = metadata("_rels/.rels")
            targets = [node for node in relationships
                       if node.tag == "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
                       and node.get("Type") in {
                           "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
                           "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"}]
            if len(targets) != 1 or targets[0].get("TargetMode", "Internal") != "Internal":
                raise ResourceValidationError("Office container lacks a unique internal main-document relationship")
            target = unquote(targets[0].get("Target", "")).lstrip("/")
            if (not target or "\\" in target or ":" in target
                    or ".." in target.split("/")):
                raise ResourceValidationError("Invalid Office main-document path")
            target = posixpath.normpath(target)
            info = archive.getinfo(target)
            if info.is_dir() or info.file_size == 0 or info.flag_bits & 1:
                raise ResourceValidationError("Office main-document part is empty or unreadable")
            overrides = [node.get("ContentType") for node in types
                         if node.tag == "{http://schemas.openxmlformats.org/package/2006/content-types}Override"
                         and unquote(node.get("PartName", "")).lstrip("/") == target]
            # OPC prefers Override matches by part name, falling back to Default matches by extension.
            # Source: a real WPS sample declares the workbook.xml main-document type through Default.
            content_types = overrides
            if not overrides:
                part_extension = posixpath.splitext(target)[1].lstrip(".").lower()
                content_types = [node.get("ContentType") for node in types
                                 if node.tag == "{http://schemas.openxmlformats.org/package/2006/content-types}Default"
                                 and node.get("Extension", "").lower() == part_extension
                                 and part_extension]
            if content_types != [OFFICE_TYPES[extension]]:
                raise ResourceValidationError("Office main-document type does not match its extension")
    except ResourceValidationError:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, UnicodeError, RuntimeError,
            NotImplementedError, ValueError, OSError) as exc:
        raise ResourceValidationError("Office container is corrupt or lacks required metadata") from exc


def _validate_content_length(data: bytes, value: str | None) -> None:
    """响应声明了非空 Content-Length 时，校验其为整数且等于实际字节数，否则抛出校验异常。"""
    if not value:
        return
    try:
        expected = int(value)
    except ValueError as exc:
        raise ResourceValidationError("Invalid response Content-Length") from exc
    if expected != len(data):
        raise ResourceValidationError(
            f"Incomplete resource download: Content-Length={expected}, actual={len(data)}"
        )


def _normalize_mime(value: str | None) -> str:
    """去除 MIME 参数和首尾空白并转为小写；缺失值返回空字符串。"""
    return (value or "").split(";", 1)[0].strip().lower()


def _safe_name(value: str | None) -> str:
    """提取原始路径的文件名，替换不允许的字符并去掉首尾点及下划线；缺失值返回空字符串。"""
    if not value:
        return ""
    return re.sub(r"[^\w.()\-\u4e00-\u9fff]+", "_", Path(value).name).strip("._")


def _safe_stem(value: str) -> str:
    """将资源标识转换为允许的文件名主干；结果为空时使用配置的后备名称。"""
    stem = re.sub(r"[^\w\-]+", "_", value).strip("._")
    return stem or CONFIG["resources"]["fileName"]["fallbackStem"]

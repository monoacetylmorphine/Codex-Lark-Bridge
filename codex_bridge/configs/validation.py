"""Complete configuration validation shared by both services, without initializing clients or writing runtime data."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def load_config(file_path: str | Path, schema_path: str | Path) -> dict[str, Any]:
    """读取 JSON 配置与 Schema，校验有限数值、Schema 约束及消息和音频字段间关系。

    返回校验后的配置字典；已处理的文件缺失、JSON 解析和校验错误转为 RuntimeError。
    不初始化客户端或写入运行数据，其他未捕获的文件读取异常原样传播。
    """
    file_path = Path(file_path).expanduser()
    try:
        config = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Configuration file does not exist: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid config.json format: {exc}") from exc
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise RuntimeError(f"Unable to load configuration Schema: {schema_path}") from exc

    _ensure_finite_numbers(config, file_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(config), key=lambda e: str(list(e.absolute_path)))
    if errors:
        details = "; ".join(
            f"{'.'.join(map(str, error.absolute_path)) or '<root>'}: violates the {error.validator} constraint"
            for error in errors
        )
        raise RuntimeError(f"Configuration validation failed ({file_path}): {details}")
    _validate_messaging(config["messaging"])
    _validate_audio_limits(config["audio"]["limits"])
    return config


def _ensure_finite_numbers(value: Any, file_path: Path, path: str = '<root>') -> None:
    """递归检查字典和列表中的浮点值，遇到 NaN 或无穷值时抛出包含字段路径的 RuntimeError。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError(f"Configuration validation failed ({file_path}): {path} must be a finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            _ensure_finite_numbers(item, file_path, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_finite_numbers(item, file_path, f"{path}.{index}")


def _validate_messaging(messaging: dict[str, Any]) -> None:
    """校验会话命令无空白且互不相同，并检查直接与缓冲消息类型构成不相交的支持类型集合。"""
    if any(character.isspace() for key in ("newThreadCommand", "stopThreadCommand")
           for character in messaging[key]):
        raise RuntimeError("Configuration validation failed: Session commands must not contain whitespace")
    direct, buffered = set(messaging["directTypes"]), set(messaging["bufferedTypes"])
    if direct & buffered or direct | buffered != set(messaging["supportedTypes"]):
        raise RuntimeError("Configuration validation failed: directTypes and bufferedTypes must not overlap, and their union must equal supportedTypes")
    if messaging["newThreadCommand"].lower() == messaging["stopThreadCommand"].lower():
        raise RuntimeError("Configuration validation failed: Session commands must be distinct")


def _validate_audio_limits(limits: dict[str, Any]) -> None:
    """校验音频目标字节数不超过最大字节数；违反约束时抛出 RuntimeError。"""
    if limits["targetBytes"] > limits["maxBytes"]:
        raise RuntimeError("Configuration validation failed: audio.limits.targetBytes must not exceed maxBytes")

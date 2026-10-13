"""Convert reverse-ordered Lark history into chronological Codex message batches."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from bridge.configs.loader import CONFIG


SUPPORTED_MESSAGE_TYPES = set(CONFIG["messaging"]["supportedTypes"])


@dataclass(frozen=True)
class CollectedMessage:
    message_id: str
    message_type: str
    content: dict[str, Any]
    sequence: int
    create_time: str | None = None


@dataclass(frozen=True)
class MessageBatch:
    chat_id: str
    union_id: str
    chat_type: str
    messages: tuple[CollectedMessage, ...]

    @property
    def unsupported_message_types(self) -> set[str]:
        """返回批次中未列入配置支持集合的消息类型。"""
        return {
            message.message_type
            for message in self.messages
            if message.message_type not in SUPPORTED_MESSAGE_TYPES
        }


def batch_from_chat_history(data: Any, history_items: list[Any]) -> MessageBatch:
    """从飞书倒序历史中提取当前用户轮次，返回按时间顺序排列的受支持消息批次。

    以触发消息为锚点，遇到 app 消息、/new、/stop 或私聊 /history 即停止回溯；
    跳过已删除及不支持的消息。历史接口尚未包含触发消息时补入该消息，命令自身返回空批次。
    批次 union_id 在群聊中保存 chat_id，在私聊中保存发送者 union_id。
    """
    event_message = data.event.message
    target_id = (
        event_message.chat_id
        if event_message.chat_type == "group"
        else data.event.sender.sender_id.union_id
    )
    retained_desc: list[Any] = []
    history_message_ids = {
        _field(item, "message_id")
        for item in history_items
    }
    current_id = event_message.message_id
    event_item = _history_item_from_event(data)

    if (is_stop_command(event_message.message_type, event_message.content)
            or is_new_command(event_message.message_type, event_message.content)
            or (event_message.chat_type == "p2p"
                and is_history_command(event_message.message_type, event_message.content))):
        return MessageBatch(event_message.chat_id, target_id, event_message.chat_type, ())
    else:
        # A history request can already include a later receive event. Anchor
        # this batch to its triggering message, so later input is routed only
        # after this submission has established its turn/status.
        for item in _candidate_history_items(event_message, history_items):
            sender = _field(item, "sender")
            sender_type = _field(sender, "sender_type")
            if sender_type == "app":
                break
            if sender_type == "user" and not _field(item, "deleted", False):
                # Both /new and /stop are history boundaries and are not merged with subsequent input.
                if _is_new_thread_command(item) or _is_command(item, "stopThreadCommand"):
                    break
                if (event_message.chat_type == "p2p" and is_history_command(
                        _field(item, "msg_type"), _field(_field(item, "body"), "content", ""))):
                    break
                if _field(item, "msg_type") in SUPPORTED_MESSAGE_TYPES:
                    retained_desc.append(item)

    # The list API can be briefly behind the receive event. Keep the triggering
    # message in the turn even when it has not appeared in history yet.
    if (event_message.message_type in SUPPORTED_MESSAGE_TYPES
            and current_id not in history_message_ids and event_item not in retained_desc):
        retained_desc.insert(0, event_item)

    messages = tuple(
        _parse_history_message(item, sequence)
        for sequence, item in enumerate(reversed(retained_desc), start=1)
    )
    return MessageBatch(
        chat_id=event_message.chat_id,
        union_id=target_id,
        chat_type=event_message.chat_type,
        messages=messages,
    )


def _candidate_history_items(event_message: Any, history_items: list[Any]) -> Iterator[Any]:
    """优先从触发消息所在位置截取历史；未找到时，仅凭有效数字时间戳过滤明确更晚的消息。"""
    current_id = event_message.message_id
    anchor = next((index for index, item in enumerate(history_items)
                   if _field(item, "message_id") == current_id), None)
    candidates = history_items[anchor:] if anchor is not None else history_items
    current_time = getattr(event_message, "create_time", None)
    for item in candidates:
        item_time = _field(item, "create_time")
        if (anchor is None and current_time and item_time
                and str(item_time).isdigit() and str(current_time).isdigit()
                and int(item_time) > int(current_time)):
            continue
        yield item


def _parse_history_message(item: Any, sequence: int) -> CollectedMessage:
    """将历史条目转换为带顺序的 CollectedMessage；JSON 解析失败时以 raw 字段保留原内容。"""
    body = _field(item, "body")
    raw_content = _field(body, "content", "")
    try:
        content = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        content = {"raw": raw_content}

    return CollectedMessage(
        message_id=_field(item, "message_id", ""),
        message_type=_field(item, "msg_type", ""),
        content=content,
        sequence=sequence,
        create_time=_field(item, "create_time"),
    )


def _is_new_thread_command(item: Any) -> bool:
    """判断历史条目是否为配置中的独立新会话命令。"""
    return _is_command(item, "newThreadCommand")


def is_new_command(message_type: str, raw_content: str) -> bool:
    """判断文本消息是否完整匹配配置的新会话命令，忽略首尾空白和大小写。"""
    return _is_command({"msg_type": message_type, "body": {"content": raw_content}}, "newThreadCommand")


def is_stop_command(message_type: str, raw_content: str) -> bool:
    """与 /new 一致：仅识别独立文本，忽略首尾空白和大小写。"""
    return _is_command({"msg_type": message_type, "body": {"content": raw_content}}, "stopThreadCommand")


def is_history_command(message_type: str, raw_content: str) -> bool:
    """私聊历史入口使用固定 /history；调用方决定聊天范围，不修改配置格式。"""
    if message_type != "text":
        return False
    try:
        content = json.loads(raw_content)
    except (TypeError, json.JSONDecodeError):
        return False
    text = content.get("text") if isinstance(content, dict) else None
    return isinstance(text, str) and text.strip().lower() == "/history"


def _is_command(item: Any, config_key: str) -> bool:
    """解析文本消息 JSON 并匹配指定配置命令；类型或内容不符合要求时返回 False。"""
    if _field(item, "msg_type") != "text":
        return False
    body = _field(item, "body")
    raw_content = _field(body, "content", "")
    try:
        content = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        return False
    text = content.get("text") if isinstance(content, dict) else None
    return (
        isinstance(text, str)
        and text.strip().lower() == CONFIG["messaging"][config_key].lower()
    )


def _history_item_from_event(data: Any) -> dict[str, Any]:
    """将接收事件转换为历史条目形状，供历史尚未包含触发消息时补入批次。"""
    message = data.event.message
    return {
        "message_id": message.message_id,
        "msg_type": message.message_type,
        "create_time": getattr(message, "create_time", None),
        "deleted": False,
        "sender": {"sender_type": "user"},
        "body": {"content": message.content},
    }


def _field(value: Any, name: str, default: Any = None) -> Any:
    """从字典键或对象属性读取字段；不存在时返回指定默认值。"""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)

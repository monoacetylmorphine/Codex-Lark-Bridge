"""Runtime probes and lifecycle deadlines, independent of IM clients and SDKs."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger
from codex_bridge.session.session_gateway import RETENTION_SECONDS

if TYPE_CHECKING:
    from openai_codex.models import Notification

logger = get_logger("Watchdog")


def settings() -> dict[str, Any]:
    """返回当前 Codex watchdog 配置字典的原始引用。"""
    return CONFIG["codex"]["watchdog"]


def should_log_event(method: str) -> bool:
    """保留生命周期和最终条目摘要，不记录流式片段。"""
    return (not method.lower().endswith("delta")
            and (not method.startswith("item/") or method == "item/completed"))


def _summary_fields(source: object, fields: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """提取白名单标量字段，解包 SDK 枚举及 root 值；字符串保留前 500 个字符，超长另加省略号。"""
    result = {}
    for attribute, key in fields:
        value = getattr(source, attribute, None)
        value = getattr(value, "value", value)
        value = getattr(value, "root", value)
        if isinstance(value, str):
            result[key] = value[:500] + ("…" if len(value) > 500 else "")
        elif isinstance(value, (bool, int, float)):
            result[key] = value
    return result


def summarize_item(item: Any) -> dict[str, Any]:
    """终端事件摘要，不序列化命令输出、工具结果或图片数据。"""
    result = _summary_fields(item, (
        ("id", "id"), ("type", "type"), ("status", "status"),
        ("duration_ms", "durationMs"), ("exit_code", "exitCode"),
    ))
    if item.type == "imageGeneration":
        result.update(_summary_fields(item, (
            ("saved_path", "savedPath"), ("transparent_background", "transparentBackground"),
        )))
    elif item.type == "agentMessage":
        result.update(_summary_fields(item, (("phase", "phase"),)))
        result["textLength"] = len(getattr(item, "text", "") or "")
    return result


def summarize_event(event: Notification) -> dict[str, Any]:
    """生成事件定位、状态、错误及已完成 item 的白名单摘要。

    可包含截断的错误消息和附加说明；不展开 turn.items、命令输出、工具结果或图片数据。
    """
    payload = event.payload
    result = {"method": event.method, **_summary_fields(payload, (
        ("thread_id", "threadId"), ("turn_id", "turnId"), ("will_retry", "willRetry"),
    ))}
    if event.method == "item/completed":
        result["item"] = summarize_item(payload.item.root)
    turn = getattr(payload, "turn", None)
    if turn is not None:
        result["turn"] = _summary_fields(turn, (
            ("id", "id"), ("status", "status"), ("duration_ms", "durationMs"),
        ))
    error = getattr(payload, "error", None) or getattr(turn, "error", None)
    if error is not None:
        result["error"] = _summary_fields(error, (
            ("message", "message"), ("additional_details", "additionalDetails"),
        ))
    return result


probes: dict[str, RunProbe] = {}


@dataclass
class RunProbe:
    run_id: str
    chat_id: str
    message_ids: frozenset[str]
    received_at: float = field(default_factory=time.monotonic)
    phase: str = "queued"
    phase_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    last_event: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    terminal: str | None = None
    stop_reason: str | None = None
    stop_confirmed: bool = False
    cleanup_at: float | None = None
    cleanup_timed_out: bool = False
    tools: set[str] = field(default_factory=set)
    event_count: int = 0

    def stage(self, phase: str) -> None:
        """仅在阶段变化时更新阶段及起始时间。"""
        if self.phase == phase:
            return
        self.phase = phase
        self.phase_at = time.monotonic()

    def finish(self, result: str) -> None:
        """仅首次设置运行终态，同时启动收尾计时。"""
        if self.terminal is None:
            self.terminal = result
            self.begin_cleanup()

    def begin_cleanup(self) -> None:
        """模型结束或收到停止请求后，开始独立的收尾计时。"""
        if self.cleanup_at is None:
            self.cleanup_at = time.monotonic()

    def activity(self, event: Notification) -> None:
        """刷新活动时间、事件计数和工具执行集合，并在 turn 完成时设置终态。"""
        self.last_activity = time.monotonic()
        if should_log_event(event.method):
            self.last_event = event.method
        self.event_count += 1
        if event.method in {"item/started", "item/completed"}:
            item = event.payload.item.root
            if item.type in {"commandExecution", "mcpToolCall", "dynamicToolCall", "webSearch"}:
                if event.method == "item/started":
                    self.tools.add(item.id)
                else:
                    self.tools.discard(item.id)
        if event.method == "turn/completed":
            status = event.payload.turn.status
            self.finish(self.stop_reason or getattr(status, "value", status))

    def expired(self, now: float | None = None) -> str | None:
        """根据运行状态返回 cleanup_timeout、run_timeout、startup_timeout、idle_timeout 或 None。

        终态或停止请求后改用独立收尾期限；工具执行中跳过空闲超时，仍检查总运行期限。
        now 使用单调时钟；本方法只判定原因，不执行停止操作。
        """
        now = time.monotonic() if now is None else now
        if self.terminal or self.stop_reason:
            self.begin_cleanup()
            if now - self.cleanup_at >= settings().get("finalizationTimeoutSeconds", 30):
                return "cleanup_timeout"
            return None
        policy = settings()
        if now - self.received_at >= min(policy["maxRunSeconds"], RETENTION_SECONDS):
            return "run_timeout"
        if self.phase == "starting" and now - self.phase_at >= policy["startupTimeoutSeconds"]:
            return "startup_timeout"
        # Tool execution can legitimately be silent; the hard deadline remains.
        if (policy["idleTimeoutSeconds"] and self.phase == "running" and not self.tools
                and now - self.last_activity >= policy["idleTimeoutSeconds"]):
            return "idle_timeout"
        return None

"""Runtime probes and append-only JSONL logs, independent of IM clients and SDKs."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, TYPE_CHECKING

from bridge.configs.loader import CONFIG
from bridge.utils.terminal_log import get_logger
from bridge.session.session_gateway import RETENTION_SECONDS

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
    """终端与 JSONL 共用摘要，不序列化命令输出、工具结果或图片数据。"""
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


class RunJournal:
    """One writer per process. Append one JSON object per line.

    ``path`` is a directory; records use their enqueue date in the configured
    timezone. Disk I/O runs in one background writer, and failed batches retry
    their original daily file.
    """

    def __init__(self, path: str | Path) -> None:
        """初始化按日期分文件的日志队列、计数及线程锁；首次记录时才启动后台写入线程。"""
        self.path = Path(path)
        self._timezone = timezone(timedelta(hours=CONFIG["runtime"]["timezoneOffsetHours"]))
        self._lock = threading.Lock()
        self._records: deque[tuple[Path, dict[str, Any], float]] = deque()
        self._revision = 0
        self._saved = 0
        self._discarded = 0
        self._thread: threading.Thread | None = None

    def record(self, record: dict[str, Any]) -> None:
        """按配置时区的入队日期确定日志文件，将记录入队并按需启动后台线程。

        返回时不保证已经落盘；记录字典按引用保存，调用方入队后不应再修改。
        """
        with self._lock:
            day = datetime.now(self._timezone).strftime("%Y-%m-%d")
            self._records.append((self.path / f"{day}_logs.jsonl", record, time.monotonic()))
            self._revision += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._write, daemon=True)
                self._thread.start()

    def _write_pending(self) -> None:
        """丢弃超过保留期限的待写记录，再按连续日期分组追加日志。

        每组成功后才出队，后续分组失败不回滚已写组；写入异常交由后台循环处理。
        """
        with self._lock:
            expired = 0
            now = time.monotonic()
            while self._records and now - self._records[0][2] >= RETENTION_SECONDS:
                self._records.popleft()
                self._discarded += 1
                expired += 1
            records = list(self._records)
        if expired:
            logger.error("Run logs remained unwritten for over 24 hours; discarded %s records without persisting them", expired)
        # Commit each contiguous date group separately. If a later file fails,
        # successful groups have already left the queue and cannot be duplicated.
        for path, group in groupby(records, key=lambda entry: entry[0]):
            batch = list(group)
            payload = "".join(json.dumps(record, ensure_ascii=False) + "\n"
                              for _, record, _ in batch).encode("utf-8")
            self._append_payload(path, payload)
            with self._lock:
                for _ in batch:
                    self._records.popleft()
                self._saved += len(batch)

    @staticmethod
    def _append_payload(path: Path, payload: bytes) -> None:
        """创建日志目录并追加字节；写入或刷新失败时尝试截断回原偏移，再抛出异常。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as file:
            offset = file.tell()
            try:
                file.write(payload)
                file.flush()
            except Exception:
                file.truncate(offset)
                raise

    def _write(self) -> None:
        """持续在后台写入待处理日志；失败时记录异常并短暂等待后重试。"""
        while True:
            time.sleep(0.1)
            try:
                self._write_pending()
            except Exception:
                logger.exception("Failed to write run logs: %s", self.path)
                time.sleep(1)

    async def flush(self) -> None:
        """在配置的收尾超时内等待调用时已入队的日志处理完成。

        不等待此后新入队的记录；发生过期丢弃时抛出 RuntimeError，等待超时则传播 TimeoutError。
        """
        with self._lock:
            target = self._revision
        async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
            while True:
                with self._lock:
                    if self._saved + self._discarded >= target:
                        if self._discarded:
                            raise RuntimeError(f"Discarded {self._discarded} expired log records; not all records were written")
                        return
                await asyncio.sleep(0.05)

journal = RunJournal(CONFIG["storage"]["runLogPath"])
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

    def record(self, event: str, **details: Any) -> None:
        """将运行身份、阶段、终态、耗时及工具活动等快照连同事件详情写入日志队列。"""
        journal.record({
            "time": datetime.now(timezone.utc).isoformat(),
            "runId": self.run_id, "chatId": self.chat_id,
            "messageIds": sorted(self.message_ids),
            "threadId": self.thread_id, "turnId": self.turn_id,
            "phase": self.phase, "terminal": self.terminal,
            "stopReason": self.stop_reason, "stopConfirmed": self.stop_confirmed,
            "elapsedSeconds": round(time.monotonic() - self.received_at, 3),
            "idleSeconds": round(time.monotonic() - self.last_activity, 3),
            "lastEvent": self.last_event, "eventCount": self.event_count,
            "tools": sorted(self.tools), "event": event, **details,
        })

    def stage(self, phase: str) -> None:
        """仅在阶段变化时更新阶段及起始时间，并记录阶段事件。"""
        if self.phase == phase:
            return
        self.phase = phase
        self.phase_at = time.monotonic()
        self.record("stage")

    def finish(self, result: str) -> None:
        """仅首次设置运行终态，同时启动收尾计时并记录终态事件。"""
        if self.terminal is None:
            self.terminal = result
            self.begin_cleanup()
            self.record("terminal")

    def begin_cleanup(self) -> None:
        """模型结束或收到停止请求后，开始独立的收尾计时。"""
        if self.cleanup_at is None:
            self.cleanup_at = time.monotonic()

    def activity(self, event: Notification) -> None:
        """刷新活动时间和事件计数，维护已识别工具的执行集合并记录完成摘要。

        收到 turn/completed 时优先使用已登记的停止原因设置终态，不记录完整工具结果。
        """
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
            if event.method == "item/completed":
                self.record(event.method, item=summarize_item(item))
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

"""Task state and short-lived deduplication caches, released per task and retained for at most 24 hours."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger

if TYPE_CHECKING:
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    from codex_bridge.codex.thread_watchdog import RunProbe
    from codex_bridge.services.lark.lark_message_card import CardStreamSession

logger = get_logger("Reaction")

# Source: user requirement to retain in-memory state and pending logs for at most 24 hours.
RETENTION_SECONDS = 24 * 60 * 60


@dataclass
class ReactionCleanup:
    created_at: float = field(default_factory=time.monotonic)
    due: float = 0
    attempts: int = 0

@dataclass
class ActiveCodexRun:
    run_id: str
    chat_id: str
    message_ids: frozenset[str]
    interrupted: bool = False
    codex_started: bool = False
    controls: asyncio.Lock = field(default_factory=asyncio.Lock)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    target_id: str = ""
    probe: RunProbe | None = None
    task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None
    timeout_task: asyncio.Task[None] | None = None
    notified: bool = False
    cards: CardStreamSession | None = None

    created_at: float = field(default_factory=time.monotonic)
    expired: bool = False
    detached: bool = False
    released: bool = False


class SessionCache:
    def __init__(self) -> None:
        """初始化聊天锁、消息去重、运行归属、缓冲及 reaction 清理等内存索引，不启动后台任务。"""
        self.codex_locks: dict[str, asyncio.Lock] = {}
        self.reset_tasks: dict[str, asyncio.Task[object]] = {}
        self.buffered_events: dict[str, P2ImMessageReceiveV1] = {}
        self.buffer_tasks: dict[str, asyncio.Task[None]] = {}
        self.reaction_message_ids: dict[str, dict[str, str]] = {}
        self.chat_generations: dict[str, int] = {}
        self.pending_chat_requests: dict[str, int] = {}
        self.received_message_ids: dict[str, dict[str, float | None]] = {}
        self.processed_message_ids: dict[str, dict[str, float | None]] = {}
        self.received_at: dict[str, float] = {}
        self.active_runs_by_message_id: dict[str, ActiveCodexRun] = {}
        self.active_runs_by_chat: dict[str, ActiveCodexRun] = {}
        self.all_runs: dict[str, ActiveCodexRun] = {}
        self.blocked_chats: dict[str, ActiveCodexRun] = {}
        self.codex_locks_guard = asyncio.Lock()
        self.buffer_guard = asyncio.Lock()
        self.active_runs_guard = asyncio.Lock()
        self.reactions_being_cleared: set[tuple[str, str, str]] = set()
        self.pending_reactions: dict[tuple[str, str, str], ReactionCleanup] = {}
        self.recalled_message_ids: set[str] = set()
        self._recall_times: dict[str, float] = {}
        self._reaction_times: dict[tuple[str, str], float] = {}
        self._users: dict[str, int] = {}
        self._buffer_times: dict[str, float] = {}

    @contextmanager
    def hold(self, chat_id: str) -> Iterator[None]:
        """在首次 await 前登记；包括等待锁、重置、历史请求的调用者。"""
        self._users[chat_id] = self._users.get(chat_id, 0) + 1
        try:
            yield
        finally:
            self._users[chat_id] -= 1
            if not self._users[chat_id]:
                self._users.pop(chat_id)
            self.release_idle(chat_id)

    def remember(
        self,
        mapping: dict[str, dict[str, float | None]],
        chat_id: str,
        message_id: str,
    ) -> None:
        """记录消息首次出现的单调时间，清理过期项并按配置容量淘汰最早登记项；重复消息不延长保留期。"""
        now = time.monotonic()
        entries = mapping.setdefault(chat_id, {})
        # Duplicate events do not extend the original message retention period.
        entries.setdefault(message_id, now)
        for key, timestamp in list(entries.items()):
            if timestamp is not None and now - timestamp >= RETENTION_SECONDS:
                entries.pop(key, None)
        while len(entries) > CONFIG["messaging"]["recallCacheMaxEntries"]:
            entries.pop(next(iter(entries)))

    def recall(self, message_id: str) -> None:
        """登记消息撤回及首次撤回时间，超过容量时移除最早登记的撤回标记。"""
        self.recalled_message_ids.add(message_id)
        self._recall_times.setdefault(message_id, time.monotonic())
        while len(self.recalled_message_ids) > CONFIG["messaging"]["recallCacheMaxEntries"]:
            oldest = next(iter(self._recall_times))
            self.recalled_message_ids.discard(oldest)
            self._recall_times.pop(oldest, None)

    def track_reaction(self, chat_id: str, message_id: str) -> None:
        """记录聊天消息 reaction 的首次跟踪时间，用于后续保留期限清理。"""
        self._reaction_times.setdefault((chat_id, message_id), time.monotonic())

    def queue_reaction(self, chat_id: str, message_id: str, reaction_id: str) -> None:
        """按聊天、消息和 reaction 标识去重登记待删记录，不直接发起删除请求。

        队列满时淘汰最早记录并清理仍匹配的 reaction 索引，同时记录未确认删除的日志。
        """
        key = (chat_id, message_id, reaction_id)
        if key not in self.pending_reactions:
            # Source: reuse the short-lived message cache capacity; failed deletion records outlive the task.
            if len(self.pending_reactions) >= CONFIG["messaging"]["recallCacheMaxEntries"]:
                oldest = next(iter(self.pending_reactions))
                self.pending_reactions.pop(oldest)
                chat, mid, rid = oldest
                if self.reaction_message_ids.get(chat, {}).get(mid) == rid:
                    self.reaction_message_ids[chat].pop(mid)
                    if not self.reaction_message_ids[chat]:
                        self.reaction_message_ids.pop(chat)
                    self._reaction_times.pop((chat, mid), None)
                logger.error("Reaction cleanup queue is full; discarding unconfirmed record: %s", oldest)
            self.pending_reactions[key] = ReactionCleanup()

    def release_messages(
        self,
        chat_id: str,
        message_ids: Iterable[str],
        owner: ActiveCodexRun | None = None,
    ) -> None:
        """只清理本任务的瞬态数据；已转交的消息由接收方任务清理。"""
        for mid in message_ids:
            current = self.active_runs_by_message_id.get(mid)
            if current is not None and current is not owner:
                continue
            self.received_at.pop(mid, None)
            self.recalled_message_ids.discard(mid)
            self._recall_times.pop(mid, None)
            # Reactions are deleted after card finalization and must not be discarded with the task cache.

    def finish_run(self, state: ActiveCodexRun) -> None:
        """将仍属于本运行的 reaction 加入清理队列，释放消息瞬态数据及任务、卡片引用，再尝试回收空闲聊天状态。"""
        for mid in state.message_ids:
            if self.active_runs_by_message_id.get(mid) not in (None, state):
                continue
            rid = self.reaction_message_ids.get(state.chat_id, {}).get(mid)
            if rid is not None:
                self.queue_reaction(state.chat_id, mid, rid)
        self.release_messages(state.chat_id, state.message_ids, state)
        # Release task and stream references; blocked state retains only metadata needed to retry stopping.
        state.task = None
        state.stop_task = None
        state.timeout_task = None
        state.cards = None
        self.release_idle(state.chat_id)

    def release_idle(self, chat_id: str) -> None:
        """聊天无使用者、任务、缓冲、阻塞状态及持有中的锁时，回收提交锁和代次等瞬态状态。

        保留短期去重记录，并把未删除的 reaction 登记到独立清理队列，不执行网络请求。
        """
        if (self._users.get(chat_id) or self.pending_chat_requests.get(chat_id)
                or chat_id in self.reset_tasks or chat_id in self.buffer_tasks
                or chat_id in self.buffered_events or chat_id in self.blocked_chats
                or chat_id in self.active_runs_by_chat
                or any(run.chat_id == chat_id for run in self.all_runs.values())
                or any(run.chat_id == chat_id for run in self.active_runs_by_message_id.values())):
            return
        lock = self.codex_locks.get(chat_id)
        if lock is not None and lock.locked():
            return
        self.codex_locks.pop(chat_id, None)
        self.chat_generations.pop(chat_id, None)
        self.release_messages(chat_id, set(self.received_message_ids.get(chat_id, {}))
                              | set(self.reaction_message_ids.get(chat_id, {})))
        for mid, reaction_id in list(self.reaction_message_ids.get(chat_id, {}).items()):
            self.queue_reaction(chat_id, mid, reaction_id)

    def prune(self, now: float | None = None) -> list[ActiveCodexRun]:
        """定期清理无后续消息的会话；返回需要关闭的过期任务。"""
        now = time.monotonic() if now is None else now
        for key, pending in list(self.pending_reactions.items()):
            if now - pending.created_at >= RETENTION_SECONDS:
                self.pending_reactions.pop(key, None)
                chat, mid, rid = key
                if self.reaction_message_ids.get(chat, {}).get(mid) == rid:
                    self.reaction_message_ids[chat].pop(mid)
                    if not self.reaction_message_ids[chat]:
                        self.reaction_message_ids.pop(chat)
                    self._reaction_times.pop((chat, mid), None)
                logger.error("Reaction deletion remains unconfirmed after 24 hours; discarded: %s", key)
        for chat, timestamp in list(self._buffer_times.items()):
            if chat not in self.buffered_events or now - timestamp >= RETENTION_SECONDS:
                self._buffer_times.pop(chat, None)
                data = self.buffered_events.pop(chat, None)
                task = self.buffer_tasks.pop(chat, None)
                if task is not None and not task.done():
                    task.cancel()
                if data is not None:
                    self.release_messages(chat, {data.event.message.message_id})
        for mapping in (self.received_message_ids, self.processed_message_ids):
            for chat, entries in list(mapping.items()):
                for mid, timestamp in list(entries.items()):
                    if timestamp is not None and now - timestamp >= RETENTION_SECONDS:
                        entries.pop(mid, None)
                if not entries:
                    mapping.pop(chat, None)
        for mid, timestamp in list(self.received_at.items()):
            if now - timestamp >= RETENTION_SECONDS:
                self.received_at.pop(mid, None)
        for mid, timestamp in list(self._recall_times.items()):
            if mid not in self.recalled_message_ids or now - timestamp >= RETENTION_SECONDS:
                self.recalled_message_ids.discard(mid)
                self._recall_times.pop(mid, None)
        for (chat, mid), timestamp in list(self._reaction_times.items()):
            if (mid not in self.reaction_message_ids.get(chat, {})
                    or now - timestamp >= RETENTION_SECONDS):
                self.reaction_message_ids.get(chat, {}).pop(mid, None)
                self._reaction_times.pop((chat, mid), None)
                if not self.reaction_message_ids.get(chat):
                    self.reaction_message_ids.pop(chat, None)
        for chat in set(self.codex_locks) | set(self.chat_generations):
            self.release_idle(chat)
        runs = {run.run_id: run for run in (*self.all_runs.values(), *self.blocked_chats.values())}
        return [run for run in runs.values() if now - run.created_at >= RETENTION_SECONDS]

    def expire_run(self, state: ActiveCodexRun) -> None:
        """关闭尝试结束后，即使失败也按用户要求解除过期缓存。"""
        for mid, owner in list(self.active_runs_by_message_id.items()):
            if owner is state:
                self.active_runs_by_message_id.pop(mid, None)
        for mapping, key in ((self.all_runs, state.run_id),
                             (self.active_runs_by_chat, state.chat_id),
                             (self.blocked_chats, state.chat_id)):
            if mapping.get(key) is state:
                mapping.pop(key, None)
        self.finish_run(state)

"""Lark message entry, fixed-window buffering, and history collection with callback-based task submission."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger
from codex_bridge.codex.thread_watchdog import RunProbe, settings
from codex_bridge.session.session_gateway import ActiveCodexRun, RETENTION_SECONDS
from codex_bridge.middleware.message_collector import batch_from_chat_history, is_new_command, is_stop_command, is_history_command

if TYPE_CHECKING:
    from lark_oapi.api.im.v1 import Message, MentionEvent, P2ImMessageReceiveV1

    from codex_bridge.middleware.message_collector import MessageBatch
    from codex_bridge.session.session_gateway import SessionCache

logger = get_logger("Message")

def _bot_identity(*, required: bool = False) -> tuple[str, str]:
    """读取并去除机器人 Union ID 与名称环境变量的首尾空白。

    required 为真且两者均为空时抛出 RuntimeError，否则返回两项字符串，缺失项为空字符串。
    """
    credentials = CONFIG["lark"]["credentials"]
    def value(key: str) -> str:
        """按配置键查找环境变量名称并读取去空白后的值；缺少配置或变量时返回空字符串。"""
        env_name = credentials.get(key)
        return (os.getenv(env_name, "").strip() if env_name else "")
    robot_union_id = value("robotUnionIdEnv")
    robot_name = value("robotNameEnv")
    if required and not (robot_union_id or robot_name):
        raise RuntimeError("Group-chat bot identity is not configured: at least one environment variable referenced by robotUnionIdEnv or robotNameEnv must be non-empty")
    return robot_union_id, robot_name

def _is_bot_mentioned(mentions: Iterable[MentionEvent] | None) -> bool:
    """检查任一 mention 是否匹配已配置的非空机器人 Union ID 或名称；未配置的标识不参与匹配。"""
    robot_union_id, robot_name = _bot_identity()
    for mention in mentions or []:
        mention_id = getattr(mention, "id", None)
        mentioned_union_id = getattr(mention_id, "union_id", None)
        if robot_union_id and mentioned_union_id == robot_union_id:
            return True
        if robot_name and getattr(mention, "name", None) == robot_name:
            return True
    return False

class MessageRouter:
    def __init__(
        self,
        cache: SessionCache,
        *,
        submit: Callable[[MessageBatch, int], Awaitable[None]],
        stop: Callable[[P2ImMessageReceiveV1], Awaitable[None]],
        new: Callable[[P2ImMessageReceiveV1], Awaitable[None]],
        notify: Callable[[ActiveCodexRun, str], Awaitable[None]],
        fetch_history: Callable[..., Awaitable[list[Message]]],
        add_reaction: Callable[..., Awaitable[str | None]],
        clear_reactions: Callable[..., Awaitable[None]],
        send_card: Callable[..., Awaitable[str | None]],
        buffer_seconds: Callable[[], float],
        history: Callable[[P2ImMessageReceiveV1], Awaitable[None]] | None = None,
    ) -> None:
        """注入共享缓存、消息及命令处理回调和缓冲时长提供函数，不启动网络或后台任务。"""
        self.cache = cache
        self.submit = submit
        self.stop = stop
        self.new = new
        self.notify = notify
        self.fetch_history = fetch_history
        self.add_reaction = add_reaction
        self.clear_reactions = clear_reactions
        self.send_card = send_card
        self.buffer_seconds = buffer_seconds
        self.history = history

    async def processing(self, data: P2ImMessageReceiveV1) -> None:
        """在保留期限内处理入站事件；内部登记缓存使用者，防止处理期间聊天锁被回收。"""
        async with asyncio.timeout(RETENTION_SECONDS):
            await self._processing(data)

    async def _processing(self, data: P2ImMessageReceiveV1) -> None:
        """过滤聊天范围及群聊 mention，去重消息并优先分发历史、停止和新会话命令。

        普通输入等待重置门禁后添加 reaction 并按配置路由；代次失效时放弃旧输入，
        finally 释放不再处于缓冲中的消息瞬态状态。
        """
        with self.cache.hold(data.event.message.chat_id):
            self.cache.prune()
            try:
                chat_type = data.event.message.chat_type
                if chat_type == "group" and not _is_bot_mentioned(data.event.message.mentions):
                    return
                if chat_type not in {"p2p", "group"}:
                    return
                message = data.event.message
                if message.message_id in self.cache.received_message_ids.get(message.chat_id, {}):
                    return
                self.cache.remember(self.cache.received_message_ids, message.chat_id, message.message_id)
                self.cache.received_at[message.message_id] = time.monotonic()
                while len(self.cache.received_at) > CONFIG["messaging"]["recallCacheMaxEntries"]:
                    self.cache.received_at.pop(next(iter(self.cache.received_at)))
                if (chat_type == "p2p" and self.history is not None
                        and is_history_command(message.message_type, message.content)):
                    await self.history(data)
                    return
                if is_stop_command(message.message_type, message.content):
                    await self.stop(data)
                    return
                if is_new_command(message.message_type, message.content):
                    await self.new(data)
                    return
                chat_id = message.chat_id
                generation = self.cache.chat_generations.get(chat_id, 0)
                while (reset := self.cache.reset_tasks.get(chat_id)) is not None:
                    await asyncio.shield(reset)
                if generation != self.cache.chat_generations.get(chat_id, 0):
                    return
                self.cache.pending_chat_requests[chat_id] = self.cache.pending_chat_requests.get(chat_id, 0) + 1
                try:
                    try:
                        async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                            reaction_id = await self.add_reaction(message_id=message.message_id)
                    except Exception:
                        logger.exception("Failed to add reaction; continuing message processing: message_id=%s", message.message_id)
                        reaction_id = None
                    if reaction_id is not None:
                        self.cache.reaction_message_ids.setdefault(chat_id, {})[message.message_id] = reaction_id
                        self.cache.track_reaction(chat_id, message.message_id)
                    if generation != self.cache.chat_generations.get(chat_id, 0):
                        if reaction_id is not None:
                            await self.clear_reactions(chat_id, {message.message_id})
                        return
                    if message.message_id in self.cache.processed_message_ids.get(chat_id, {}):
                        # History may have included this attachment before its receive
                        # event completed the reaction request.
                        if message.message_id not in self.cache.active_runs_by_message_id:
                            await self.clear_reactions(chat_id, {message.message_id})
                        return
                    await self._route_message(data, generation)
                finally:
                    self.cache.pending_chat_requests[chat_id] -= 1
                    if not self.cache.pending_chat_requests[chat_id]:
                        self.cache.pending_chat_requests.pop(chat_id)
            except Exception as exc:
                logger.exception("Message parsing error: chat_id=%s", data.event.message.chat_id)
            finally:
                message = data.event.message
                buffered = self.cache.buffered_events.get(message.chat_id)
                pending = getattr(getattr(getattr(buffered, "event", None), "message", None), "message_id", None)
                if pending != message.message_id:
                    self.cache.release_messages(message.chat_id, {message.message_id})

    async def _route_message(self, data: P2ImMessageReceiveV1, generation: int) -> None:
        """按配置选择缓冲或立即拉取历史的路径；不支持的消息发送提示并清理 reaction。"""
        message_type = data.event.message.message_type
        if message_type in CONFIG["messaging"]["bufferedTypes"]:
            await self._buffer_message(data, generation)
            return

        if message_type in CONFIG["messaging"]["directTypes"]:
            await self._cancel_buffer(data.event.message.chat_id)
            await self._process_chat_history(data, generation)
            return
        message = data.event.message
        target_id = message.chat_id if message.chat_type == "group" else data.event.sender.sender_id.union_id
        try:
            await self.send_card(target_id, CONFIG["messages"]["unsupportedMessageType"])
        finally:
            await self.clear_reactions(message.chat_id, {message.message_id})

    async def _buffer_message(self, data: P2ImMessageReceiveV1, generation: int) -> None:
        """为聊天启动不随新消息延长的固定缓冲窗口，并将最新事件保存为本轮历史锚点。"""
        chat_id = data.event.message.chat_id
        async with self.cache.buffer_guard:
            if generation != self.cache.chat_generations.get(chat_id, 0):
                return
            # Keep the latest event so the history fallback is anchored to the
            # newest message received during this fixed window.
            self.cache.buffered_events[chat_id] = data
            self.cache._buffer_times.setdefault(chat_id, time.monotonic())
            task = self.cache.buffer_tasks.get(chat_id)
            if task is None or task.done():
                self.cache.buffer_tasks[chat_id] = asyncio.create_task(
                    self._flush_after_fixed_window(chat_id, generation)
                )

    async def _cancel_buffer(self, chat_id: str) -> None:
        """在锁内移除聊天的缓冲事件及时间记录，并取消尚未结束的缓冲任务。"""
        async with self.cache.buffer_guard:
            self.cache.buffered_events.pop(chat_id, None)
            self.cache._buffer_times.pop(chat_id, None)
            task = self.cache.buffer_tasks.pop(chat_id, None)
            if task is not None and not task.done():
                task.cancel()

    async def _flush_after_fixed_window(self, chat_id: str, generation: int) -> None:
        """等待配置的缓冲时长后取出本轮最新事件，尝试一次历史处理并释放缓存引用。

        等待时长不超过保留期限；取消向外传播，其余异常记录日志。
        """
        data = None
        try:
            await asyncio.sleep(min(self.buffer_seconds(), RETENTION_SECONDS))
            async with self.cache.buffer_guard:
                data = self.cache.buffered_events.pop(chat_id, None)
                self.cache._buffer_times.pop(chat_id, None)
                self.cache.buffer_tasks.pop(chat_id, None)

            if data is None:
                return
            await self._process_chat_history(data, generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Message buffer processing error: chat_id=%s", chat_id)
        finally:
            if data is not None:
                self.cache.release_messages(chat_id, {data.event.message.message_id})
            self.cache.release_idle(chat_id)

    async def _process_chat_history(self, data: P2ImMessageReceiveV1, generation: int) -> None:
        """在代次有效且消息未撤回时，按剩余任务时限获取一页历史并组装批次。

        非空批次交给提交回调，历史请求异常交给失败通知与清理流程；本函数不直接调用 Codex SDK。
        """
        with self.cache.hold(data.event.message.chat_id):

            current_message_id = data.event.message.message_id
            if generation != self.cache.chat_generations.get(data.event.message.chat_id, 0):
                return
            async with self.cache.active_runs_guard:
                if current_message_id in self.cache.recalled_message_ids:
                    self.cache.recalled_message_ids.discard(current_message_id)
                    was_recalled = True
                else:
                    was_recalled = False
            if was_recalled:
                await self.clear_reactions(data.event.message.chat_id, {current_message_id})
                return

            try:
                remaining = min(settings()["maxRunSeconds"], RETENTION_SECONDS) - (time.monotonic() - self.cache.received_at.get(
                    current_message_id, time.monotonic()))
                async with asyncio.timeout(max(0, remaining)):
                    history_items = await self.fetch_history(
                        chat_id=data.event.message.chat_id,
                        messages_num=CONFIG["messaging"]["historyPageSize"],
                    )
            except Exception:
                await self._history_failed(data, generation, current_message_id)
                return
            batch = batch_from_chat_history(data, history_items)
            if batch.messages:
                await self.submit(batch, generation)

    async def _history_failed(
        self,
        data: P2ImMessageReceiveV1,
        generation: int,
        current_message_id: str,
    ) -> None:
        """历史请求失败时沿用当前通知和 reaction 清理顺序。"""
        logger.exception("Failed to fetch message history: message_id=%s", current_message_id)
        state = ActiveCodexRun(uuid4().hex, data.event.message.chat_id,
                               frozenset({current_message_id}),
                               target_id=(data.event.message.chat_id if data.event.message.chat_type == "group"
                                          else data.event.sender.sender_id.union_id))
        state.probe = RunProbe(state.run_id, state.chat_id, state.message_ids)
        state.probe.finish("failed")
        if generation == self.cache.chat_generations.get(state.chat_id, 0):
            await self.notify(state, "codexFailure")
        await self.clear_reactions(state.chat_id, state.message_ids)

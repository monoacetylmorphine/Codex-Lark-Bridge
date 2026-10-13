"""Recall, stop, new-session, and history selection and restoration commands."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import NotRequired, TYPE_CHECKING, TypedDict

from codex_bridge.session.session_history import get_session, list_sessions
from codex_bridge.codex.thread_manager import get_user_thread
from codex_bridge.codex.thread_watchdog import settings
from codex_bridge.services.lark import lark_interactive_card as interactive
from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger
from .gateway_runtime import GatewayRuntime

if TYPE_CHECKING:
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, P2ImMessageRecalledV1
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger, P2CardActionTriggerResponse,
    )

    from codex_bridge.session.session_gateway import ActiveCodexRun
    from codex_bridge.services.lark.lark_interactive_card import HistoryOption
    from codex_bridge.utils.event_dispatcher import EventDispatcher

logger = get_logger("Message")


class HistoryRestoreResult(TypedDict):
    """Restoration results preserve the dictionary protocol and omit the thread ID on failure."""

    ok: bool
    content: str
    thread_id: NotRequired[str]


class GatewayCommands:
    """Reuse runtime stop and submission gates while managing history card context independently."""

    def __init__(
        self,
        runtime: GatewayRuntime,
        *,
        cancel_buffer: Callable[[str], Awaitable[None]],
    ) -> None:
        """绑定共享 runtime、缓存和缓冲取消入口，并创建独立的历史卡片存储。"""
        self.runtime = runtime
        self.cache = runtime.cache
        self.cancel_buffer = cancel_buffer
        self.history_cards = interactive.HistoryCardStore()

    async def prune_history(self) -> None:
        """供启动层调度历史卡片过期清理。"""
        self.history_cards.prune()

    async def processing_recall(self, data: P2ImMessageRecalledV1) -> None:
        """处理消息所有者发起的撤回，停止关联任务或登记撤回以阻止后续提交。

        同时取消对应的缓冲触发消息并尝试清理 reaction；存在关联任务时发送停止结果通知。
        """
        event = data.event
        if event.recall_type != "message_owner":
            return

        message_id = event.message_id
        chat_id = event.chat_id
        async with self.cache.active_runs_guard:
            state = self.cache.active_runs_by_message_id.get(message_id)
            if state is None:
                state = next((run for run in self.cache.all_runs.values()
                              if run.chat_id == chat_id and message_id in run.message_ids), None)
            if state is None:
                self.cache.recall(message_id)
            else:
                state.interrupted = True

        async with self.cache.buffer_guard:
            buffered = self.cache.buffered_events.get(chat_id)
            buffered_message = getattr(getattr(buffered, "event", None), "message", None)
            if getattr(buffered_message, "message_id", None) == message_id:
                self.cache.buffered_events.pop(chat_id, None)
                self.cache._buffer_times.pop(chat_id, None)
                task = self.cache.buffer_tasks.pop(chat_id, None)
                if task is not None:
                    task.cancel()
                self.cache.recalled_message_ids.discard(message_id)

        if state is not None:
            if state.probe and not state.probe.stop_reason:
                state.probe.stop_reason = "recalled"
            succeeded = await self.runtime._interrupt_run(state)
            await self.runtime._notify_terminal(state, "recallStopped" if succeeded else "stopFailed")
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                await self.runtime._clear_reaction(chat_id, {message_id})
        except Exception:
            logger.exception("Failed to clean up reactions for a recalled message: message_id=%s", message_id)
        finally:
            self.cache.release_idle(chat_id)

    async def _stop_chat(
        self,
        data: P2ImMessageReceiveV1 | SimpleNamespace,
        *,
        advance_generation: bool = True,
    ) -> tuple[dict[str, ActiveCodexRun], bool, bool]:
        """停止本聊天的活跃及待处理任务、取消缓冲输入，并保留线程绑定。

        返回任务字典、是否全部确认停止、此前是否存在待处理工作组成的三元组。
        advance_generation 为真时推进消息代次，使旧输入失效；失败的停止允许重新尝试。
        """
        message = data.event.message
        chat_id = message.chat_id
        reaction_ids = set(self.cache.reaction_message_ids.get(chat_id, {}))
        async with self.cache.active_runs_guard:
            if advance_generation:
                self.cache.chat_generations[chat_id] = self.cache.chat_generations.get(chat_id, 0) + 1
            states = {
                state.run_id: state for state in self.cache.active_runs_by_message_id.values()
                if state.chat_id == chat_id
            }
            states.update({state.run_id: state for state in self.cache.all_runs.values()
                           if state.chat_id == chat_id})
            blocked = self.cache.blocked_chats.get(chat_id)
            if blocked:
                blocked.stop_task = None
                blocked.notified = False
                states[blocked.run_id] = blocked
            for state in states.values():
                state.interrupted = True
                if state.probe and not state.probe.stop_reason:
                    state.probe.stop_reason = "stopped"
            had_work = bool(states or self.cache.pending_chat_requests.get(chat_id) or chat_id in self.cache.buffered_events)
        await self.cancel_buffer(chat_id)
        results = await asyncio.gather(*(self.runtime._interrupt_run(state) for state in states.values()))
        succeeded = all(results)
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                await self.runtime._clear_reaction(chat_id, reaction_ids)
        except Exception:
            logger.exception("Reaction cleanup failed after stopping: chat_id=%s", chat_id)
        return states, succeeded, had_work

    async def processing_stop(self, data: P2ImMessageReceiveV1 | SimpleNamespace) -> None:
        """执行聊天级停止，并按停止确认结果和此前是否有任务发送一次状态卡片。

        已经全部通知过的任务不重复发送；交付失败仅记录日志，不据此重放停止操作。
        """
        states, succeeded, had_work = await self._stop_chat(data)
        message = data.event.message
        chat_id = message.chat_id
        key = "stopFailed" if not succeeded else "stopRequested" if had_work else "stopIdle"
        target_id = chat_id if message.chat_type == "group" else data.event.sender.sender_id.union_id
        if states and all(state.notified for state in states.values()):
            return
        for state in states.values():
            state.notified = True
        try:
            async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                await self.runtime.send_card(union_id=target_id, content=CONFIG["messages"][key])
            for state in states.values():
                if state.probe:
                    state.probe.record("notification_sent", messageKey=key)
        except Exception:
            logger.exception("Stop card delivery failed or its outcome is uncertain: chat_id=%s", chat_id)

    async def history_options(
        self,
        data: P2ImMessageReceiveV1 | SimpleNamespace,
    ) -> list[HistoryOption]:
        """按可信事件中的私聊用户或群 chat_id 获取历史会话，转换为下拉选项列表。

        返回数据库排序后的全部选项，数量限制由调用方应用；本函数不验证事件身份真实性。
        """
        message = data.event.message
        target_id = message.chat_id if message.chat_type == "group" else data.event.sender.sender_id.union_id
        return [
            {"label": record.thread_name or "Untitled session", "value": record.thread_id,
             "updated_at": record.updated_at}
            for record in await list_sessions(target_id)
        ]

    async def processing_history(self, data: P2ImMessageReceiveV1 | SimpleNamespace) -> None:
        """仅私聊展示个人历史；不停止任务、不修改活跃绑定。"""
        if data.event.message.chat_type != "p2p":
            return
        user_id = data.event.sender.sender_id.union_id
        if not user_id:
            logger.error("History card lacks a trusted union_id")
            return
        card = None
        try:
            # list_sessions already orders by updated_at DESC, thread_id DESC; sort before applying the total limit.
            options = (await self.history_options(data))[:CONFIG["messaging"].get("sessionHistoryLimit", 30)]
            card = self.history_cards.create(user_id, data.event.message.chat_id, options)
            card.message_id = await interactive.send_interactive_card(user_id, interactive.build_history_card(card))
        except Exception:
            if card is not None:
                self.history_cards.cards.pop(card.token, None)
            logger.exception("Failed to load or send the history card")
            await interactive.send_interactive_card(user_id, interactive.status_card("Failed to load or send session history. Send /history again."))

    async def processing_history_action(self, data: P2CardActionTrigger) -> None:
        """主事件循环内校验、去重并串行恢复；SDK 回调不等待恢复完成。"""
        try:
            card = self.history_cards.resolve(data)
        except (ValueError, AttributeError):
            # Unvalidated callbacks must not be used to send messages to arbitrary users.
            logger.warning("History card callback rejected: expired context, context mismatch, or duplicate action")
            return
        value = data.event.action.value
        try:
            if value.get("action") == "history_page":
                page = value.get("page")
                if (data.event.action.tag != "button" or type(page) is not int
                        or abs(page - card.page) != 1
                        or not 0 <= page < (len(card.options) + interactive.PAGE_SIZE - 1) // interactive.PAGE_SIZE):
                    raise ValueError("Invalid history page number")
                card.busy = True
                updated = replace(card, page=page, revision=card.revision + 1, busy=False)
                await interactive.update_interactive_card(card.message_id, interactive.build_history_card(updated))
                self.history_cards.cards[card.token] = updated
                return
            thread_id = interactive.confirmed_thread_id(data, card)
        except ValueError as exc:
            await interactive.send_interactive_card(card.user_id, interactive.status_card(str(exc)))
            return
        except Exception:
            logger.exception("Failed to change the history card page")
            card.busy = False
            await interactive.send_interactive_card(card.user_id, interactive.status_card("Failed to change the page. Send /history again."))
            return
        card.busy = True
        # Consume each card confirmation once; use /history again after failure so callback redelivery does not stop the task again.
        card.finished = True
        try:
            with self.cache.hold(card.chat_id):
                result = await self.processing_history_restore(card.message_event(), thread_id)
            label = next(item["label"] for item in card.visible_options if item["value"] == thread_id)
            text = f"Activated: {label}" if result["ok"] else "Activation failed and the current thread binding was not changed. Send /history again and retry."
            body = interactive.status_card(text)
            try:
                await interactive.update_interactive_card(card.message_id, body)
            except Exception:
                logger.exception("Failed to update the activation result card; restoration will not be retried")
                await interactive.send_interactive_card(card.user_id, body)
        finally:
            card.busy = False

    def dispatch_history_action(
        self,
        dispatcher: EventDispatcher,
        data: P2CardActionTrigger,
    ) -> P2CardActionTriggerResponse:
        """同步 SDK 入口，只提交已知动作；不把入队成功报告为线程激活成功。"""
        action = getattr(getattr(data, "event", None), "action", None)
        value = getattr(action, "value", None)
        if not isinstance(value, dict) or value.get("action") not in {"activate_history", "history_page"}:
            return interactive.callback_response("No action was performed")
        try:
            # Perform a read-only check for immediate feedback on expired or forwarded cards; the main loop validates again before execution.
            self.history_cards.resolve(data)
        except ValueError as exc:
            return interactive.callback_response(str(exc), error=True)
        except AttributeError:
            return interactive.callback_response("Card callback is missing required identity or context", error=True)
        if not dispatcher.submit(self.processing_history_action, data, control=True):
            return interactive.callback_response("The current task is busy. Please try again later", error=True)
        return interactive.callback_response("Processing. Refer to the final result on the card")

    async def processing_history_restore(
        self,
        data: P2ImMessageReceiveV1 | SimpleNamespace,
        thread_id: str,
    ) -> HistoryRestoreResult:
        """串行恢复指定历史会话，返回包含 ok、content 及成功时 thread_id 的结果字典。

        在首次 await 前建立提交门禁；校验归属后停止旧任务，等待收尾并持锁切换绑定。
        目标已激活时直接成功；失败转为失败结果，调用方取消不取消受 shield 保护的恢复任务。
        """
        chat_id = data.event.message.chat_id
        target_id = chat_id if data.event.message.chat_type == "group" else data.event.sender.sender_id.union_id
        previous = self.cache.reset_tasks.get(chat_id)

        async def restore() -> HistoryRestoreResult:
            """等待前次切换，校验历史归属并在启动超时内完成停止和恢复，最后释放本次门禁。"""
            try:
                if previous is not None:
                    await asyncio.shield(previous)
                async with asyncio.timeout(settings()["startupTimeoutSeconds"]):
                    if not isinstance(thread_id, str) or not thread_id or await get_session(target_id, thread_id) is None:
                        raise ValueError("Session history does not exist or does not belong to the current user")
                    if await get_user_thread(target_id) == thread_id:
                        return {"ok": True, "thread_id": thread_id, "content": "Previous session restored"}
                    self.cache.chat_generations[chat_id] = self.cache.chat_generations.get(chat_id, 0) + 1
                    states, succeeded, _ = await self._stop_chat(data, advance_generation=False)
                    for state in states.values():
                        state.notified = True
                    if not succeeded:
                        raise RuntimeError("Current task termination is unconfirmed")
                    for state in states.values():
                        if state.task is not None:
                            await state.finished.wait()
                    async with await self.runtime._get_codex_lock(chat_id):
                        await self.runtime.codex.restore_session(target_id, thread_id)
                return {"ok": True, "thread_id": thread_id, "content": "Previous session restored"}
            except Exception:
                logger.exception("Failed to restore the previous session: chat_id=%s", chat_id)
                return {"ok": False, "content": "Failed to restore the previous session"}
            finally:
                if self.cache.reset_tasks.get(chat_id) is asyncio.current_task():
                    self.cache.reset_tasks.pop(chat_id, None)
                    self.cache.release_idle(chat_id)

        # Establish the gate before the first await so subsequent regular input waits for this switch to complete.
        task = asyncio.create_task(restore())
        self.cache.reset_tasks[chat_id] = task
        return await asyncio.shield(task)

    async def processing_new(self, data: P2ImMessageReceiveV1 | SimpleNamespace) -> None:
        """在首次 await 前推进消息代次并登记切换任务，阻止后续输入越过会话重置。

        等待前次切换、停止确认及旧任务收尾后持锁重置线程，并发送结果；调用方取消不取消内部重置任务。
        """
        chat_id = data.event.message.chat_id
        target_id = chat_id if data.event.message.chat_type == "group" else data.event.sender.sender_id.union_id
        previous = self.cache.reset_tasks.get(chat_id)
        self.cache.chat_generations[chat_id] = self.cache.chat_generations.get(chat_id, 0) + 1

        async def reset() -> None:
            """串行等待并停止旧任务，确认收尾后重置绑定和通知结果，finally 释放本次门禁。"""
            try:
                if previous is not None:
                    await asyncio.shield(previous)
                states, succeeded, _ = await self._stop_chat(data, advance_generation=False)
                for state in states.values():
                    state.notified = True
                key = "stopFailed"
                if succeeded:
                    try:
                        async with asyncio.timeout(settings()["startupTimeoutSeconds"]):
                            # A cancelled input worker must release its submission lock
                            # and finish cleanup before its binding can be replaced.
                            for state in states.values():
                                if state.task is not None:
                                    await state.finished.wait()
                            async with await self.runtime._get_codex_lock(chat_id):
                                await self.runtime.codex.reset_thread(target_id)
                        key = "newThreadCreated"
                    except Exception:
                        logger.exception("New-session reset failed: chat_id=%s", chat_id)
                        key = "newThreadFailed"
                async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                    await self.runtime.send_card(union_id=target_id, content=CONFIG["messages"][key])
            except Exception:
                logger.exception("New-session command handling or notification failed: chat_id=%s", chat_id)
            finally:
                if self.cache.reset_tasks.get(chat_id) is asyncio.current_task():
                    self.cache.reset_tasks.pop(chat_id, None)
                    self.cache.release_idle(chat_id)

        task = asyncio.create_task(reset())
        self.cache.reset_tasks[chat_id] = task
        await asyncio.shield(task)

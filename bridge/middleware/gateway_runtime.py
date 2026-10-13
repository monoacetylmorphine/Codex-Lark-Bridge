"""Shared gateway state, submission locks, runtime supervision, stop confirmation, and background resource cleanup."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from bridge.codex.thread_watchdog import probes, settings
from bridge.configs.loader import CONFIG
from bridge.utils.terminal_log import get_logger
from .message_collector import MessageBatch
from bridge.session.session_gateway import ActiveCodexRun, SessionCache

if TYPE_CHECKING:
    from bridge.codex.codex_execution import Codex

logger = get_logger("Message")


class GatewayRuntime:
    """All gateway components share the same cache and external service dependencies."""

    def __init__(
        self,
        cache: SessionCache,
        *,
        codex: type[Codex],
        send_card: Callable[..., Awaitable[str | None]],
        delete_reaction: Callable[..., Awaitable[bool]],
    ) -> None:
        """绑定网关共享缓存、Codex 入口和卡片及 reaction 请求依赖，并初始化残留任务集合。"""
        self.cache = cache
        self.codex = codex
        self.send_card = send_card
        self.delete_reaction = delete_reaction
        self.detached_tasks: set[asyncio.Task[Any]] = set()

    async def maintain(self, operation: Callable[[], Awaitable[None]]) -> None:
        """按既有检查间隔重试清理，单轮失败不终止维护任务。"""
        while True:
            await asyncio.sleep(settings()["checkIntervalSeconds"])
            try:
                await operation()
            except Exception:
                logger.exception("Background cleanup failed")

    def _observe_detached(self, task: asyncio.Task[Any]) -> None:
        """保留残留任务引用并消费异常；不等待不配合取消的协程。"""
        if task in self.detached_tasks:
            return
        self.detached_tasks.add(task)
        def done(completed: asyncio.Task[Any]) -> None:
            """移除已结束的残留任务引用，消费并记录非取消异常。"""
            self.detached_tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.error("Background cleanup task failed", exc_info=completed.exception())
        task.add_done_callback(done)

    def _release_run(self, state: ActiveCodexRun) -> None:
        """事件循环内无 await 的幂等释放，不能依赖网络或异步锁。"""
        if state.released:
            return
        state.released = True
        for mid, owner in list(self.cache.active_runs_by_message_id.items()):
            if owner is state:
                self.cache.active_runs_by_message_id.pop(mid, None)
        if self.cache.active_runs_by_chat.get(state.chat_id) is state:
            self.cache.active_runs_by_chat.pop(state.chat_id, None)
        self.cache.all_runs.pop(state.run_id, None)
        state.finished.set()
        state.probe.finish(state.probe.stop_reason or "completed")
        state.probe.record("released", cleanupTimedOut=state.probe.cleanup_timed_out)
        logger.info("Task released: run_id=%s, terminal=%s, cleanup_timeout=%s",
                    state.run_id, state.probe.terminal, state.probe.cleanup_timed_out)
        if self.cache.blocked_chats.get(state.chat_id) is not state:
            probes.pop(state.run_id, None)
        # Unfinished workers may still use cards/task; do not clear references early.
        if state.task is None or state.task.done():
            self.cache.finish_run(state)
        else:
            state.task.add_done_callback(lambda _: self.cache.finish_run(state))

    async def _cleanup_timeout(self, state: ActiveCodexRun) -> None:
        """收尾期限耗尽后，有界尝试关闭进程并唤醒等待者。"""
        if state.released:
            return
        state.probe.cleanup_timed_out = True
        state.probe.record("cleanup_timeout")
        logger.error("Task cleanup timed out: run_id=%s, terminal=%s", state.run_id, state.probe.terminal)
        state.interrupted = True
        if state.cards is not None:
            state.cards.close()
        if state.task is not None and not state.task.done():
            state.task.cancel()
        closing = asyncio.create_task(self.codex.force_close(state.run_id))
        self._observe_detached(closing)
        confirmed = False
        try:
            pending = {closing}
            if state.task is not None:
                pending.add(state.task)
            # Process shutdown and worker cancellation share a grace period; do not mistake unscheduled workers for leftovers.
            done, _ = await asyncio.wait(pending, timeout=settings()["cleanupTimeoutSeconds"])
            if closing in done and not closing.cancelled():
                confirmed = bool(closing.result())
        except Exception:
            logger.exception("Forced shutdown failed: run_id=%s", state.run_id)
        finally:
            if not closing.done():
                closing.cancel()
            state.detached = state.task is not None and not state.task.done()
            if state.detached:
                self._observe_detached(state.task)
            # Remaining workers may still hold the submission lock; new runs must not overlap them.
            if not confirmed or state.detached:
                self.cache.blocked_chats[state.chat_id] = state
            self._release_run(state)
        await self._notify_terminal(state, "cleanupTimeout")

    def _remember_message(
        self,
        journal: dict[str, dict[str, float | None]],
        chat_id: str,
        message_id: str,
    ) -> None:
        """委托缓存记录消息去重信息，并应用保留期限及容量限制。"""
        self.cache.remember(journal, chat_id, message_id)

    async def _register_active_run(
        self,
        batch: MessageBatch,
        state: ActiveCodexRun | None = None,
    ) -> ActiveCodexRun:
        """创建或更新运行的消息集合，在锁内登记消息归属并合并已收到的撤回标记。

        返回运行对象；这里只登记消息到运行的映射，不启动 Codex 或设置聊天当前运行。
        """
        state = state or ActiveCodexRun(
            run_id=uuid4().hex,
            chat_id=batch.chat_id,
            message_ids=frozenset(message.message_id for message in batch.messages),
        )
        state.message_ids = frozenset(message.message_id for message in batch.messages)
        async with self.cache.active_runs_guard:
            state.interrupted |= bool(state.message_ids & self.cache.recalled_message_ids)
            for message_id in state.message_ids:
                self.cache.active_runs_by_message_id[message_id] = state
                self.cache.recalled_message_ids.discard(message_id)
        return state

    async def _unregister_active_run(self, state: ActiveCodexRun) -> None:
        """在锁内移除仍属于本运行的消息映射，不影响已转交给其他运行的消息。"""
        async with self.cache.active_runs_guard:
            for message_id in state.message_ids:
                if self.cache.active_runs_by_message_id.get(message_id) is state:
                    self.cache.active_runs_by_message_id.pop(message_id, None)

    async def _run_was_interrupted(self, state: ActiveCodexRun) -> bool:
        """在活跃运行锁内读取本运行的中断标记。"""
        async with self.cache.active_runs_guard:
            return state.interrupted

    async def _start_codex_run(self, state: ActiveCodexRun) -> bool:
        """在锁内检查中断标记；允许启动时标记 codex_started 并返回 True，否则返回 False。"""
        async with self.cache.active_runs_guard:
            if state.interrupted:
                return False
            state.codex_started = True
            return True

    async def _get_codex_lock(self, chat_id: str) -> asyncio.Lock:
        """在保护锁内获取或创建聊天专属的提交锁。"""
        async with self.cache.codex_locks_guard:
            return self.cache.codex_locks.setdefault(chat_id, asyncio.Lock())

    @asynccontextmanager
    async def _submission_lock(self, chat_id: str) -> AsyncIterator[None]:
        """等待聊天重置完成并获取提交锁，持锁后复查是否出现新的重置任务。

        上下文期间登记缓存使用者，退出时释放锁，避免等待中的请求使用已被回收的会话锁。
        """
        with self.cache.hold(chat_id):
            lock = await self._get_codex_lock(chat_id)
            while True:
                reset = self.cache.reset_tasks.get(chat_id)
                if reset is not None:
                    await asyncio.shield(reset)
                await lock.acquire()
                if self.cache.reset_tasks.get(chat_id) is None:
                    break
                lock.release()
            try:
                yield
            finally:
                lock.release()

    async def _notify_terminal(self, state: ActiveCodexRun, key: str) -> None:
        """在请求超时内尝试发送一次终态通知，并记录已发送或结果不确定。

        发送前即标记 notified，异常仅记录日志，避免响应丢失后重复发送可能已交付的通知。
        """
        if state.notified:
            return
        state.notified = True
        try:
            async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                content = (CONFIG["messages"].get("cleanupTimeout", "Task cleanup timed out and delivery is unconfirmed. If subsequent tasks are blocked, use /stop to retry stopping.")
                           if key == "cleanupTimeout" else CONFIG["messages"][key])
                await self.send_card(state.target_id or state.chat_id, content)
            if state.probe:
                state.probe.record("notification_sent", messageKey=key)
        except Exception:
            # Delivery may have succeeded despite a lost response; don't resend blindly.
            logger.exception("Final card delivery failed or its outcome is uncertain: run_id=%s", state.run_id)
            if state.probe:
                state.probe.record("notification_uncertain", messageKey=key)

    async def _watch_run(self, state: ActiveCodexRun) -> None:
        """周期检查运行和收尾期限，必要时请求停止并发送超时通知。

        判定运行超时前可查询服务器终态，避免慢卡片误报；停止后继续监督收尾，
        收尾期限耗尽时交由强制清理流程处理。
        """
        async def stop_and_notify() -> None:
            """请求停止本运行，并按是否确认停止发送对应的超时通知。"""
            confirmed = await self._interrupt_run(state)
            await self._notify_terminal(state, "taskTimeout" if confirmed else "taskTimeoutUnconfirmed")

        while not state.finished.is_set():
            await asyncio.sleep(settings()["checkIntervalSeconds"])
            if state.finished.is_set():
                break
            probe = state.probe
            reason = probe.expired()
            if reason == "cleanup_timeout":
                await self._cleanup_timeout(state)
                return
            if reason:
                if state.codex_started:
                    status = await self.codex.completed_status(state.run_id)
                    if status:
                        probe.finish(status)
                        continue
                    # Completion or a user stop may have won during the status RPC.
                    if probe.terminal or probe.stop_reason:
                        continue
                state.interrupted = True
                probe.stop_reason = reason
                probe.begin_cleanup()
                state.timeout_task = asyncio.create_task(stop_and_notify())
                self._observe_detached(state.timeout_task)
                # Continue supervising cleanup after requesting stop; do not exit before the worker is released.
        if state.timeout_task is not None:
            await asyncio.wait({state.timeout_task}, timeout=settings()["cardRequestTimeoutSeconds"])

    async def _clear_reaction(
        self,
        chat_id: str,
        message_ids: frozenset[str] | set[str] | None = None,
    ) -> None:
        """固定本轮消息范围，跳过仍由执行任务持有的消息，登记并尝试删除其 reaction。

        在首次 await 前保存全部待删记录，取消或删除失败时保留后台重试所需信息。
        """
        # Fix the cleanup scope for this pass; messages arriving during network requests are handled by their own batch.
        keys = []
        for message_id, reaction_id in list(self.cache.reaction_message_ids.get(chat_id, {}).items()):
            if message_ids is not None and message_id not in message_ids:
                continue
            owner = self.cache.active_runs_by_message_id.get(message_id)
            if owner is not None and owner.task is not None and not owner.task.done():
                continue
            key = (chat_id, message_id, reaction_id)
            self.cache.queue_reaction(*key)
            keys.append(key)
        # Register all records before the first await so cancellation does not lose unattempted deletions.
        for key in keys:
            await self._delete_pending_reaction(key)

    async def _delete_pending_reaction(self, key: tuple[str, str, str]) -> None:
        """去重执行一次待处理 reaction 删除；成功清理匹配索引，失败或取消时安排退避重试。"""
        pending = self.cache.pending_reactions.get(key)
        if pending is None or key in self.cache.reactions_being_cleared:
            return
        chat_id, message_id, reaction_id = key
        self.cache.reactions_being_cleared.add(key)
        try:
            async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
                succeeded = await self.delete_reaction(message_id=message_id, reaction_id=reaction_id)
            if succeeded:
                self.cache.pending_reactions.pop(key, None)
                current = self.cache.reaction_message_ids.get(chat_id, {})
                if current.get(message_id) == reaction_id:
                    current.pop(message_id)
                    self.cache._reaction_times.pop((chat_id, message_id), None)
                    if not current:
                        self.cache.reaction_message_ids.pop(chat_id, None)
                return
        except Exception:
            logger.exception("Message reaction cleanup error: chat_id=%s", chat_id)
        finally:
            self.cache.reactions_being_cleared.discard(key)
            if self.cache.pending_reactions.get(key) is pending:
                # Source: the backoff policy chosen for this fix, not a Lark platform limit.
                delays = (5, 30, 120, 600)
                pending.due = time.monotonic() + delays[min(pending.attempts, len(delays) - 1)]
                pending.attempts += 1

    async def _retry_reactions(self) -> None:
        """每轮仅重试一条已到期的 reaction 删除记录，避免占满正常消息请求容量。"""
        # Process at most one per pass so failed retries do not crowd out normal message requests.
        for key, pending in list(self.cache.pending_reactions.items()):
            if pending.due <= time.monotonic():
                await self._delete_pending_reaction(key)
                return

    async def _interrupt_run(self, state: ActiveCodexRun) -> bool:
        """为同一运行复用一个受 shield 保护的停止任务，返回是否确认停止。

        未确认时保留聊天阻塞状态；后续 /stop 可由调用方清空 stop_task 后重新尝试。
        本函数不自动重试已有的失败停止任务。
        """
        caller = asyncio.current_task()

        async def stop() -> bool:
            """请求并记录停止确认，维护聊天阻塞状态，随后取消非当前调用者的执行任务。"""
            confirmed = not state.codex_started
            if state.probe:
                state.probe.begin_cleanup()
                state.probe.stage("stopping")
            try:
                if state.codex_started:
                    confirmed = await self.codex.interrupt_and_confirm(state.run_id)
            except Exception:
                logger.exception("Failed to stop the task: run_id=%s", state.run_id)
                confirmed = False
            if state.detached and state.task is not None and not state.task.done():
                confirmed = False
            if state.probe:
                state.probe.stop_confirmed = confirmed
                state.probe.finish(state.probe.stop_reason or "stopped")
                state.probe.record("stop_result")
            if confirmed:
                if self.cache.blocked_chats.get(state.chat_id) is state:
                    self.cache.blocked_chats.pop(state.chat_id, None)
                    if state.finished.is_set():
                        probes.pop(state.run_id, None)
            elif not state.expired:
                self.cache.blocked_chats[state.chat_id] = state
            if state.task is not None and state.task is not caller and not state.task.done():
                state.task.cancel()
            return confirmed

        if state.stop_task is None:
            state.stop_task = asyncio.create_task(stop())
        return await asyncio.shield(state.stop_task)

    async def _expire_session_cache(self) -> None:
        """清理缓存返回的过期运行，尝试关闭 SDK 并有界等待执行任务结束。

        无论关闭结果如何，finally 都解除该过期运行的缓存和探针引用。
        """
        for state in self.cache.prune():
            state.expired = True
            state.interrupted = True
            task = state.task
            if task is not None and not task.done():
                task.cancel()
            try:
                await self.codex.discard_expired_run(state.run_id)
            except Exception:
                logger.exception("Failed to close the expired task; cache cleanup still follows the 24-hour retention limit: run_id=%s", state.run_id)
            finally:
                try:
                    if task is not None and not task.done():
                        try:
                            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                                await asyncio.shield(task)
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():
                                raise
                        except Exception:
                            logger.exception("Expired task cleanup is incomplete: run_id=%s", state.run_id)
                finally:
                    self.cache.expire_run(state)
                    probes.pop(state.run_id, None)
                    logger.warning("Cleared task cache older than 24 hours: run_id=%s", state.run_id)

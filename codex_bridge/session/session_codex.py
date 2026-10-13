"""Session restoration, reset, and synchronization of history names and timestamps."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from codex_bridge.codex import thread_manager
from codex_bridge.codex.thread_watchdog import settings
from codex_bridge.session import session_history
from codex_bridge.utils.terminal_log import get_logger

if TYPE_CHECKING:
    from openai_codex import AsyncThread, InputItem

logger = get_logger("Codex")


async def _initialize_session_name(
    user_id: str,
    thread: AsyncThread,
    prompt: str | list[InputItem],
) -> None:
    """为已登记但尚未完成命名同步的线程设置首次名称，并回填 SDK 更新时间。

    未登记的旧线程不补造名称；仅附件线程可在首次文本到达时认领候选名称，已同步的记录直接返回。
    设置名称后再次读取确认，不一致时抛出 RuntimeError。
    """
    record = await session_history.get_session(user_id, thread.id)
    if record is None:
        return
    if record.thread_name is None:
        record = await session_history.claim_session_name(user_id, thread.id, prompt)
    if record.updated_at is not None:
        return
    metadata = (await thread.read(include_turns=False)).thread
    if record.thread_name is not None and metadata.name != record.thread_name:
        await thread.set_name(record.thread_name)
        metadata = (await thread.read(include_turns=False)).thread
        if metadata.name != record.thread_name:
            raise RuntimeError("Thread name was not saved")
    await session_history.update_session_time(user_id, thread.id, metadata.updated_at)


async def _sync_session_time(user_id: str, thread: AsyncThread) -> None:
    """元数据同步失败单独记录，不把已完成的模型任务改判为执行失败。"""
    try:
        async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
            record = await session_history.get_session(user_id, thread.id)
            if record is not None:
                # After steer naming fails, timestamp synchronization must not mark naming as complete.
                if record.thread_name is not None and record.updated_at is None:
                    logger.warning("Session history naming is not complete: user_id=%s, thread_id=%s", user_id, thread.id)
                    return
                metadata = (await thread.read(include_turns=False)).thread
                await session_history.update_session_time(user_id, thread.id, metadata.updated_at)
    except Exception:
        logger.exception("Failed to synchronize session history timestamps: user_id=%s, thread_id=%s", user_id, thread.id)


class CodexSession:
    """SDK lifecycle and shared runtime state composed by Codex."""

    @classmethod
    async def restore_session(cls, user_id: str, thread_id: str) -> None:
        """恢复历史并替换活跃绑定；调用方必须先停止任务并持有提交锁。"""
        if await session_history.get_session(user_id, thread_id) is None:
            raise ValueError("Session history does not exist or does not belong to the current user")
        if await thread_manager.get_user_thread(user_id) == thread_id:
            return
        async with cls._session(None) as codex:
            thread = await codex.thread_unarchive(thread_id)
            await _initialize_session_name(user_id, thread, "")
            metadata = (await thread.read(include_turns=False)).thread
        # Write to the database only after the SDK operation and close succeed; do not archive or replace the original thread.
        await session_history.update_session_time(user_id, thread_id, metadata.updated_at)
        await thread_manager.set_user_thread(user_id, thread_id)

    @classmethod
    async def reset_thread(cls, user_id: str) -> None:
        """归档当前绑定线程，待控制会话成功关闭后将绑定置为 None；没有绑定时仍持久化空绑定。

        不创建新线程，也不解析 prompt；调用方应先停止旧任务并保证提交隔离，失败时不继续清空绑定。
        """
        from codex_bridge.codex.codex_execution import _retry_on_overload_async

        thread_id = await thread_manager.get_user_thread(user_id)
        if thread_id:
            async with cls._session(None) as codex:
                await _retry_on_overload_async(
                    lambda: codex.thread_archive(thread_id=thread_id),
                    operation_name="thread_archive",
                )
        await thread_manager.set_user_thread(user_id, None)

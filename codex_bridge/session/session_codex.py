"""Session restoration, reset, and synchronization of history names and timestamps."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from codex_bridge.codex import thread_manager
from codex_bridge.codex.thread_watchdog import settings
from codex_bridge.session import session_history
from codex_bridge.utils.terminal_log import get_logger

if TYPE_CHECKING:
    from openai_codex import AsyncThread

logger = get_logger("Codex")


async def _initialize_session_name(
    user_id: str,
    thread: AsyncThread,
) -> None:
    """已登记线程在 turn 启动前尝试捕获 Codex 生成的名称与时间；尚未生成时留待完成时同步。"""
    await session_history.sync_session_index(user_id, thread.id)


async def _sync_session_time(user_id: str, thread: AsyncThread) -> None:
    """元数据同步失败单独记录，不把已完成的模型任务改判为执行失败。"""
    try:
        async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
            await session_history.sync_session_index(user_id, thread.id)
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
            _ = await codex.thread_unarchive(thread_id)
        # Write to the database only after the SDK operation and close succeed; do not archive or replace the original thread.
        await session_history.sync_session_index(user_id, thread_id)
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

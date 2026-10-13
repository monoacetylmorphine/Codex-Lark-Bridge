"""SDK lifecycle, runtime state, and stop and steer controls."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from openai_codex import AsyncCodex, InvalidRequestError, LocalImageInput

from codex_bridge.session import session_codex
from .thread_watchdog import probes
from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger
from .thread_watchdog import settings

if TYPE_CHECKING:
    import subprocess

    from openai_codex import AsyncThread, AsyncTurnHandle, InputItem

    from .codex_execution import RunEvent

logger = get_logger("Codex")


def _sdk_process(manager: AsyncCodex | None) -> subprocess.Popen[bytes] | None:
    """从 SDK 私有属性路径获取所属子进程；路径不存在时返回 None。"""
    return getattr(getattr(getattr(manager, "_client", None), "_sync", None), "_proc", None)


@dataclass
class LiveTurn:
    thread: AsyncThread
    handle: AsyncTurnHandle
    model: str
    provider: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False
    user_id: str | None = None


class CodexRuntime:
    """Central runtime indexes shared by Codex execution and session methods."""
    _active_turns: dict[str, AsyncTurnHandle] = {}
    _live_turns: dict[str, LiveTurn] = {}
    _pending_interrupts: set[str] = set()
    _turns_guard = asyncio.Lock()
    _clients: dict[str, AsyncCodex] = {}
    _processes: dict[str, subprocess.Popen[bytes]] = {}
    _closed_runs: set[str] = set()
    _initializers: dict[str, asyncio.Task[AsyncCodex]] = {}
    # Retain only failed control-session cleanup; do not replay archive or restore operations.
    _control_cleanup: dict[str, tuple[float, float]] = {}

    @classmethod
    async def completed_status(cls, run_id: str) -> str | None:
        """在有界等待内读取当前 turn 的终态，避免慢卡片造成的完成事件延迟被判为超时。

        返回 completed、failed 或 interrupted；无活跃句柄、未找到终态或读取失败时返回 None。
        """
        live = cls._live_turns.get(run_id)
        if live is None:
            return None
        try:
            async with asyncio.timeout(min(1, settings()["interruptGraceSeconds"])), live.lock:
                response = await live.thread.read(include_turns=True)
                for turn in response.thread.turns:
                    status = getattr(turn.status, "value", turn.status)
                    if turn.id == live.handle.id and status in {"completed", "failed", "interrupted"}:
                        return status
        except Exception:
            logger.debug("Unable to read the final status: run_id=%s", run_id, exc_info=True)
        return None

    @classmethod
    @asynccontextmanager
    async def _session(cls, run_id: str | None) -> AsyncGenerator[AsyncCodex]:
        """创建并登记 SDK 会话，在上下文退出时关闭并确认所属进程回收。

        run_id 为 None 时生成独立控制会话标识，关闭失败的控制会话登记为后台清理任务。
        初始化及清理受 shield 保护，防止取消丢失进程；清理结束后仍向调用方传播取消。
        """
        control = run_id is None
        run_id = run_id or f"control-{uuid4().hex}"
        manager = AsyncCodex()
        initializing = asyncio.create_task(manager.__aenter__())
        initializing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        cls._clients[run_id] = manager
        cls._initializers[run_id] = initializing

        try:
            # Cancellation must not lose a subprocess that start() creates late in a worker thread.
            client = await asyncio.shield(initializing)
            proc = _sdk_process(manager)
            if proc is not None:
                cls._processes[run_id] = proc
            yield client
        finally:
            # Repeated outer cancellation must not interrupt process cleanup; propagate cancellation to the caller afterward.
            cancelled = bool(asyncio.current_task().cancelling())
            closing = asyncio.create_task(cls._close_session(run_id, manager, initializing))
            try:
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception:
                        break
                closing.result()
            finally:
                if control:
                    if run_id in cls._clients or run_id in cls._initializers:
                        now = time.monotonic()
                        cls._control_cleanup[run_id] = (now, now)
                    else:
                        cls._closed_runs.discard(run_id)
                if cancelled:
                    raise asyncio.CancelledError

    @classmethod
    async def _close_session(
        cls,
        run_id: str,
        manager: AsyncCodex,
        initializing: asyncio.Task[AsyncCodex],
    ) -> None:
        """在收尾超时内关闭 SDK 会话并确认初始化及进程退出，失败时尝试强制关闭。

        确认成功后移除客户端、进程和初始化索引；仍无法确认时抛出 RuntimeError 并保留清理信息。
        """
        # close() may clear the SDK process reference; save it first.
        proc = _sdk_process(manager)
        if proc is not None:
            cls._processes[run_id] = proc
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                await manager.__aexit__(None, None, None)
                if not initializing.done():
                    raise RuntimeError("SDK initialization still processing, closing unverified")
                late_proc = _sdk_process(manager)
                if proc is None and late_proc is not None:
                    proc = late_proc
                    cls._processes[run_id] = proc
                if proc is not None:
                    await asyncio.to_thread(proc.wait, timeout=1)
                    if proc.poll() is None:
                        raise RuntimeError("SDK processes are exited, but unverified")
        except (Exception, asyncio.CancelledError):
            logger.exception("Closing Codex client Error: run_id=%s", run_id)
            if not await cls.force_close(run_id):
                raise RuntimeError("Processes exit still unverified after Codex client was closed")
        cls._clients.pop(run_id, None)
        cls._processes.pop(run_id, None)
        cls._initializers.pop(run_id, None)

    @classmethod
    async def cleanup_control_sessions(cls) -> None:
        """每轮处理一个到期的控制会话清理任务；失败延后重试，超过保留期限则尝试关闭并释放索引。"""
        from codex_bridge.session.session_gateway import RETENTION_SECONDS
        now = time.monotonic()
        for run_id, (created, due) in list(cls._control_cleanup.items()):
            if now < due:
                continue
            try:
                if now - created >= RETENTION_SECONDS:
                    await cls.discard_expired_run(run_id)
                elif not await cls.force_close(run_id):
                    # Source: initial retry policy for failed control-session cleanup, not an SDK limit.
                    cls._control_cleanup[run_id] = (created, time.monotonic() + 30)
                    return
                cls._control_cleanup.pop(run_id, None)
                cls._closed_runs.discard(run_id)
            except Exception:
                logger.exception("Background control-session cleanup failed: run_id=%s", run_id)
                if now - created >= RETENTION_SECONDS:
                    cls._control_cleanup.pop(run_id, None)
                else:
                    cls._control_cleanup[run_id] = (created, time.monotonic() + 30)
            return

    @classmethod
    async def force_close(cls, run_id: str) -> bool:
        """尝试关闭本次运行的 SDK 客户端或进程，并返回是否确认回收。

        关闭前保存进程引用，关闭异常时尝试 kill；初始化尚未完成时不报告确认成功。
        成功后清除客户端、进程和初始化索引；不保证远程工具或脱离进程的后代已停止。
        """
        cls._closed_runs.add(run_id)
        client = cls._clients.get(run_id)
        proc = cls._processes.get(run_id)
        initializing = cls._initializers.get(run_id)
        if client is None and proc is None:
            return run_id not in cls._active_turns and (initializing is None or initializing.done())
        if proc is None:
            proc = _sdk_process(client)
            if proc is not None:
                cls._processes[run_id] = proc
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                if client is not None:
                    await client.close()
                elif proc is not None:
                    proc.terminate()
                if proc is not None:
                    await asyncio.to_thread(proc.wait, timeout=1)
            confirmed = ((proc is not None and proc.poll() is not None)
                         or (proc is None and initializing is not None and initializing.done()))
            if initializing is not None and not initializing.done():
                confirmed = False
        except Exception:
            logger.exception("Force-closing Codex: run_id=%s", run_id)
            confirmed = False
            if proc is not None:
                try:
                    proc.kill()
                    async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                        await asyncio.to_thread(proc.wait, timeout=1)
                    confirmed = proc.poll() is not None
                except Exception:
                    logger.exception("Codex process exit is unconfirmed: run_id=%s", run_id)
        probe = probes.get(run_id)
        if initializing is not None and not initializing.done():
            confirmed = False
        if probe:
            probe.record("force_close", processExited=confirmed)
        if confirmed:
            cls._processes.pop(run_id, None)
            cls._clients.pop(run_id, None)
            cls._initializers.pop(run_id, None)
        return confirmed

    @classmethod
    async def interrupt_and_confirm(cls, run_id: str) -> bool:
        """请求中断当前 turn，并在宽限期内轮询所属线程，确认 idle 或运行已无待清理资源。

        未启动时登记中断请求以阻止后续提交；超时、检查失败或运行已被关闭时尝试强制回收。
        返回停止是否确认，不将发出中断请求本身视为停止成功。
        """
        cls._pending_interrupts.add(run_id)
        if run_id in cls._closed_runs:
            return await cls.force_close(run_id)
        try:
            async with asyncio.timeout(settings()["interruptGraceSeconds"]):
                while True:
                    live = cls._live_turns.get(run_id)
                    if live is not None:
                        # Keep the owning client alive while checking its status.
                        async with live.lock:
                            if not live.closed:
                                await live.handle.interrupt()
                                while True:
                                    response = await live.thread.read()
                                    status = response.thread.status.root.type
                                    logger.info("Stop confirmed: run_id=%s, thread_id=%s, status=%s",
                                                run_id, live.thread.id, status)
                                    probe = probes.get(run_id)
                                    if probe:
                                        probe.record("stop_status", threadStatus=status)
                                    if status == "idle":
                                        return True
                                    await asyncio.sleep(0.1)
                    probe = probes.get(run_id)
                    if (probe and probe.terminal and run_id not in cls._clients
                            and run_id not in cls._processes and run_id not in cls._initializers):
                        return True
                    if (run_id not in cls._clients and run_id not in cls._active_turns
                            and run_id not in cls._processes and run_id not in cls._initializers):
                        # Startup has not reached a client; pending flag prevents submission.
                        return True
                    await asyncio.sleep(0.1)
        except Exception:
            logger.exception("Interrupt or stop confirmation timed out or failed: run_id=%s", run_id)
        return await cls.force_close(run_id)

    @classmethod
    async def steer(
        cls,
        run_id: str,
        prompt: str | list[InputItem],
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> RunEvent:
        """在所属运行的锁内核对线程状态，并向仍活跃的 turn 提交补充输入。

        返回 idle、cancelled、steered 或 error 事件；不在运行中切换模型或 provider。
        只有明确未接受输入且线程已空闲时返回 idle，不重放结果不确定的请求。
        外层锁等待超时及取消可能直接传播，不一定转换为 error 事件。
        """
        from .codex_execution import _error_event

        live = cls._live_turns.get(run_id)
        if live is None:
            return {"type": "idle"}
        async with asyncio.timeout(settings()["interruptGraceSeconds"]), live.lock:
            if cancelled():
                return {"type": "cancelled"}
            if live.closed or run_id in cls._pending_interrupts:
                return {"type": "idle"}
            try:
                response = await live.thread.read()
                if cancelled():
                    return {"type": "cancelled"}
                status = response.thread.status.root.type
                logger.info("Thread status: thread_id=%s, turn_id=%s, status=%s",
                            live.thread.id, live.handle.id, status)
                if status == "idle":
                    return {"type": "idle"}
                if status != "active":
                    raise RuntimeError(f"Thread state does not accept input: {status}")
                if run_id in cls._pending_interrupts:
                    return {"type": "idle"}
                # Steering cannot change model/provider. Do not silently send
                # images to the configured text-only route.
                image_route = CONFIG["codex"]["models"]["image"]
                if (isinstance(prompt, list)
                        and any(isinstance(item, LocalImageInput) for item in prompt)
                        and (live.model, live.provider) !=
                        (image_route["model"], image_route["provider"])):
                    return {"type": "error", "content": CONFIG["messages"]["steerImageUnsupported"]}
                if live.user_id is not None:
                    await session_codex._initialize_session_name(live.user_id, live.thread, prompt)
                try:
                    result = await live.handle.steer(prompt)
                except InvalidRequestError as exc:
                    # These errors explicitly mean the input was NOT accepted.
                    # Other invalid requests (e.g. review/compact) must surface.
                    if (exc.message == "no active turn to steer"
                            or exc.message.startswith("expected active turn id `")):
                        response = await live.thread.read()
                        if response.thread.status.root.type == "idle":
                            return {"type": "idle"}
                    raise
                if result.turn_id != live.handle.id:
                    raise RuntimeError("steer returned a different turn ID")
                logger.info("Steer accepted: thread_id=%s, turn_id=%s", live.thread.id, result.turn_id)
                return {"type": "steered"}
            except Exception as exc:
                return _error_event(exc, operation="turn_steer")

    @classmethod
    async def interrupt(cls, run_id: str) -> bool:
        """登记中断意图，并在已有可用 turn 句柄时发送中断请求。

        返回 True 仅表示请求已发送；尚未启动或运行已关闭时返回 False，保留中断标记。
        """
        async with cls._turns_guard:
            cls._pending_interrupts.add(run_id)
            handle = cls._active_turns.get(run_id)

        if handle is None or run_id in cls._closed_runs:
            return False

        await handle.interrupt()
        return True

    @classmethod
    async def forget_run(cls, run_id: str) -> None:
        """移除已结束运行的 turn、活跃状态及中断标记；尚有客户端或进程时保留关闭标记。"""
        async with cls._turns_guard:
            cls._active_turns.pop(run_id, None)
            cls._live_turns.pop(run_id, None)
            cls._pending_interrupts.discard(run_id)
            if run_id not in cls._clients and run_id not in cls._processes:
                cls._closed_runs.discard(run_id)

    @classmethod
    async def discard_expired_run(cls, run_id: str) -> None:
        """为调用方判定已过期的运行尝试有界关闭，并在 finally 中移除运行索引。

        即使关闭失败也释放缓存引用；未完成的初始化另注册回调处理迟到的子进程。
        本函数不自行检查运行年龄，过期判断由调用方负责。
        """
        manager = cls._clients.get(run_id)
        initializing = cls._initializers.get(run_id)
        if initializing is not None and not initializing.done():
            def stop_late_process(task: asyncio.Task[AsyncCodex]) -> None:
                """消费迟到初始化的异常，并尝试 kill 尚存的 SDK 子进程，不恢复已释放的索引。"""
                # Attempt process termination even when initialization returns late from a thread; do not restore runtime indexes.
                if not task.cancelled():
                    task.exception()
                proc = _sdk_process(manager)
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            initializing.add_done_callback(stop_late_process)
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                if not await cls.force_close(run_id):
                    logger.error("Expired task process exit is unconfirmed: run_id=%s", run_id)
        finally:
            for mapping in (cls._active_turns, cls._live_turns, cls._clients,
                            cls._processes, cls._initializers):
                mapping.pop(run_id, None)
            cls._pending_interrupts.discard(run_id)
            cls._closed_runs.discard(run_id)

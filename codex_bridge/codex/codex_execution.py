"""Model routing, retries, event stream conversion, and usage recording."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NotRequired, TYPE_CHECKING, TypeVar, TypedDict
from uuid import uuid4

from openai_codex import (
    InvalidParamsError, InvalidRequestError, LocalImageInput,
    MethodNotFoundError, Sandbox, TextInput, is_retryable_error,
)
from openai_codex.types import TurnStatus

from codex_bridge.codex.codex_workspace import prepare_workspace, workspace_environment
from codex_bridge.utils.token_usage import SavingLog, finalize_usage
from . import thread_manager
from codex_bridge.session import session_codex, session_history
from .codex_runtime import CodexRuntime, LiveTurn
from codex_bridge.session.session_codex import CodexSession
from .thread_watchdog import RunProbe, probes, should_log_event, summarize_event
from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger
from .thread_watchdog import settings

if TYPE_CHECKING:
    from openai_codex import InputItem

    from codex_bridge.services.lark.lark_message_card import CardSteer

T = TypeVar("T")


__all__ = ["Codex", "LiveTurn"]

logger = get_logger("Codex")


class RunEvent(TypedDict):
    """Dictionary events between the SDK and gateway; optional fields preserve existing defaults."""

    type: str
    content: NotRequired[str]
    code: NotRequired[str]
    item_id: NotRequired[str]
    thread_id: NotRequired[str]
    turn_id: NotRequired[str]
    phase: NotRequired[str | None]
    control: NotRequired[CardSteer]


def _tool_progress(item: Any, *, completed: bool) -> str | None:
    """将已识别的工具事件转换为名称与执行状态文案，其他 item 返回 None。

    completed 表示收到完成事件，不代表执行成功；失败标志优先于成功标志，
    无可靠结果标志时显示状态未知。仅读取名称和状态字段，不展开参数或结果正文。
    """
    if item.type not in {
        "commandExecution", "mcpToolCall", "dynamicToolCall", "fileChange",
        "collabAgentToolCall", "webSearch", "imageView", "imageGeneration", "sleep",
    }:
        return None
    tool = getattr(item, "tool", None)
    name = getattr(tool, "value", tool) or item.type
    prefix = (getattr(item, "server", None) if item.type == "mcpToolCall"
              else getattr(item, "namespace", None) if item.type == "dynamicToolCall" else None)
    if prefix:
        name = f"{prefix}.{name}"
    content = f"Agent executing {name} tool"
    if not completed:
        return content + "\n"

    status = getattr(item, "status", None)
    status = getattr(status, "value", status)
    exit_code = getattr(item, "exit_code", None) if item.type == "commandExecution" else None
    success = getattr(item, "success", None) if item.type == "dynamicToolCall" else None
    failed = (status in {"failed", "declined", "interrupted"}
              or (exit_code is not None and exit_code != 0) or success is False
              or getattr(item, "error", None) is not None
              or getattr(item, "failure", None) is not None)
    if failed:
        suffix = "failed"
    elif status == "completed" or exit_code == 0 or success is True:
        suffix = "succeeded"
    else:
        # SDK items such as webSearch and imageView have no success flag; completion does not imply success.
        suffix = "finished (status unknown)"
    return content + ": " + suffix + "\n"


def _resolve_model(prompt: str | list[InputItem]) -> tuple[str, str]:
    """按输入类型返回模型名称和 provider：纯文本走文本路由，包含本地图片走图片路由，其余列表走多模态路由。"""
    if isinstance(prompt, str):
        route = "text"
    elif isinstance(prompt, list):
        route = "text" if prompt and all(isinstance(item, TextInput) for item in prompt) else "multimodal"
        if any(isinstance(item, LocalImageInput) for item in prompt):
            route = "image"
    else:
        raise TypeError(f"Not Supported Prompt Type: {type(prompt).__name__}")
    selection = CONFIG["codex"]["models"][route]
    return selection["model"], selection["provider"]


def _sandbox_from_config() -> Sandbox:
    """将配置中的 sandbox 名称转换为 SDK 枚举；不支持的名称抛出 RuntimeError。"""
    attribute = CONFIG["codex"]["sandbox"].replace("-", "_")
    try:
        return getattr(Sandbox, attribute)
    except AttributeError as exc:
        raise RuntimeError(f"Not Supported Codex Sandbox: {CONFIG['codex']['sandbox']}") from exc


async def _retry_on_overload_async(
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str,
    max_attempts: int | None = None,
    initial_delay_s: float | None = None,
    max_delay_s: float | None = None,
    jitter_ratio: float | None = None,
    backoff_multiplier: float | None = None,
) -> T:
    """按配置或显式覆盖值重试 SDK 判定可重试的异常，返回操作结果。

    max_attempts 包含首次尝试；等待时间采用指数退避并加入随机抖动。
    不可重试异常和最后一次失败原样抛出，取消不重试。
    """
    retry_config = CONFIG["codex"]["retry"]
    max_attempts = retry_config["maxAttempts"] if max_attempts is None else max_attempts
    initial_delay_s = retry_config["initialDelaySeconds"] if initial_delay_s is None else initial_delay_s
    max_delay_s = retry_config["maxDelaySeconds"] if max_delay_s is None else max_delay_s
    jitter_ratio = retry_config["jitterRatio"] if jitter_ratio is None else jitter_ratio
    backoff_multiplier = retry_config["backoffMultiplier"] if backoff_multiplier is None else backoff_multiplier
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    delay = initial_delay_s
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt >= max_attempts or not is_retryable_error(exc):
                raise

            jitter = delay * jitter_ratio
            sleep_for = min(max_delay_s, delay) + random.uniform(-jitter, jitter)
            logger.warning(
                "Codex Transient Error, to Retry: operation=%s, attempt=%s/%s, error=%s",
                operation_name, attempt, max_attempts, exc,
            )
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            delay = min(max_delay_s, delay * backoff_multiplier)

    raise RuntimeError("unreachable")


def _error_event(exc: Exception, *, operation: str) -> RunEvent:
    """将异常分类为用户可见的 error 事件，并记录操作名及原始错误；文案取自配置。"""
    if is_retryable_error(exc):
        code = "retry_exhausted"
        content = CONFIG["messages"]["codexBusy"]
    elif isinstance(exc, (InvalidParamsError, InvalidRequestError)):
        code = "invalid_request"
        content = CONFIG["messages"]["invalidRequest"]
    elif isinstance(exc, MethodNotFoundError):
        code = "unsupported_method"
        content = CONFIG["messages"]["unsupportedMethod"]
    else:
        code = "codex_error"
        content = CONFIG["messages"]["codexFailure"]

    logger.error("Codex Execute Failure: operation=%s, code=%s, error=%s", operation, code, exc)
    return {"type": "error", "code": code, "content": content}


async def _save_turn_usage(
    log: dict[str, Any],
    *,
    duration_ms: int | None = None,
    finalize: bool = False,
) -> None:
    """在配置的收尾超时内尝试写入单条用量或回填耗时，不重复提交失败写入。

    写入异常仅记录日志；调用方取消时等待受保护的写入任务结束，再传播取消。
    """
    async def save() -> None:
        """执行一次有超时限制的写入或回填；异常仅记录，不重试可能已提交的事务。"""
        try:
            async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]):
                if finalize:
                    await finalize_usage(log["runId"], duration_ms)
                else:
                    await SavingLog(log=log)
        except Exception:
            # A commit may already have succeeded; do not retry and duplicate it.
            logger.exception("Token Usage Logs Saving Failure: user_id=%s, thread_id=%s",
                             log["userId"], log["threadId"])

    saving = asyncio.create_task(save())
    cancelled = False
    while not saving.done():
        try:
            await asyncio.shield(saving)
        except asyncio.CancelledError:
            cancelled = True
    saving.result()
    if cancelled:
        raise asyncio.CancelledError


class Codex(CodexSession, CodexRuntime):
    """Execute requests using shared session-management and runtime-control state."""

    @classmethod
    async def running(
        cls,
        user_id: str,
        prompt: str | list[InputItem],
        run_id: str | None = None,
        *,
        notify_started: bool = False,
    ) -> AsyncGenerator[RunEvent, None]:
        """准备用户工作区、恢复或创建线程，并启动一个 turn，逐项产出 RunEvent。

        启动前保存线程绑定及历史名称；notify_started 为真时在流开始前产出 started。
        可重试的启动操作按配置重试，流开始后不重新提交 turn；已捕获的业务异常转为
        error 事件，取消和未捕获异常向调用方传播。调用方应关闭生成器以释放 SDK 会话。
        """
        try:
            thread_id = await thread_manager.get_user_thread(user_id)
        except Exception as error:
            yield _error_event(error, operation="thread_binding_read")
            return

        model, model_provider = _resolve_model(prompt)

        workspace = Path(CONFIG["storage"]["workspaceRoot"]).expanduser() / user_id
        try:
            # The project default of 10 seconds supports mounted configurations that omit the new field.
            await prepare_workspace(workspace, CONFIG["codex"].get("gitInitTimeoutSeconds", 10))
        except Exception as error:
            yield _error_event(error, operation="workspace_init")
            return

        thread_config = {
            "cwd":str(workspace),
            # Source: Codex shell_environment_policy.set; override only the current user environment paths.
            "config": {f"shell_environment_policy.set.{key}": value
                       for key, value in workspace_environment(workspace).items()},
            "sandbox":_sandbox_from_config(),
            "model":model,
            "model_provider":model_provider,
        }
        # 来源：业务 JSON 的 codex.modelReasoningEffort；保留工作区环境变量配置。
        thread_config["config"]["model_reasoning_effort"] = CONFIG["codex"]["modelReasoningEffort"]

        if run_id in cls._pending_interrupts:
            yield {"type": "interrupted"}
            return
        async with cls._session(run_id) as codex:
            if run_id in cls._pending_interrupts:
                yield {"type": "interrupted"}
                return
            running_timestamp = datetime.now(timezone(timedelta(hours=CONFIG["runtime"]["timezoneOffsetHours"])))
            thread = None
            if thread_id:
                try:
                    thread = await _retry_on_overload_async(
                        lambda: codex.thread_resume(
                            thread_id=thread_id,
                            **thread_config,
                        ),
                        operation_name="thread_resume",
                    )
                except Exception as e:
                    if is_retryable_error(e) or isinstance(
                        e, (InvalidParamsError, MethodNotFoundError)
                    ):
                        yield _error_event(e, operation="thread_resume")
                        return

                    logger.warning("Restoring the thread (%s) failure, try to restore the thread from the archive.", e)

                    try:
                        thread = await _retry_on_overload_async(
                            lambda: codex.thread_unarchive(thread_id),
                            operation_name="thread_unarchive",
                        )
                        # Unarchive does not accept configuration; resume again to inject the current user dependency paths.
                        thread = await _retry_on_overload_async(
                            lambda: codex.thread_resume(thread_id=thread_id, **thread_config),
                            operation_name="thread_resume",
                        )
                    except Exception as error:
                        if is_retryable_error(error) or isinstance(
                            error, (InvalidParamsError, MethodNotFoundError)
                        ):
                            yield _error_event(error, operation="thread_unarchive")
                            return
                        logger.warning("Restoring the thread (%s) failure from the archive, try to create a new thread", error)
                        thread = None

            if thread_id is None or thread is None:
                try:
                    thread = await _retry_on_overload_async(
                        lambda: codex.thread_start(**thread_config),
                        operation_name="thread_start",
                    )
                except Exception as error:
                    yield _error_event(error, operation="thread_start")
                    return
                try:
                    await session_history.register_session(user_id, thread.id, prompt)
                except Exception as error:
                    yield _error_event(error, operation="session_history_register")
                    return
            logger.info("Codex : user_id=%s, thread_id=%s, prompt=%s", user_id, thread.id, prompt)
            # Save the binding before starting the turn so database failure cannot leave an executed request behind.
            try:
                await thread_manager.set_user_thread(user_id, thread.id)
            except Exception as error:
                yield _error_event(error, operation="thread_binding_write")
                return
            try:
                await session_codex._initialize_session_name(user_id, thread, prompt)
            except Exception as error:
                yield _error_event(error, operation="session_history_name")
                return
            try:
                if run_id in cls._pending_interrupts:
                    yield {"type": "interrupted"}
                    return
                handle = await _retry_on_overload_async(
                    lambda: thread.turn(input=prompt),
                    operation_name="turn_start",
                )
            except Exception as error:
                yield _error_event(error, operation="turn_start")
                return

            if run_id in cls._closed_runs:
                yield {"type": "interrupted"}
                return
            live = LiveTurn(thread, handle, model, model_provider, user_id=user_id)
            probe = probes.get(run_id)
            if probe:
                probe.thread_id = thread.id
                probe.turn_id = handle.id
                probe.last_activity = asyncio.get_running_loop().time()
                probe.stage("running")
            async with aclosing(cls._stream_turn(
                live, user_id, run_id, notify_started, running_timestamp, probe,
            )) as events:
                async for event in events:
                    yield event

    @classmethod
    async def _stream_turn(
        cls,
        live: LiveTurn,
        user_id: str,
        run_id: str | None,
        notify_started: bool,
        running_timestamp: datetime,
        probe: RunProbe | None,
    ) -> AsyncGenerator[RunEvent, None]:
        """将单个 SDK turn 的事件转换为推理、回答、工具状态、用量及结束事件。

        工具参数和结果不进入输出；推理 delta 和回答 phase 保持原样，用量逐条持久化。
        流异常或缺少完成事件时产出 error，不重放 turn；finally 等待并发控制操作，
        清理活跃索引、同步已完成线程的时间，并为已记录用量回填已知耗时。
        """
        thread, handle, model = live.thread, live.handle, live.model
        usage_run_id = run_id or uuid4().hex
        usage_received = False
        duration_ms = None
        completed = False
        interrupted = False
        message_phases = {}
        try:
            if run_id is not None:
                async with cls._turns_guard:
                    cls._active_turns[run_id] = handle
                    cls._live_turns[run_id] = live
                    should_interrupt = run_id in cls._pending_interrupts
                if should_interrupt:
                    await handle.interrupt()

            if notify_started:
                yield {"type": "started", "thread_id": thread.id, "turn_id": handle.id}

            try:
                async with aclosing(handle.stream()) as stream:
                    async for event in stream:
                        if should_log_event(event.method):
                            logger.info("Codex event: run_id=%s, event=%s", run_id, summarize_event(event))
                        if probe:
                            probe.activity(event)

                        if event.method in {"item/started", "item/completed"}:
                            item = event.payload.item.root
                            if item.type == "agentMessage":
                                phase = getattr(item, "phase", None)
                                if event.method == "item/started":
                                    message_phases[item.id] = getattr(phase, "value", phase)
                                else:
                                    message_phases.pop(item.id, None)
                            elif item.type not in {"userMessage", "reasoning"}:
                                content = _tool_progress(item, completed=event.method == "item/completed")
                                if content:
                                    yield {"type": "progress", "item_id": item.id, "content": content}

                        elif event.method in {
                            "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
                        }:
                            yield {"type": "reasoning", "content": event.payload.delta,
                                   "item_id": event.payload.item_id}

                        elif event.method == "item/agentMessage/delta":
                            yield {"type": "answer", "content": event.payload.delta,
                                   "item_id": event.payload.item_id,
                                   "phase": message_phases.get(event.payload.item_id)}

                        elif event.method == "thread/tokenUsage/updated":
                            if hasattr(event.payload.token_usage.last, "model_dump"):
                                usage = event.payload.token_usage.last.model_dump()
                                usage_received = True
                                await _save_turn_usage({
                                    "timeStamp": datetime.now(running_timestamp.tzinfo).strftime(
                                        CONFIG["logging"]["timestampFormat"]),
                                    "userId": user_id,
                                    "threadId": thread.id,
                                    "runId": usage_run_id,
                                    "model": model,
                                    # Backfill after completion; 0 means the total task duration is not yet available.
                                    "taskDuration_ms": 0,
                                    **usage,
                                })

                        elif event.method not in {"turn/completed", "turn/started"}:
                            # Output deltas, tool progress, hooks, and unknown events are consumed only by the logging and watchdog code above.
                            continue

                        elif event.method == "turn/completed":
                            completed = True
                            duration_ms = event.payload.turn.duration_ms
                            turn = event.payload.turn
                            if turn.status == TurnStatus.failed:
                                message = (
                                    turn.error.message
                                    if turn.error is not None
                                    else "turn failed"
                                )
                                yield _error_event(
                                    RuntimeError(message),
                                    operation="turn_stream",
                                )
                                return
                            interrupted = turn.status == TurnStatus.interrupted
                            break
                if not completed:
                    if probe:
                        probe.finish("failed")
                    yield _error_event(RuntimeError("The streaming events is turn/completed, but not received"),
                                       operation="turn_stream")
                    return
            except Exception as error:
                # A started/streaming turn must not be submitted again: it
                # may already have produced output or external side effects.
                yield _error_event(error, operation="turn_stream")
                return
        finally:
            # Keep the process alive until an in-flight status/steer RPC
            # finishes, even when turn/completed arrives concurrently.
            try:
                async with asyncio.timeout(settings()["cleanupTimeoutSeconds"]), live.lock:
                    live.closed = True
                    if run_id is not None:
                        async with cls._turns_guard:
                            cls._live_turns.pop(run_id, None)
                            cls._active_turns.pop(run_id, None)
                            cls._pending_interrupts.discard(run_id)
                if completed:
                    await session_codex._sync_session_time(user_id, thread)
            finally:
                # Finalize once on every exit path; backfill known duration without inserting usage again.
                if usage_received:
                    await _save_turn_usage({
                        "userId": user_id,
                        "threadId": thread.id,
                        "runId": usage_run_id,
                    }, duration_ms=duration_ms, finalize=True)

        yield {"type": "interrupted" if interrupted else "done"}

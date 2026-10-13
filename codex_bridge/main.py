"""Lark gateway entry point: assemble components, register events, and manage background tasks."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import lark_oapi as lark

# Support running main.py directly from the project directory.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codex_bridge.codex.codex_execution import Codex
from codex_bridge.codex.thread_watchdog import journal
from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger, configure_logging
from codex_bridge.utils.event_dispatcher import EventDispatcher
from codex_bridge.utils.health import heartbeat, run_websocket, write_health
from codex_bridge.services.lark.lark_client import create_websocket_client
from codex_bridge.services.lark.lark_tools import getting_chat_history, adding_reaction_emoji, delete_reaction_emoji
from codex_bridge.services.lark.lark_message_card import sending_card
from codex_bridge.middleware.message_collector import is_stop_command, is_new_command, is_history_command
from codex_bridge.middleware.message_router import MessageRouter, _bot_identity
from codex_bridge.middleware.message_assemble import assemble_codex_input
from codex_bridge.session.session_gateway import SessionCache
from codex_bridge.middleware.gateway_runtime import GatewayRuntime
from codex_bridge.middleware.gateway_execution import GatewayExecution
from codex_bridge.middleware.gateway_commands import GatewayCommands

logger = get_logger("Message")


def create_gateway() -> tuple[GatewayRuntime, GatewayExecution, GatewayCommands, MessageRouter]:
    """每次创建独立网关，组件共享唯一缓存；不启动网络或后台任务。"""
    cache = SessionCache()
    runtime = GatewayRuntime(cache, codex=Codex, send_card=sending_card,
                             delete_reaction=delete_reaction_emoji)
    execution = GatewayExecution(runtime, assemble_input=assemble_codex_input)
    commands = GatewayCommands(runtime, cancel_buffer=lambda chat_id: router._cancel_buffer(chat_id))
    router = MessageRouter(
        cache, submit=execution._handle_message_batch,
        stop=commands.processing_stop, new=commands.processing_new,
        notify=runtime._notify_terminal, fetch_history=getting_chat_history,
        add_reaction=adding_reaction_emoji, clear_reactions=runtime._clear_reaction,
        send_card=lambda *args: runtime.send_card(*args),
        buffer_seconds=lambda: CONFIG["messaging"]["bufferWindowSeconds"],
        history=commands.processing_history,
    )
    return runtime, execution, commands, router


async def main() -> None:
    """配置日志与机器人身份，组装网关、注册事件并启动 WebSocket 及后台维护任务。

    退出时取消维护任务、请求停止仍登记的运行，并尝试在有界等待内刷新运行日志。
    """
    configure_logging(CONFIG["logging"].get("logLevel", "INFO"))
    _bot_identity(required=True)
    runtime, _, commands, router = create_gateway()
    loop = asyncio.get_running_loop()
    dispatcher = EventDispatcher(loop, CONFIG["messaging"].get("maxPendingEvents", 32))
    notices = EventDispatcher(loop, capacity=1)
    accepting = True
    stopping_requested = False
    incoming_tasks: set[asyncio.Task] = set()

    async def receive(data) -> None:
        """追踪入站协程，停机后不再开始处理排队消息。"""
        if not accepting:
            return
        task = asyncio.current_task()
        incoming_tasks.add(task)
        try:
            await router.processing(data)
        finally:
            incoming_tasks.discard(task)

    async def busy(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        """向消息所属聊天发送繁忙提示，不提交模型任务。"""
        await sending_card(data.event.message.chat_id, "The current task is busy. Please try again later.")

    def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        """记录入站消息并按普通或控制事件容量提交处理；容量不足时尝试提交繁忙提示。"""
        if not accepting:
            return
        message = data.event.message
        logger.debug("Message received: chat_id=%s, message_id=%s, type=%s",
                     message.chat_id, message.message_id, message.message_type)
        control = (is_stop_command(message.message_type, message.content)
                   or is_new_command(message.message_type, message.content)
                   or (message.chat_type == "p2p" and is_history_command(message.message_type, message.content)))
        if not dispatcher.submit(receive, data, control=control):
            notices.submit(busy, data)

    def do_p2_im_message_recalled_v1(data: lark.im.v1.P2ImMessageRecalledV1) -> None:
        """使用控制事件容量将撤回消息交给异步撤回处理器。"""
        if accepting:
            dispatcher.submit(commands.processing_recall, data, control=True)

    def do_p2_im_message_read_v1(data: lark.im.v1.P2ImMessageMessageReadV1) -> None:
        """接收已读事件；当前无需业务处理。"""
        pass

    def do_p2_im_message_reaction_created_v1(data: lark.im.v1.P2ImMessageReactionCreatedV1) -> None:
        """接收 reaction 创建事件；当前无需业务处理。"""
        pass

    def do_p2_im_message_reaction_deleted_v1(data: lark.im.v1.P2ImMessageReactionDeletedV1) -> None:
        """接收 reaction 删除事件；当前无需业务处理。"""
        pass

    def do_p2_im_chat_access_event_bot_p2p_chat_entered_v1(
        data: lark.im.v1.P2ImChatAccessEventBotP2pChatEnteredV1,
    ) -> None:
        """接收用户进入机器人私聊的事件；当前无需业务处理。"""
        pass

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .register_p2_card_action_trigger(lambda data: commands.dispatch_history_action(dispatcher, data) if accepting else None)
        .register_p2_im_message_recalled_v1(do_p2_im_message_recalled_v1)
        .register_p2_im_message_message_read_v1(do_p2_im_message_read_v1)
        .register_p2_im_message_reaction_created_v1(do_p2_im_message_reaction_created_v1)
        .register_p2_im_message_reaction_deleted_v1(do_p2_im_message_reaction_deleted_v1)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(do_p2_im_chat_access_event_bot_p2p_chat_entered_v1)
        .build()
    )

    websocket_client = create_websocket_client(event_handler)
    write_health(False)
    health_task = asyncio.create_task(heartbeat(websocket_client))
    main_task = asyncio.current_task()

    def terminate() -> None:
        """一次性关闭入站后进入既有任务中断流程，不重复取消收尾。"""
        nonlocal accepting, stopping_requested
        if accepting:
            accepting = False
            stopping_requested = True
            write_health(False)
            main_task.cancel()

    loop.add_signal_handler(signal.SIGTERM, terminate)
    cleanup_tasks = [asyncio.create_task(runtime.maintain(operation)) for operation in
                     (runtime._expire_session_cache, runtime._retry_reactions,
                      commands.prune_history, runtime.codex.cleanup_control_sessions)]
    try:
        await run_websocket(websocket_client.start)
    except asyncio.CancelledError:
        if not stopping_requested:
            raise
    finally:
        accepting = False
        health_task.cancel()
        await asyncio.gather(health_task, return_exceptions=True)
        write_health(False)
        # 取消尚未形成运行的入站和缓冲，防止清理期间新建任务。
        pending = set(incoming_tasks) | set(runtime.cache.buffer_tasks.values())
        for task in pending:
            task.cancel()
        for task in cleanup_tasks:
            task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        for state in list(runtime.cache.all_runs.values()):
            state.interrupted = True
            state.probe.stop_reason = state.probe.stop_reason or "shutdown"
        await asyncio.gather(*(runtime._interrupt_run(state) for state in list(runtime.cache.all_runs.values())),
                             return_exceptions=True)
        try:
            await journal.flush()
        except Exception:
            logger.exception("Run logs were not fully written before exit")
        if pending:
            # 来源：沿用既有 cleanupTimeoutSeconds；容器最终退出期限由 Compose 约束。
            await asyncio.wait(pending, timeout=CONFIG['codex']['watchdog']['cleanupTimeoutSeconds'])
        loop.remove_signal_handler(signal.SIGTERM)


def cli() -> None:
    """同步命令入口，负责启动异步网关。"""
    asyncio.run(main())


if __name__ == "__main__":
    cli()

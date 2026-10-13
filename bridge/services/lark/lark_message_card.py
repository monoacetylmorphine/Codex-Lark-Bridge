"""Send regular notifications and streaming replies using the reference project Card 2.0 style."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING, TypeVar
from uuid import uuid4

from lark_oapi.api.cardkit.v1 import (
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
)
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from bridge.services.lark.lark_client import client
from bridge.services.lark.lark_requests import call_lark
from bridge.configs.loader import CONFIG
from bridge.utils.terminal_log import get_logger
from bridge.codex.thread_watchdog import settings

if TYPE_CHECKING:
    from bridge.codex.codex_execution import RunEvent

    from lark_oapi.core.model import BaseResponse

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT", bound="BaseResponse")

logger = get_logger("Card")
UPDATE_INTERVAL = 0.25
STREAM_LIFETIME = 9 * 60
ELEMENT_ID = "response_content"
MARKDOWN_IMAGE_PATTERN = re.compile(
    r"(?<!\\)!\[((?:\\.|[^\]\\\r\n])*)\]\(((?:\\.|[^)\\\r\n])*)\)"
)


class CardDeliveryError(RuntimeError):
    """Message delivery failure, handled separately from upstream task failure."""


class CardRequestError(CardDeliveryError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        """保存卡片请求错误文案及可选的飞书错误码。"""
        super().__init__(message)
        self.code = code


class CardStreamStopped(Exception):
    """Upstream recall or /stop request: stop output and discard unsent buffered text."""


@dataclass(frozen=True)
class CardReplace:
    """Replace the current body and buffer when switching from reasoning to the answer."""

    content: str


class CardSteer:
    """A writer barrier: pause before the RPC, rotate only on confirmed acceptance."""

    def __init__(self, content: str) -> None:
        """在当前事件循环中创建写入屏障及等待状态，用于协调 steer 确认和换卡结果。"""
        loop = asyncio.get_running_loop()
        self.content = content
        self.ready: asyncio.Future[bool] = loop.create_future()
        self.decision: asyncio.Future[bool] = loop.create_future()
        self.applied: asyncio.Future[bool] = loop.create_future()
        self.accepted = False

    def decide(self, accepted: bool) -> None:
        """仅首次记录 steer 是否被接受并完成决策 Future，后续调用不覆盖已有决定。"""
        if not self.decision.done():
            self.accepted = accepted
            self.decision.set_result(accepted)

    def release(self) -> None:
        """将尚未完成的 ready 和 applied Future 置为 False，唤醒等待者；不修改决策 Future。"""
        for future in (self.ready, self.applied):
            if not future.done():
                future.set_result(False)


def _replace_markdown_images(content: str) -> str:
    """将飞书卡片不支持的 Markdown 图片降级为裸地址。"""
    return MARKDOWN_IMAGE_PATTERN.sub(r"\2", content)


class CardStreamSession:
    """One run's output controls; the sender remains the only CardKit writer.
    Controls wake a silent source without cancelling its pending read. There is
    at most one prefetched event, so a slow card cannot build an unbounded queue.
    The gateway serializes steer RPCs with its existing owner.controls lock.
    """

    def __init__(self, *, cancelled: Callable[[], bool] = lambda: False) -> None:
        """初始化单次运行的控制队列、屏障及卡片缓冲状态；cancelled 回调由发送过程检查。"""
        self.cancelled = cancelled
        self.controls: asyncio.Queue[CardSteer] = asyncio.Queue()
        self.closed = False
        self.barriers: set[CardSteer] = set()
        self.card_id: str | None = None
        self.message_id: str | None = None
        self.sequence = 0
        self.accumulated = ""
        self.sent = ""
        self.last_update = 0.0
        self.opened_at = 0.0
        self.card_number = 0
        self.pending_replacement = False
        self.placeholder = False

    @asynccontextmanager
    async def steering(self, content: str) -> AsyncIterator[CardSteer]:
        """将 steer 屏障排入输出控制队列，等待发送层就绪后交给调用方决策。

        已关闭或取消时释放等待者；退出上下文时将尚未决定的屏障判为未接受，并移除登记。
        """
        barrier = CardSteer(content)
        self.barriers.add(barrier)
        try:
            if self.closed or self.cancelled():
                barrier.release()
            else:
                self.controls.put_nowait(barrier)
            await asyncio.shield(barrier.ready)
            yield barrier
        finally:
            barrier.decide(False)
            self.barriers.discard(barrier)

    def close(self) -> None:
        """标记会话关闭，拒绝未决定的屏障并释放等待者；不直接发送关闭卡片请求。"""
        self.closed = True
        for barrier in self.barriers:
            barrier.decide(False)
            barrier.release()

    async def events(
        self,
        source: AsyncGenerator[RunEvent, None],
    ) -> AsyncGenerator[RunEvent, None]:
        """合并上游运行事件和 steer 控制事件，同时就绪时优先产出控制事件。

        最多保留一个预读事件，取消时抛出 CardStreamStopped；finally 释放屏障、取消待读任务并关闭上游生成器。
        """
        pending = control = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(source))
                if control is None:
                    control = asyncio.create_task(self.controls.get())
                await asyncio.wait({pending, control}, return_when=asyncio.FIRST_COMPLETED)
                if self.cancelled():
                    raise CardStreamStopped()
                # Prefer a submitted control over a simultaneous EOF/delta.
                if control.done():
                    barrier, control = control.result(), None
                    yield {"type": "card_control", "control": barrier}
                    continue
                ready, pending = pending, None
                try:
                    event = ready.result()
                except StopAsyncIteration:
                    return
                yield event
        finally:
            self.close()
            tasks = [task for task in (pending, control) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await source.aclose()


def _card(content: str, streaming: bool) -> dict[str, Any]:
    """组合配置、固定标题和正文，构造静态或流式 Card 2.0 数据，不发送请求。"""
    # Source: project-reference/skills/lark-im/references/card/card-2.0-schema.md.
    return {
        "schema": "2.0",
        "config": _card_config(content, streaming),
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "Codex",
            },
            "template": "blue",
            "icon": {
                "tag": "standard_icon",
                "token": "lark-logo_colorful",
            },
        },
        "body": _card_body(content),
    }


def _card_config(content: str, streaming: bool) -> dict[str, Any]:
    """构造卡片展示与流式开关配置；流式摘要使用等待提示，静态摘要取正文前 100 个字符。"""
    return {
        "update_multi": True,
        "width_mode": "fill",
        "streaming_mode": streaming,
        "summary": {
            "content": 'Thinking, please wait...' if streaming else content[:100],
        },
        "style": {
            "text_size": {
                "body": {
                    "default": "normal",
                    "pc": "normal",
                    "mobile": "normal",
                },
            },
        },
    }


def _card_body(content: str) -> dict[str, Any]:
    """构造带固定正文元素标识和 AI 提示的卡片正文，并将 Markdown 图片语法降级为地址。"""
    return {
        "direction": "vertical",
        "padding": "12px 12px 12px 12px",
        "elements": [
            {
                "tag": "markdown",
                "content": _replace_markdown_images(content),
                "text_align": "left",
                "text_size": "normal",
                "margin": "0px 0px 0px 0px",
                "element_id": ELEMENT_ID,
            },
            {
                "tag": "hr",
                "margin": "0px 0px 0px 0px",
            },
            {
                "tag": "column_set",
                "horizontal_spacing": "12px",
                "horizontal_align": "right",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": "<font color=\"grey-600\">内容由 AI 生成, 请仔细甄别</font>",
                                "text_align": "center",
                                "text_size": "notation",
                                "margin": "4px 0px 0px 0px",
                                "element_id": "footnote_text",
                            },
                        ],
                        "padding": "0px 0px 0px 0px",
                        "direction": "vertical",
                        "horizontal_spacing": "8px",
                        "vertical_spacing": "8px",
                        "horizontal_align": "left",
                        "vertical_align": "top",
                        "margin": "0px 0px 0px 0px",
                        "weight": 1,
                    },
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [],
                        "padding": "0px 0px 0px 0px",
                        "direction": "vertical",
                        "horizontal_spacing": "8px",
                        "vertical_spacing": "8px",
                        "horizontal_align": "left",
                        "vertical_align": "top",
                        "margin": "0px 0px 0px 0px",
                    },
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [],
                        "padding": "0px 0px 0px 0px",
                        "vertical_spacing": "8px",
                        "horizontal_align": "left",
                        "vertical_align": "top",
                        "margin": "0px 0px 0px 0px",
                    },
                ],
                "margin": "0px 0px 4px 0px",
            },
        ],
    }


async def _call(operation: Callable[[RequestT], ResponseT], request: RequestT) -> ResponseT:
    """在配置超时内调用飞书卡片接口，统一将请求异常或失败响应转为 CardRequestError。

    失败响应保留飞书错误码供恢复逻辑判断；外部取消不在此处转换。
    """
    try:
        async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
            response = await call_lark(operation, request)
    except Exception as exc:
        raise CardRequestError(f"Lark card request error: {type(exc).__name__}: {exc}") from exc
    if not response.success():
        raise CardRequestError(
            f"Lark card request failed: code={response.code}, msg={response.msg}, "
            f"log_id={response.get_log_id()}", code=response.code,
        )
    return response


async def _send(union_id: str, content: dict[str, Any]) -> str:
    """发送交互卡片数据并返回 message_id；以 oc_ 开头的目标按 chat_id 处理，其余按 union_id 处理。"""
    request = (CreateMessageRequest.builder()
        .receive_id_type("chat_id" if union_id.startswith("oc_") else "union_id")
        .request_body(CreateMessageRequestBody.builder()
            .receive_id(union_id).msg_type("interactive")
            .content(json.dumps(content, ensure_ascii=False)).build())
        .build())
    response = await _call(client.im.v1.message.create, request)
    return response.data.message_id


async def sending_card(
    union_id: str,
    content: str | AsyncIterable[str | CardReplace | CardSteer],
    *,
    session: CardStreamSession | None = None,
) -> str | None:
    """发送静态文本或消费异步文本流，返回最后一张已发送卡片的 message_id，无消息时返回 None。

    流式正文按更新间隔合并；CardReplace 替换正文及缓冲，CardSteer 在确认接受后换卡。
    流式卡片达到 STREAM_LIFETIME 时关闭，后续有内容才续卡；300309 明确拒绝时承接未发送内容。
    其他交付失败后继续消费上游，结束时抛出 CardDeliveryError；CardStreamStopped 丢弃未发送缓冲，
    但仍尝试关闭流式状态。目标支持 union_id 或以 oc_ 开头的群 chat_id，空文本不发送。
    """
    if isinstance(content, str):
        return await _send(union_id, _card(content, False)) if content else None
    if not isinstance(content, AsyncIterable):
        raise TypeError("content must be a string or an asynchronous text stream")

    session = session or CardStreamSession()

    delivery_error = None
    pending = None
    no_chunk = object()

    def reset_card() -> None:
        """重置当前卡片标识、序号及正文缓冲，保留最后发送的 message_id 和跨卡会话状态。"""
        session.card_id, session.sequence, session.accumulated, session.sent = None, 0, "", ""
        session.pending_replacement = False

    async def create_card() -> None:
        """根据当前累计正文创建流式卡片并发送消息引用；请求前记录生命周期起点，发送前检查取消。"""
        if session.cancelled():
            raise CardStreamStopped()
        session.card_number += 1
        body = _card(session.accumulated, True)
        # Start the deadline before the HTTP request, conservatively including latency.
        session.opened_at = time.monotonic()
        request = (CreateCardRequest.builder()
            .request_body(CreateCardRequestBody.builder().type("card_json")
                .data(json.dumps(body, ensure_ascii=False)).build()).build())
        response = await _call(client.cardkit.v1.card.create, request)
        session.card_id = response.data.card_id
        if session.cancelled():
            raise CardStreamStopped()
        session.message_id = await _send(union_id, {
            "type": "card", "data": {"card_id": session.card_id},
        })
        session.sent = session.accumulated
        session.pending_replacement = False
        session.last_update = time.monotonic()
        logger.info("Streaming card sent: card_id=%s, part=%s", session.card_id, session.card_number)

    async def flush() -> None:
        """提交与已发送正文不同的累计内容；300309 拒绝时换卡承接未发送后缀或完整替换正文。

        其他请求错误交给外层处理；成功后更新已发送内容及刷新时间。
        """
        if session.cancelled():
            raise CardStreamStopped()
        if session.accumulated == session.sent:
            return
        session.sequence += 1
        request = (ContentCardElementRequest.builder()
            .card_id(session.card_id).element_id(ELEMENT_ID)
            .request_body(ContentCardElementRequestBody.builder()
                .content(_replace_markdown_images(session.accumulated))
                .sequence(session.sequence).uuid(uuid4().hex).build())
            .build())
        try:
            await _call(client.cardkit.v1.card_element.content, request)
        except CardRequestError as exc:
            if exc.code != 300309:
                raise
            # The rejected write was not applied. Carry only the unsent suffix;
            # a replacement must instead carry its full new body.
            remainder = (session.accumulated[len(session.sent):]
                         if not session.pending_replacement and session.accumulated.startswith(session.sent) else session.accumulated)
            logger.warning("Streaming card closed; continuing in a new card: card_id=%s", session.card_id)
            reset_card()
            session.accumulated = remainder
            if session.accumulated:
                await create_card()
            return
        session.sent = session.accumulated
        session.pending_replacement = False
        session.last_update = time.monotonic()
        logger.debug("Streaming card updated: card_id=%s, sequence=%s, chars=%s, age=%.3f",
                     session.card_id, session.sequence, len(session.sent), session.last_update - session.opened_at)

    async def close_card() -> None:
        """关闭当前卡片的流式状态并设置摘要；忽略已关闭错误，finally 重置当前卡片缓冲。"""
        if session.card_id is None:
            return
        session.sequence += 1
        request = (SettingsCardRequest.builder().card_id(session.card_id)
            .request_body(SettingsCardRequestBody.builder()
                .settings(json.dumps({"config": {
                    "streaming_mode": False,
                    "summary": {"content": session.sent[:100]},
                }}, ensure_ascii=False))
                .sequence(session.sequence).uuid(uuid4().hex).build()).build())
        try:
            await _call(client.cardkit.v1.card.settings, request)
        except CardRequestError as exc:
            if exc.code != 300309:
                raise
        else:
            logger.info("Streaming card closed: card_id=%s, age=%.3f",
                        session.card_id, time.monotonic() - session.opened_at)
        finally:
            reset_card()

    async def delivery_failed(exc: CardDeliveryError) -> None:
        """保存交付错误并尝试关闭当前卡片，使主循环转为仅消费上游而不继续发送正文。"""
        nonlocal delivery_error
        delivery_error = exc
        logger.exception("Card delivery failed; continuing to consume task events without retrying requests with uncertain results")
        try:
            await close_card()
        except CardDeliveryError:
            logger.exception("Failed to close Lark card streaming mode")

    try:
        iterator = aiter(content)
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(iterator))
            deadline = None
            if session.card_id is not None:
                deadline = session.opened_at + STREAM_LIFETIME
                if session.accumulated != session.sent:
                    deadline = min(deadline, session.last_update + UPDATE_INTERVAL)
            timeout = None if deadline is None else max(0, deadline - time.monotonic())
            # asyncio.wait does not cancel the upstream anext on a timer tick.
            await asyncio.wait({pending}, timeout=timeout)
            chunk = no_chunk
            ended = False
            if pending.done():
                ready, pending = pending, None
                try:
                    chunk = ready.result()
                except StopAsyncIteration:
                    ended = True
            if session.cancelled():
                raise CardStreamStopped()
            if isinstance(chunk, CardSteer):
                # No other CardKit request runs while this writer awaits the RPC.
                if not chunk.ready.done():
                    chunk.ready.set_result(True)
                accepted = await asyncio.shield(chunk.decision)
                rotated = False
                try:
                    if session.cancelled():
                        raise CardStreamStopped()
                    if accepted and delivery_error is None:
                        # Drop the old unsent buffer; already shown text remains.
                        await close_card()
                        session.accumulated = chunk.content
                        await create_card()
                        session.placeholder = True
                        rotated = True
                except CardDeliveryError as exc:
                    await delivery_failed(exc)
                finally:
                    if not chunk.applied.done():
                        chunk.applied.set_result(rotated and not session.cancelled())
                continue
            if delivery_error is not None:
                if ended:
                    break
                continue
            try:
                if ended:
                    if session.card_id is not None:
                        if session.placeholder:
                            session.accumulated = CONFIG["messages"]["steerCompleted"]
                            session.pending_replacement = True
                            session.placeholder = False
                        await flush()
                    break
                if session.card_id is not None and time.monotonic() >= session.opened_at + STREAM_LIFETIME:
                    old_card = session.card_id
                    await flush()
                    # flush may have recovered from 300309 by opening a fresh card.
                    if session.card_id == old_card:
                        await close_card()
                replace = isinstance(chunk, CardReplace)
                if replace:
                    chunk = chunk.content
                if chunk is not no_chunk:
                    if not isinstance(chunk, str):
                        raise TypeError("Streaming content may yield only strings or CardReplace")
                    if chunk:
                        if session.placeholder:
                            replace = True
                            session.placeholder = False
                        session.pending_replacement |= replace
                        session.accumulated = chunk if replace else session.accumulated + chunk
                        if session.card_id is None:
                            await create_card()
                if session.card_id is not None and (
                    replace or time.monotonic() - session.last_update >= UPDATE_INTERVAL
                ):
                    await flush()
            except CardDeliveryError as exc:
                await delivery_failed(exc)
                if ended:
                    break
    except CardStreamStopped:
        delivery_error = None
    finally:
        session.close()
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        try:
            await close_card()
        except CardDeliveryError as exc:
            delivery_error = delivery_error or exc
            logger.exception("Failed to close Lark card streaming mode")
    if delivery_error is not None:
        raise delivery_error
    return session.message_id

"""Private-chat history selection cards with form confirmation, delivery, and callback context separate from regular message cards."""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, NotRequired, TYPE_CHECKING, TypeVar, TypedDict
from uuid import uuid4

from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    PatchMessageRequest, PatchMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from codex_bridge.services.lark.lark_requests import call_lark
from codex_bridge.configs.loader import CONFIG

if TYPE_CHECKING:
    from lark_oapi.core.model import BaseResponse
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT", bound="BaseResponse")


# Source: user requirement to show the latest 30 entries by default, matching the gateway option limit.
PAGE_SIZE = CONFIG["messaging"].get("sessionHistoryLimit", 30)
# Source: initial interaction and memory policy for this feature, not a Lark platform limit. Use /history again after expiry.
CARD_TTL_SECONDS = 24 * 60 * 60
MAX_CARDS = 1024
SELECT_NAME = "history_thread"


class HistoryOption(TypedDict):
    """History options retain their original dictionary representation and card protocol."""

    label: str
    value: str
    updated_at: NotRequired[int | None]


@dataclass
class HistoryCard:
    token: str
    user_id: str
    chat_id: str
    options: tuple[HistoryOption, ...]
    created_at: float
    message_id: str = ""
    page: int = 0
    revision: int = 0
    busy: bool = False
    finished: bool = False

    @property
    def visible_options(self) -> tuple[HistoryOption, ...]:
        """按当前页码和 PAGE_SIZE 返回本页历史选项切片。"""
        return self.options[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]

    def message_event(self) -> SimpleNamespace:
        """仅用服务端保存的私聊上下文适配现有恢复入口。"""
        return SimpleNamespace(event=SimpleNamespace(
            message=SimpleNamespace(chat_id=self.chat_id, chat_type="p2p"),
            sender=SimpleNamespace(sender_id=SimpleNamespace(union_id=self.user_id)),
        ))


class HistoryCardStore:
    """Accessed only by the main event loop; lifetime and count are bounded, and old cards expire on restart."""

    def __init__(self) -> None:
        """初始化以随机票据索引的内存历史卡片集合。"""
        self.cards: dict[str, HistoryCard] = {}

    def prune(self) -> None:
        """移除超过有效期且未在处理中的历史卡片，不中断正在处理的卡片。"""
        now = time.monotonic()
        for token, card in list(self.cards.items()):
            if not card.busy and now - card.created_at >= CARD_TTL_SECONDS:
                self.cards.pop(token, None)

    def create(self, user_id: str, chat_id: str, options: Iterable[HistoryOption]) -> HistoryCard:
        """清理过期卡片并保存一张新历史卡片；达到容量时淘汰最早的非忙碌卡片。

        所有卡片均忙碌且容量已满时抛出 RuntimeError；返回的卡片尚未发送，message_id 由调用方回填。
        """
        self.prune()
        if len(self.cards) >= MAX_CARDS:
            oldest = next((key for key, card in self.cards.items() if not card.busy), None)
            if oldest is None:
                raise RuntimeError("History card processing capacity is full")
            self.cards.pop(oldest)
        card = HistoryCard(uuid4().hex, user_id, chat_id, tuple(options), time.monotonic())
        self.cards[card.token] = card
        return card

    def resolve(self, data: P2CardActionTrigger) -> HistoryCard:
        """卡片参数不是身份来源；同时验证操作者和原私聊消息，拒绝转发卡片。"""
        event = data.event
        action = event.action
        value = action.value if isinstance(action.value, dict) else {}
        token = value.get("ticket")
        card = self.cards.get(token) if isinstance(token, str) else None
        if card is None or time.monotonic() - card.created_at >= CARD_TTL_SECONDS:
            raise ValueError("This card has expired. Send /history again in a private chat")
        operator, context = event.operator, event.context
        if (not operator or not context or not card.message_id
                or operator.union_id != card.user_id
                or context.open_chat_id != card.chat_id
                or context.open_message_id != card.message_id):
            raise ValueError("Only the original user can operate this history card in the original private chat")
        if card.busy or card.finished or value.get("revision") != card.revision:
            raise ValueError("This action has been processed or is in progress. Use the latest card")
        return card


def _plain(content: str) -> dict[str, str]:
    """将文本包装为飞书 plain_text 元素。"""
    return {"tag": "plain_text", "content": content}


def _shell(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """将给定正文元素包装为历史会话 Card 2.0 卡片结构。"""
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"title": _plain("Session history"), "template": "blue"},
        "body": {"elements": elements},
    }


def status_card(content: str) -> dict[str, Any]:
    """对状态文案进行 HTML 和 Markdown 转义后构造历史会话状态卡片，不发送请求。"""
    escaped = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", html.escape(content))
    return _shell([{"tag": "markdown", "content": escaped}])


def build_history_card(card: HistoryCard) -> dict[str, Any]:
    """采用 form_action_type=submit 的 confirm：选中不回调，取消不提交表单。

    来源：用户提供的 confirm/表单字段说明；Card 2.0 form/button 官方文档。
    """
    if not card.options:
        return status_card("No session history available")
    value = {"ticket": card.token, "revision": card.revision}
    elements = [
        {"tag": "markdown", "content": "Select a previous session and click Activate. The session switches only after confirmation."},
        {"tag": "form", "name": "history_form", "elements": [
            {"tag": "select_static", "name": SELECT_NAME, "required": True,
             "placeholder": _plain("Select a previous session"), "width": "fill",
             "options": [{"text": _plain(item["label"]), "value": item["value"]}
                         for item in card.visible_options]},
            {"tag": "button", "name": "activate_history", "type": "primary",
             "text": _plain("Activate this session"), "form_action_type": "submit",
             "behaviors": [{"type": "callback", "value": {**value, "action": "activate_history"}}],
             "confirm": {
                 "title": _plain("Switch to this previous session?"),
                 "text": _plain("Confirming will stop the current task and activate the selected session. Subsequent messages will continue in that session."),
             }},
        ]},
    ]
    pages = (len(card.options) + PAGE_SIZE - 1) // PAGE_SIZE
    if pages > 1:
        elements.append({"tag": "markdown", "content": f"Page {card.page + 1}/{pages}"})
        for label, page in (("Previous", card.page - 1), ("Next", card.page + 1)):
            if 0 <= page < pages:
                elements.append({"tag": "button", "text": _plain(label), "behaviors": [
                    {"type": "callback", "value": {**value, "action": "history_page", "page": page}},
                ]})
    return _shell(elements)


def confirmed_thread_id(data: P2CardActionTrigger, card: HistoryCard) -> str:
    """验证回调来自激活按钮，并返回表单中属于当前可见选项的 thread_id。

    无效按钮或选择抛出 ValueError；身份、有效期和修订号校验应先由 HistoryCardStore.resolve 完成。
    """
    action = data.event.action
    # Do not activate a session from select_static alone, action.option, or an unknown button.
    if (action.tag != "button" or action.name != "activate_history"
            or not isinstance(action.value, dict)
            or action.value.get("action") != "activate_history"):
        raise ValueError("Confirm your selection using the Activate button")
    values = action.form_value
    selected = values.get(SELECT_NAME) if isinstance(values, dict) else None
    if not isinstance(selected, str) or not any(item["value"] == selected for item in card.visible_options):
        raise ValueError("Select a session from the current card")
    return selected


def callback_response(content: str, *, error: bool = False) -> P2CardActionTriggerResponse:
    """构造信息或错误类型的卡片回调 toast 响应，不主动发送消息。"""
    return P2CardActionTriggerResponse({"toast": {"type": "error" if error else "info", "content": content}})


async def _request(operation: Callable[[RequestT], ResponseT], request: RequestT) -> ResponseT:
    """通过受限请求执行器调用飞书接口，业务响应失败时抛出 RuntimeError，其他请求异常原样传播。"""
    response = await call_lark(operation, request)
    if not response.success():
        raise RuntimeError(f"Interactive card request failed: code={response.code}, log_id={response.get_log_id()}")
    return response


async def send_interactive_card(user_id: str, body: dict[str, Any]) -> str:
    """向指定 union_id 发送交互卡片并返回 message_id；请求失败时抛出异常。"""
    # Load authentication lazily so card construction and offline callback tests need no credentials.
    from codex_bridge.services.lark.lark_client import client
    request = (CreateMessageRequest.builder().receive_id_type("union_id")
        .request_body(CreateMessageRequestBody.builder().receive_id(user_id)
            .msg_type("interactive").content(json.dumps(body, ensure_ascii=False)).build()).build())
    response = await _request(client.im.v1.message.create, request)
    return response.data.message_id


async def update_interactive_card(message_id: str, body: dict[str, Any]) -> None:
    """按 message_id 更新已有交互卡片正文；请求失败时抛出异常，不创建替代消息。"""
    from codex_bridge.services.lark.lark_client import client
    request = (PatchMessageRequest.builder().message_id(message_id)
        .request_body(PatchMessageRequestBody.builder()
            .content(json.dumps(body, ensure_ascii=False)).build()).build())
    await _request(client.im.v1.message.patch, request)

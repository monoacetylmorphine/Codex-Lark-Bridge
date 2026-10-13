from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageReactionResponse,
    DeleteMessageReactionRequest,
    DeleteMessageReactionResponse,
    Emoji,
    GetMessageResourceRequest,
    GetMessageResourceResponse,
    ListMessageRequest,
    ListMessageResponse,
    Message,
)

from bridge.configs.loader import CONFIG

from bridge.services.lark.lark_client import client
from bridge.services.lark.lark_requests import call_lark
from bridge.utils.terminal_log import get_logger

logger = get_logger("LarkTools")
from bridge.middleware.resource_validator import (
    ResourceValidationError,
    read_resource_bytes,
    validate_downloaded_resource,
)


async def adding_reaction_emoji(message_id: str) -> str | None:
    """为消息添加配置的处理中 reaction，成功返回 reaction_id，业务响应失败时记录日志并返回 None。

    请求执行异常向调用方传播。
    """

    request: CreateMessageReactionRequest = CreateMessageReactionRequest.builder() \
            .message_id(message_id) \
            .request_body(CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder()
                    .emoji_type(CONFIG["lark"]["reaction"]["processingEmoji"])
                    .build())
                .build()) \
            .build()

    response: CreateMessageReactionResponse = await call_lark(client.im.v1.message_reaction.create, request)

    if not response.success():
        _log_lark_failure(response, "client.im.v1.message_reaction.create")
        return

    lark.logger.info(lark.JSON.marshal(response.data, indent=4))
    return response.data.reaction_id


async def delete_reaction_emoji(message_id: str, reaction_id: str) -> bool:
    """删除指定消息的 reaction；业务响应成功返回 True，失败记录日志并返回 False，请求异常向外传播。"""

    request: DeleteMessageReactionRequest = DeleteMessageReactionRequest.builder() \
        .message_id(message_id) \
        .reaction_id(reaction_id) \
        .build()

    response: DeleteMessageReactionResponse = await call_lark(client.im.v1.message_reaction.delete, request)

    if not response.success():
        _log_lark_failure(response, "client.im.v1.message_reaction.delete")
        return False

    lark.logger.info(lark.JSON.marshal(response.data, indent=4))
    return True


async def download_msg_resource(
    union_id: str,
    message_id: str,
    resource_key: str,
    resource_type: str,
) -> str | None:
    """请求飞书消息资源，在线程中校验并保存到目标工作区，成功返回保存路径字符串。

    业务响应失败返回 None；资源校验、请求或文件写入异常向外传播。union_id 用作工作区标识，
    群聊调用方可传入 chat_id，本函数不验证该标识的归属。
    """

    logger.debug("Fetching message resource: message_id=%s, type=%s", message_id, resource_type)

    request: GetMessageResourceRequest = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(resource_key) \
        .type(resource_type) \
        .build()

    response: GetMessageResourceResponse = await call_lark(client.im.v1.message_resource.get, request)

    if not response.success():
        _log_lark_failure(response, "client.im.v1.message_resource.get")
        return

    # Move response reading, validation, and disk writes off the event loop together.
    return await asyncio.to_thread(_save_resource, response, union_id, message_id, resource_key, resource_type)


def _save_resource(
    response: GetMessageResourceResponse,
    union_id: str,
    message_id: str,
    resource_key: str,
    resource_type: str,
) -> str:
    """读取并校验下载响应，向目标工作区的入站目录写入带时间戳的文件，返回路径字符串。

    校验失败记录日志并抛出 ResourceValidationError；目录创建和文件写入异常原样传播。
    """
    saving_path = (
        Path(CONFIG["storage"]["workspaceRoot"])
        / union_id
        / CONFIG["storage"]["inboundSubdirectory"]
    )
    saving_path.mkdir(parents=True, exist_ok=True)

    try:
        data = read_resource_bytes(response.file)
        resource = validate_downloaded_resource(
            data=data,
            resource_key=resource_key,
            resource_type=resource_type,
            file_name=response.file_name,
            headers=response.raw.headers,
        )
    except ResourceValidationError as error:
        lark.logger.error(
            f"Message resource validation failed, message_id={message_id}, "
            f"resource_key={resource_key}, error={error}"
        )
        raise

    file_stem = Path(resource.file_name).stem
    file_ext = Path(resource.file_name).suffix
    timestamp = datetime.now(
        timezone(timedelta(hours=CONFIG["runtime"]["timezoneOffsetHours"]))
    ).strftime(CONFIG["resources"]["fileName"]["timestampFormat"])
    file_path = saving_path / f"{file_stem}_{timestamp}{file_ext}"
    with open(file_path, "wb") as file:
        file.write(resource.data)

    logger.debug("Resource saved: path=%s", file_path)
    return str(file_path)

async def getting_chat_history(chat_id: str, messages_num: int) -> list[Message]:
    """按创建时间倒序获取一页聊天历史，messages_num 作为 page_size，不继续翻页。

    业务响应失败时记录日志并返回空列表；请求执行异常向调用方传播。
    """

    request: ListMessageRequest = ListMessageRequest.builder() \
            .container_id_type("chat") \
            .container_id(chat_id) \
            .sort_type("ByCreateTimeDesc") \
            .page_size(messages_num) \
            .build()

    response: ListMessageResponse = await call_lark(client.im.v1.message.list, request)

    if not response.success():
        _log_lark_failure(response, "client.im.v1.message.list")
        return []

    return list(response.data.items or [])


def _log_lark_failure(
    response: CreateMessageReactionResponse | DeleteMessageReactionResponse | GetMessageResourceResponse | ListMessageResponse,
    action: str,
) -> None:
    """复用同一失败日志格式，保留各调用方原有返回契约。"""
    lark.logger.error(
        f"{action} failed, code: {response.code}, msg: {response.msg}, "
        f"log_id: {response.get_log_id()}, resp: \n"
        f"{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}"
    )

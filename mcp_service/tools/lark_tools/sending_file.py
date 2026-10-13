"""校验接收方后上传文件，失败通过异常明确返回给 MCP 调用方。"""

import json
from pathlib import Path
import re

from lark_oapi.api.im.v1 import (
    CreateFileRequest, CreateFileRequestBody,
    CreateMessageRequest, CreateMessageRequestBody,
)

from mcp_service.configs.loader import CONFIG
from mcp_service.services.lark.lark_client import get_client
from mcp_service.services.lark.lark_requests import call_lark

# 来源：用户提供的 ID 样例，共 35 字符（前缀 3 + ID 32）；同样应用于 oc_。
RECIPIENT_ID_LENGTH = 35


def _recipient(path: Path) -> str:
    # 接收方由调用方提供的路径目录指定；只接受完整且唯一的 ID 目录。
    recipients = {part for part in path.parent.parts
                  if re.fullmatch(r"(?:on_|oc_)[A-Za-z0-9]+", part)
                  and len(part) == RECIPIENT_ID_LENGTH}
    if len(recipients) != 1:
        raise ValueError("文件路径必须包含唯一的 on_ 或 oc_ 接收方目录，ID 总长度必须为 35 字符")
    return recipients.pop()


async def sending_file(file_path: str) -> dict[str, str]:
    """发送指定绝对路径文件，成功返回接收方、文件和消息 ID。"""
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        raise ValueError("文件路径必须是绝对路径")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("文件路径必须指向普通文件")
    recipient = _recipient(path)
    upload = CONFIG["lark"]["upload"]
    extension = path.suffix.lower().removeprefix(".")
    file_type = extension if extension in upload["nativeFileTypes"] else upload["fallbackFileType"]
    client = get_client()

    def upload_file():
        # 文件必须由 worker 持有，调用方超时不能提前关闭仍在上传的句柄。
        with path.open("rb") as file:
            request = (CreateFileRequest.builder()
                .request_body(CreateFileRequestBody.builder()
                    .file_type(file_type).file_name(path.name).file(file).build())
                .build())
            return client.im.v1.file.create(request)
    response = await call_lark(upload_file)
    if not response.success():
        raise RuntimeError(f"飞书文件上传失败: code={response.code}, log_id={response.get_log_id()}")
    if response.data is None or not response.data.file_key:
        raise RuntimeError("飞书文件上传响应缺少 file_key")
    file_key = response.data.file_key
    request = (CreateMessageRequest.builder()
        .receive_id_type("union_id" if recipient.startswith("on_") else "chat_id")
        .request_body(CreateMessageRequestBody.builder()
            .receive_id(recipient).msg_type("file")
            .content(json.dumps({"file_key": file_key})).build())
        .build())
    response = await call_lark(client.im.v1.message.create, request)
    if not response.success():
        raise RuntimeError(f"飞书文件消息发送失败: code={response.code}, log_id={response.get_log_id()}")
    if response.data is None or not response.data.message_id:
        raise RuntimeError("飞书文件消息响应缺少 message_id，发送结果未确认")
    return {"recipient_id": recipient, "file_key": file_key, "message_id": response.data.message_id}

from __future__ import annotations

import os

import lark_oapi as lark
from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import protect_sdk_logs

protect_sdk_logs(lark.logger)


def _required_setting(name: str) -> str:
    """读取指定环境变量；缺失或为空时抛出 RuntimeError，不提供后备凭据。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing Lark configuration setting: {name}")
    return value


LARK_APP_ID = _required_setting(CONFIG["lark"]["credentials"]["appIdEnv"])
LARK_APP_SECRET = _required_setting(CONFIG["lark"]["credentials"]["appSecretEnv"])


# OpenAPI client: used for normal HTTP calls such as sending messages and
# downloading message resources.
client = (
    lark.Client.builder()
    .app_id(LARK_APP_ID)
    .app_secret(LARK_APP_SECRET)
    .timeout(CONFIG["codex"]["watchdog"]["cardRequestTimeoutSeconds"])
    .log_level(lark.LogLevel.DEBUG)
    .build()
)


def create_websocket_client(event_handler: lark.EventDispatcherHandler) -> lark.ws.Client:
    """使用与 OpenAPI 客户端相同的应用凭据构造 WebSocket 客户端并绑定事件处理器，不启动连接。"""
    return lark.ws.Client(
        LARK_APP_ID,
        LARK_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG,
    )

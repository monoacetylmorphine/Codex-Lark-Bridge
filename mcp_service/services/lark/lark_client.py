import os

import lark_oapi as lark
from mcp_service.configs.loader import CONFIG
from mcp_service.utils.terminal_log import protect_sdk_logs

protect_sdk_logs(lark.logger)


def _required_setting(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少飞书配置项: {name}")
    return value


def get_client():
    """仅在调用飞书工具时校验凭据，避免影响其他工具启动。"""
    credentials = CONFIG["lark"]["credentials"]
    return (
        lark.Client.builder()
        .app_id(_required_setting(credentials["appIdEnv"]))
        .app_secret(_required_setting(credentials["appSecretEnv"]))
        .timeout(CONFIG["codex"]["watchdog"]["cardRequestTimeoutSeconds"])
        .log_level(lark.LogLevel.DEBUG)
        .build()
    )

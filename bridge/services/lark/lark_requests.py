"""Shared entry point for synchronous Lark requests, isolated from the default model and file-processing thread pool."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


# The same source is loaded by two packages; configuration and thread pools remain service-specific.
if __package__ == "mcp_service.services.lark":
    from mcp_service.utils.bounded_executor import BoundedExecutor
    from mcp_service.configs.loader import CONFIG
else:
    from bridge.utils.bounded_executor import BoundedExecutor
    from bridge.configs.loader import CONFIG

executor: BoundedExecutor = BoundedExecutor(CONFIG["lark"].get("requestConcurrency", 8))


async def call_lark(operation: Callable[..., T], *args: Any) -> T:
    """使用服务专属的受限线程池和配置超时执行同步飞书请求，返回原始响应。

    不检查业务响应是否成功；容量耗尽、等待超时和底层异常向调用方传播。
    """
    return await executor.call(operation, *args,
                               timeout=CONFIG["codex"]["watchdog"]["cardRequestTimeoutSeconds"])

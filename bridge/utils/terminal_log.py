"""Consistent console output for application logs."""

from __future__ import annotations

import logging
import re


_logger = logging.getLogger("bridge")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(source)s] [%(asctime)s] [%(levelname)s] %(message)s",
        defaults={"source": "APP"},
    ))
    _logger.addHandler(_handler)
_handler = _logger.handlers[0]
_logger.propagate = False


class RedactingFormatter(logging.Formatter):
    """Preserve SDK DEBUG logs and their original format while masking connection credentials."""

    def __init__(self, original: logging.Formatter | None) -> None:
        """保留原日志 formatter，没有原 formatter 时使用默认实现。"""
        super().__init__()
        self.original = original or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        """沿用原 formatter 生成文本，再遮蔽 URL 查询参数中的 access_key、ticket 和 access_token 值。"""
        return re.sub(r"(?i)([?&](?:access_key|ticket|access_token)=)[^&\s]+",
                      r"\1[REDACTED]", self.original.format(record))


def protect_sdk_logs(sdk_logger: logging.Logger) -> None:
    """为 SDK Logger 已有 handler 安装去重的脱敏 formatter，不改变日志等级或添加 handler。"""
    for handler in sdk_logger.handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))


def configure_logging(level: str) -> None:
    """由启动入口配置业务等级，不改动 SDK 等级。"""
    _logger.setLevel(level.upper())
    for handler in _logger.handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))


def get_logger(source: str) -> logging.LoggerAdapter:
    """为共享 Logger 绑定固定来源，避免并发调用互相覆盖标识。"""
    return logging.LoggerAdapter(_logger, {"source": source})

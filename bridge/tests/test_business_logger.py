"""Application log levels are independent of SDK DEBUG, and connection credentials are excluded from output."""

from __future__ import annotations

import importlib.util
import io
import logging
from pathlib import Path
import unittest

from bridge.utils import terminal_log as codex_logger


class BusinessLoggerTests(unittest.TestCase):
    def test_levels_traceback_redaction_and_sdk_debug(self) -> None:
        path = Path(__file__).resolve().parents[2] / "mcp_service/utils/terminal_log.py"
        spec = importlib.util.spec_from_file_location("mcp_logger_test", path)
        mcp_logger = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp_logger)
        for module in (codex_logger, mcp_logger):
            business = module.get_logger("TEST")
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            previous_handlers = business.logger.handlers[:]
            previous_level = business.logger.level
            sdk = logging.Logger("test-sdk", logging.DEBUG)
            sdk.addHandler(handler)
            try:
                business.logger.handlers = [handler]
                module.configure_logging("DEBUG")
                business.debug("Application DEBUG is visible")
                module.configure_logging("INFO")
                business.debug("Must not appear")
                try:
                    raise ValueError("Exception traceback")
                except ValueError:
                    business.exception("Application failure")
                module.protect_sdk_logs(sdk)
                sdk.debug("wss://example.invalid/ws?access_key=secret&ticket=hidden&aid=1")
                text = stream.getvalue()
                self.assertIn("Application DEBUG is visible", text)
                self.assertNotIn("Must not appear", text)
                self.assertIn("Traceback", text)
                self.assertNotIn("secret", text)
                self.assertNotIn("hidden", text)
                self.assertIn("aid=1", text)
                self.assertEqual(sdk.level, logging.DEBUG)
            finally:
                business.logger.handlers = previous_handlers
                business.logger.setLevel(previous_level)

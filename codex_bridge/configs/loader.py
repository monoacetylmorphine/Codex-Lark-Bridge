"""Read the shared runtime configuration and validate it against the shared Schema."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
# Symlinked modules select dependencies by importing package; MCP does not require Codex runtime modules.
if __package__ == "mcp_service.configs":
    from mcp_service.configs.validation import load_config
else:
    from codex_bridge.configs.validation import load_config


PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
DATA_ROOT: Path = Path.home() / ".codex_bridge"
ENV_FILE: Path = DATA_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)
CONFIG_FILE: Path = Path(os.environ.get("CONFIG_FILE", str(DATA_ROOT / "config.json"))).expanduser()
SCHEMA_FILE: Path = PROJECT_ROOT / "configs" / "config_schema.json"


def _load_config(file_path: str | Path) -> dict[str, Any]:
    """加载并校验配置；在 Codex 包中相对配置文件目录解析存储路径，MCP 包保留配置原值。"""
    file_path = Path(file_path).expanduser()
    config = load_config(file_path, SCHEMA_FILE)
    # Only Codex resolves persistence paths; MCP keeps the original configuration values.
    if __package__ != "mcp_service.configs":
        for key in ("runLogPath", "databasePath", "workspaceRoot"):
            path = Path(config["storage"][key]).expanduser()
            if not path.is_absolute():
                path = file_path.resolve().parent / path
            config["storage"][key] = str(path)
    return config

CONFIG: dict[str, Any] = _load_config(CONFIG_FILE)

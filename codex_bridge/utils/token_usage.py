from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger

logger = get_logger("Usage")


DB_PATH = CONFIG["storage"]["databasePath"]
TABLE_NAME = CONFIG["logging"]["tokenUsageTable"]


DEFAULT_VALUES: dict[str, str | int] = {
    "timeStamp": "",
    "userId": "",
    "threadId": "",
    "model": "",
    "taskDuration_ms": 0,
    "cache_write_input_tokens": 0,
    "cached_input_tokens": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0,
    "runId": "",
}


REQUIRED_KEYS: tuple[str, ...] = tuple(DEFAULT_VALUES)


CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timeStamp TEXT NOT NULL,
    userId TEXT,
    threadId TEXT,
    model TEXT,
    taskDuration_ms INTEGER,
    cache_write_input_tokens INTEGER,
    cached_input_tokens INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    runId TEXT NOT NULL DEFAULT ''
)
"""


INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (
    timeStamp,
    userId,
    threadId,
    model,
    taskDuration_ms,
    cache_write_input_tokens,
    cached_input_tokens,
    input_tokens,
    output_tokens,
    reasoning_output_tokens,
    total_tokens,
    runId
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def ensure_keys(log: dict[str, Any]) -> dict[str, Any]:
    """返回日志字典的浅拷贝并为缺失键补默认值，不修改原字典，也不校验已有字段值的类型或范围。"""
    fixed_log = log.copy()

    for key in REQUIRED_KEYS:
        if key not in fixed_log:
            fixed_log[key] = DEFAULT_VALUES[key]
            logger.debug("Missing field %s; filled with default value %s", key, DEFAULT_VALUES[key])

    return fixed_log


async def SavingLog(log: dict[str, Any]) -> None:
    """将一条补齐字段的用量记录写入配置的 SQLite 表并提交。

    启用 WAL，按需建表，并在写事务中为旧表补充 runId 列；不合并或去重用量记录。
    SQLite 异常记录后重新抛出，不输出 CSV 文件。
    """
    fixed_log = ensure_keys(log)

    try:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(DB_PATH) as db:

            async with db.execute("PRAGMA journal_mode=WAL") as cursor:
                if (await cursor.fetchone())[0] != "wal":
                    raise aiosqlite.OperationalError("Unable to enable SQLite WAL mode")
            await db.execute(CREATE_TABLE_SQL)
            # Serialize legacy table checks and column additions within a write transaction to avoid duplicate migrations on concurrent first writes.
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(f"PRAGMA table_info({TABLE_NAME})") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if "runId" not in columns:
                await db.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN runId TEXT NOT NULL DEFAULT ''")

            await db.execute(
                INSERT_SQL,
                (
                    fixed_log["timeStamp"],
                    fixed_log["userId"],
                    fixed_log["threadId"],
                    fixed_log["model"],
                    fixed_log["taskDuration_ms"],
                    fixed_log["cache_write_input_tokens"],
                    fixed_log["cached_input_tokens"],
                    fixed_log["input_tokens"],
                    fixed_log["output_tokens"],
                    fixed_log["reasoning_output_tokens"],
                    fixed_log["total_tokens"],
                    fixed_log["runId"],
                ),
            )

            await db.commit()
            logger.info("Usage : detail=%s", log)
    except aiosqlite.Error as exc:
        logger.exception("Database operation failed")
        raise


async def finalize_usage(run_id: str, duration_ms: int | None) -> None:
    """任务退出时仅回填已知耗时，不新增用量记录或导出文件。"""
    if duration_ms is not None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                f"UPDATE {TABLE_NAME} SET taskDuration_ms = ? WHERE runId = ?",
                (duration_ms, run_id),
            )
            await db.commit()

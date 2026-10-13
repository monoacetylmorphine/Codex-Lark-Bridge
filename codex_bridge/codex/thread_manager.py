"""Persist user-to-Codex thread bindings in the storage.databasePath database."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3

import aiosqlite

from codex_bridge.configs.loader import CONFIG


DB_PATH = CONFIG["storage"]["databasePath"]
# 来源：沿用原连接的 30 秒锁等待预算，以及 session_history 的 50ms 初始化退避策略。
INITIALIZATION_TIMEOUT = 30.0
INITIALIZATION_RETRY_DELAY = 0.05


CREATE_TABLE_SQL = """
            CREATE TABLE IF NOT EXISTS user_thread (
                user_id TEXT PRIMARY KEY NOT NULL,
                thread_id TEXT
            )
        """

GET_USER_THREAD_SQL = "SELECT thread_id FROM user_thread WHERE user_id = ?"

SET_USER_THREAD_SQL = """
            INSERT INTO user_thread (user_id, thread_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET thread_id = excluded.thread_id
        """


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    """有界重试 WAL 和建表初始化竞争；连接交给调用方后不重放业务操作。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + INITIALIZATION_TIMEOUT
    while True:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            try:
                # 初始化使用统一期限，避免每次 SQLite 调用分别等待完整 30 秒。
                await db.execute("PRAGMA busy_timeout=0")
                async with db.execute("PRAGMA journal_mode=WAL") as cursor:
                    if (await cursor.fetchone())[0] != "wal":
                        raise aiosqlite.OperationalError("Unable to enable SQLite WAL mode")
                await db.execute(CREATE_TABLE_SQL)
                await db.commit()
                await db.execute("PRAGMA busy_timeout=30000")
            except aiosqlite.OperationalError as error:
                # 只重试其他连接造成的 BUSY（含扩展码），不吞掉 I/O 或同连接错误。
                code = getattr(error, "sqlite_errorcode", None)
                if code is None or code & 0xff != sqlite3.SQLITE_BUSY or loop.time() >= deadline:
                    raise
            else:
                # yield 在异常捕获范围之外，业务 SQL/commit 失败不会被重新执行。
                yield db
                return
        # 先关闭失败连接，再让出事件循环；取消直接向上传播。
        await asyncio.sleep(min(INITIALIZATION_RETRY_DELAY, max(0.0, deadline - loop.time())))


async def get_user_thread(user_id: str) -> str | None:
    """查询用户绑定；用户不存在或绑定已重置时返回 None。"""
    async with _connect() as db:
        async with db.execute(
            GET_USER_THREAD_SQL,
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row is not None else None


async def set_user_thread(user_id: str, thread_id: str | None) -> None:
    """原子新增或更新绑定；传入 None 持久化重置，提交成功后才返回。"""
    async with _connect() as db:
        await db.execute(SET_USER_THREAD_SQL, (user_id, thread_id))
        await db.commit()

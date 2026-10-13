"""Session history shares the active-binding database, keeps names fixed, and uses SDK timestamps for recency ordering."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

import aiosqlite
import regex
from openai_codex import TextInput

from codex_bridge.configs.loader import CONFIG

if TYPE_CHECKING:
    from openai_codex import InputItem


DB_PATH = CONFIG["storage"]["databasePath"]
# 来源：沿用原连接的 30 秒锁等待预算；50ms 为初始化竞争的异步退避间隔。
INITIALIZATION_TIMEOUT = 30.0
INITIALIZATION_RETRY_DELAY = 0.05


CREATE_TABLE_SQL = """
            CREATE TABLE IF NOT EXISTS session_history (
                user_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                thread_name TEXT,
                updated_at INTEGER,
                PRIMARY KEY (user_id, thread_id)
            )
        """

CREATE_INDEX_SQL = """
            CREATE INDEX IF NOT EXISTS idx_session_history_user_time
            ON session_history (user_id, updated_at DESC)
        """

REGISTER_SESSION_SQL = """
            INSERT INTO session_history (user_id, thread_id, thread_name)
            VALUES (?, ?, ?) ON CONFLICT(user_id, thread_id) DO NOTHING
        """

GET_SESSION_SQL = """
            SELECT user_id, thread_id, thread_name, updated_at
            FROM session_history WHERE user_id = ? AND thread_id = ?
        """

LIST_SESSIONS_SQL = """
            SELECT user_id, thread_id, thread_name, updated_at
            FROM session_history WHERE user_id = ?
            ORDER BY updated_at DESC, thread_id DESC
        """

CLAIM_SESSION_NAME_SQL = """
                UPDATE session_history SET thread_name = ?, updated_at = NULL
                WHERE user_id = ? AND thread_id = ? AND thread_name IS NULL
            """

UPDATE_SESSION_TIME_SQL = """
            UPDATE session_history SET updated_at = ?
            WHERE user_id = ? AND thread_id = ?
              AND (updated_at IS NULL OR updated_at < ?)
        """


@dataclass(frozen=True)
class SessionRecord:
    user_id: str
    thread_id: str
    thread_name: str | None
    updated_at: int | None


def make_thread_name(prompt: str | list[InputItem]) -> str | None:
    """提取 prompt 中的文本并合并连续空白，保留前 15 个 Unicode 字素簇，超长追加省略号；无文本返回 None。"""
    text = prompt if isinstance(prompt, str) else " ".join(
        item.text for item in prompt if isinstance(item, TextInput)
    )
    text = " ".join(text.split())
    return _truncate_thread_name(text)


def _truncate_thread_name(text: str) -> str | None:
    """返回至多 15 个 Unicode 字素簇组成的名称，超长追加省略号，空字符串返回 None。"""
    # Scan only up to the 16th grapheme cluster to avoid building a full character list for long prompts.
    clusters = []
    for match in regex.finditer(r"\X", text):
        clusters.append(match.group())
        if len(clusters) == 16:
            return "".join(clusters[:15]) + "…"
    return "".join(clusters) or None


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    """有界重试初始化锁竞争；连接交给调用方后不重放业务操作。"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + INITIALIZATION_TIMEOUT
    while True:
        async with aiosqlite.connect(DB_PATH, timeout=30) as db:
            try:
                # 初始化由异步期限统一管理，避免每条 DDL 各自等待完整 30 秒。
                await db.execute('PRAGMA busy_timeout=0')
                async with db.execute("PRAGMA journal_mode=WAL") as cursor:
                    if (await cursor.fetchone())[0] != "wal":
                        raise aiosqlite.OperationalError("Unable to enable SQLite WAL mode")
                await db.execute(CREATE_TABLE_SQL)
                await db.execute(CREATE_INDEX_SQL)
                await db.commit()
                # 调用方业务操作仍沿用原来的 SQLite 锁等待策略。
                await db.execute('PRAGMA busy_timeout=30000')
            except aiosqlite.OperationalError as error:
                # 只按 SQLite 错误码识别其他连接的竞争，不吞掉 I/O、SQL 或同连接错误。
                code = getattr(error, 'sqlite_errorcode', None)
                if code is None or code & 0xff != sqlite3.SQLITE_BUSY or loop.time() >= deadline:
                    raise
            else:
                # 业务代码抛出的异常不进入初始化重试分支。
                yield db
                return
        # 先关闭失败连接释放锁，再让出事件循环；取消直接向外传播。
        await asyncio.sleep(min(INITIALIZATION_RETRY_DELAY, max(0.0, deadline - loop.time())))


async def register_session(user_id: str, thread_id: str, prompt: str | list[InputItem]) -> None:
    """仅新线程调用；先固定首次名称，失败重试不会用后续输入覆盖。"""
    async with _connect() as db:
        await db.execute(REGISTER_SESSION_SQL, (user_id, thread_id, make_thread_name(prompt)))
        await db.commit()


async def get_session(user_id: str, thread_id: str) -> SessionRecord | None:
    """通过可信用户身份校验归属，不接受仅凭 thread_id 的查询。"""
    async with _connect() as db:
        async with db.execute(GET_SESSION_SQL, (user_id, thread_id)) as cursor:
            row = await cursor.fetchone()
    return SessionRecord(*row) if row else None


async def list_sessions(user_id: str) -> list[SessionRecord]:
    """时间降序；同秒用线程 ID 稳定排序，尚无 SDK 时间的记录排在末尾。"""
    async with _connect() as db:
        async with db.execute(LIST_SESSIONS_SQL, (user_id,)) as cursor:
            return [SessionRecord(*row) for row in await cursor.fetchall()]


async def claim_session_name(
    user_id: str,
    thread_id: str,
    prompt: str | list[InputItem],
) -> SessionRecord | None:
    """纯附件线程收到首次文本时固定候选名称，并标记等待 SDK 命名。"""
    name = make_thread_name(prompt)
    if name:
        async with _connect() as db:
            await db.execute(CLAIM_SESSION_NAME_SQL, (name, user_id, thread_id))
            await db.commit()
    return await get_session(user_id, thread_id)


async def update_session_time(user_id: str, thread_id: str, updated_at: int) -> None:
    """只更新已有记录的 SDK 时间，不改名称；过期响应不能使时间倒退。"""
    if type(updated_at) is not int or updated_at < 0:
        raise ValueError("SDK updated_at must be a non-negative integer timestamp")
    async with _connect() as db:
        await db.execute(UPDATE_SESSION_TIME_SQL, (updated_at, user_id, thread_id, updated_at))
        await db.commit()

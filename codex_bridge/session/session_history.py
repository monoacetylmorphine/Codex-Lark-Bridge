"""Session history shares the active-binding database, keeps names fixed, and uses Codex session_index timestamps for recency ordering."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sqlite3

import aiosqlite

from codex_bridge.configs.loader import CONFIG


DB_PATH = CONFIG["storage"]["databasePath"]
# 来源：Codex 应用数据目录；与容器内 HOME=/home/app 对应，即挂载的主机 ~/.codex。
SESSION_INDEX_PATH = Path.home() / ".codex" / "session_index.jsonl"
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
                UPDATE session_history SET thread_name = ?
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


@dataclass(frozen=True)
class SessionIndexEntry:
    thread_name: str | None
    updated_at: int | None


def _parse_index_updated_at(value: str | None) -> int | None:
    """将 session_index 的 ISO 时间解析为 Unix 秒；缺失、空值或非法格式返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    timestamp = int(parsed.timestamp())
    return timestamp if timestamp >= 0 else None


def _read_session_index_entry(thread_id: str) -> SessionIndexEntry:
    """同步读取 Codex session_index.jsonl，返回最新匹配项的名称与 ISO 时间换算的 Unix 秒。"""
    try:
        lines = SESSION_INDEX_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return SessionIndexEntry(None, None)
    # JSONL 按追加写入，倒序取最新一条重复 id 的名称；损坏行直接跳过。
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("id") == thread_id:
            name = entry.get("thread_name")
            name = name.strip() if isinstance(name, str) and name.strip() else None
            return SessionIndexEntry(name, _parse_index_updated_at(entry.get("updated_at")))
    return SessionIndexEntry(None, None)


async def lookup_session_index(thread_id: str) -> SessionIndexEntry:
    """非阻塞读取 Codex session_index.jsonl，返回会话名称与 ISO 时间换算的 Unix 秒。"""
    return await asyncio.to_thread(_read_session_index_entry, thread_id)


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


async def register_session(user_id: str, thread_id: str) -> None:
    """仅新线程调用；名称来自 Codex session_index，尚未生成时先登记空名称。"""
    entry = await lookup_session_index(thread_id)
    async with _connect() as db:
        await db.execute(REGISTER_SESSION_SQL, (user_id, thread_id, entry.thread_name))
        await db.commit()


async def get_session(user_id: str, thread_id: str) -> SessionRecord | None:
    """通过可信用户身份校验归属，不接受仅凭 thread_id 的查询。"""
    async with _connect() as db:
        async with db.execute(GET_SESSION_SQL, (user_id, thread_id)) as cursor:
            row = await cursor.fetchone()
    return SessionRecord(*row) if row else None


async def list_sessions(user_id: str) -> list[SessionRecord]:
    """时间降序；同秒用线程 ID 稳定排序，尚无排序时间的记录排在末尾。"""
    async with _connect() as db:
        async with db.execute(LIST_SESSIONS_SQL, (user_id,)) as cursor:
            return [SessionRecord(*row) for row in await cursor.fetchall()]


async def sync_session_index(
    user_id: str,
    thread_id: str,
) -> SessionRecord | None:
    """从 Codex session_index 同步名称与排序时间；未登记的线程不创建记录。"""
    record = await get_session(user_id, thread_id)
    if record is None:
        return None
    entry = await lookup_session_index(thread_id)
    if entry.thread_name:
        async with _connect() as db:
            await db.execute(CLAIM_SESSION_NAME_SQL, (entry.thread_name, user_id, thread_id))
            await db.commit()
    if entry.updated_at is not None:
        await update_session_time(user_id, thread_id, entry.updated_at)
    return await get_session(user_id, thread_id)


async def update_session_time(user_id: str, thread_id: str, updated_at: int) -> None:
    """只更新已有记录的排序时间，不改名称；过期响应不能使时间倒退。"""
    if type(updated_at) is not int or updated_at < 0:
        raise ValueError("updated_at must be a non-negative integer timestamp")
    async with _connect() as db:
        await db.execute(UPDATE_SESSION_TIME_SQL, (updated_at, user_id, thread_id, updated_at))
        await db.commit()

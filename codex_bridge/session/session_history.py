"""Persistent session history: registration, recency from Codex thread_items, and LLM-generated titles."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

import aiosqlite
from openai import AsyncOpenAI

from codex_bridge.configs.loader import CONFIG
from codex_bridge.utils.terminal_log import get_logger


DB_PATH = CONFIG["storage"]["databasePath"]
# 来源：Codex 应用数据目录；与容器内 HOME=/home/app 对应，即挂载的主机 ~/.codex。
THREAD_HISTORY_DB_PATH = Path.home() / ".codex" / "thread_history_1.sqlite"
# 来源：沿用原连接的 30 秒锁等待预算；50ms 为初始化竞争的异步退避间隔。
INITIALIZATION_TIMEOUT = 30.0
INITIALIZATION_RETRY_DELAY = 0.05

logger = get_logger("SessionTitle")


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
            VALUES (?, ?, NULL) ON CONFLICT(user_id, thread_id) DO NOTHING
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

EARLIEST_TURN_SQL = """
            SELECT turn_id, created_at_ms FROM thread_items
            WHERE thread_id = ? AND created_at_ms IS NOT NULL
            ORDER BY created_at_ms ASC, item_id ASC
            LIMIT 1
        """

TURN_TITLE_ITEMS_SQL = """
            SELECT item_json FROM thread_items
            WHERE thread_id = ? AND turn_id = ?
              AND item_type IN ('userMessage', 'agentMessage')
            ORDER BY created_at_ms ASC, rollout_ordinal ASC
        """


@dataclass(frozen=True)
class SessionRecord:
    user_id: str
    thread_id: str
    thread_name: str | None
    updated_at: int | None


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


@asynccontextmanager
async def _connect_thread_history() -> AsyncIterator[aiosqlite.Connection]:
    """以只读方式打开 Codex thread_history 数据库；不建表、不写 WAL。"""
    if not THREAD_HISTORY_DB_PATH.is_file():
        raise FileNotFoundError(f"Thread history database does not exist: {THREAD_HISTORY_DB_PATH}")
    async with aiosqlite.connect(f"file:{THREAD_HISTORY_DB_PATH}?mode=ro", uri=True) as db:
        yield db


async def register_session(user_id: str, thread_id: str) -> None:
    """仅新线程调用；先登记空名称，标题在 turn 完成后由 ensure_session_title 生成。"""
    async with _connect() as db:
        await db.execute(REGISTER_SESSION_SQL, (user_id, thread_id))
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


async def _read_earliest_turn(thread_id: str) -> tuple[str, int] | None:
    """读取 thread_items 中该 thread 最早一行对应的 turn_id 与 created_at_ms；失败返回 None。"""
    try:
        async with _connect_thread_history() as db:
            async with db.execute(EARLIEST_TURN_SQL, (thread_id,)) as cursor:
                row = await cursor.fetchone()
    except (OSError, sqlite3.Error):
        return None
    if row is None:
        return None
    turn_id, created_at_ms = row
    if not isinstance(turn_id, str) or type(created_at_ms) is not int or created_at_ms < 0:
        return None
    return turn_id, created_at_ms


async def sync_session_time(user_id: str, thread_id: str) -> SessionRecord | None:
    """从 thread_items 最早 turn 的 created_at_ms 同步排序时间；未登记的线程不创建记录。"""
    record = await get_session(user_id, thread_id)
    if record is None:
        return None
    turn = await _read_earliest_turn(thread_id)
    if turn is not None:
        _, created_at_ms = turn
        await update_session_time(user_id, thread_id, created_at_ms)
    return await get_session(user_id, thread_id)


async def update_session_time(user_id: str, thread_id: str, updated_at: int) -> None:
    """只更新已有记录的排序时间，不改名称；过期响应不能使时间倒退。"""
    if type(updated_at) is not int or updated_at < 0:
        raise ValueError("updated_at must be a non-negative integer timestamp")
    async with _connect() as db:
        await db.execute(UPDATE_SESSION_TIME_SQL, (updated_at, user_id, thread_id, updated_at))
        await db.commit()


def _extract_item_text(item_json: str) -> str:
    """从 thread_items.item_json 提取文本；只认 userMessage 的 content[].text 与 agentMessage 的 text。"""
    try:
        item = json.loads(item_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(item, dict):
        return ""
    if item.get("type") == "userMessage":
        parts: list[str] = []
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    stripped = text.strip()
                    if stripped:
                        parts.append(stripped)
        return "\n".join(parts)
    if item.get("type") == "agentMessage":
        text = item.get("text")
        return text.strip() if isinstance(text, str) else ""
    return ""


async def _read_turn_title_texts(thread_id: str, turn_id: str) -> list[str]:
    """读取最早 turn 中 userMessage/agentMessage 的文本；失败返回空列表。"""
    try:
        async with _connect_thread_history() as db:
            async with db.execute(TURN_TITLE_ITEMS_SQL, (thread_id, turn_id)) as cursor:
                rows = await cursor.fetchall()
    except (OSError, sqlite3.Error):
        return []
    texts: list[str] = []
    for (item_json,) in rows:
        if isinstance(item_json, str):
            text = await asyncio.to_thread(_extract_item_text, item_json)
            if text.strip():
                texts.append(text)
    return texts


def _build_title_input(texts: list[str], max_chars: int) -> str:
    combined = "\n\n---\n\n".join(texts)
    return combined[:max_chars]


def _extract_output_text(response: object) -> str:
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []):
            if getattr(content, "type", None) == "output_text":
                return str(content.text).strip()
    return ""


def _sanitize_title(title: str, max_length: int) -> str | None:
    title = " ".join(title.split())
    if not title:
        return None
    return title[:max_length].strip() or None


async def _generate_title(texts: list[str], config: dict[str, Any]) -> str | None:
    """用配置的模型把会话文本总结成标题；缺凭据或响应无文本时返回 None。"""
    api_key = os.getenv(config["apiKeyEnv"])
    if not api_key:
        logger.warning("%s is not configured; session title generation is skipped", config["apiKeyEnv"])
        return None
    user_prompt = config.get("userPrompt") or "Summarize this session in a short title."
    instructions = config.get("instructions") or "You are a helpful assistant."
    max_chars = int(config.get("maxInputChars", 4000))
    max_length = int(config.get("titleMaxLength", 60))
    user_input = _build_title_input(texts, max_chars)
    async with AsyncOpenAI(api_key=api_key, base_url=config["baseUrl"]) as client:
        response = await client.responses.create(
            model=config["model"],
            instructions=instructions,
            input=f"{user_prompt}\n\n{user_input}",
        )
    return _sanitize_title(_extract_output_text(response), max_length)


async def claim_session_title(user_id: str, thread_id: str, title: str) -> bool:
    """仅在名称仍为空时写入标题；返回是否成功认领，名称一旦写入不再改变。"""
    title = " ".join(title.split())
    if not title:
        return False
    async with _connect() as db:
        cursor = await db.execute(CLAIM_SESSION_NAME_SQL, (title, user_id, thread_id))
        await db.commit()
        return cursor.rowcount == 1


async def ensure_session_title(user_id: str, thread_id: str) -> bool:
    """名称仍为空时，按最早 turn 的 userMessage/agentMessage 内容生成并写入标题。

    单次只调用一次模型；任何失败或无可提取内容都保留 NULL，等待下一次 turn 完成后重试。
    """
    try:
        record = await get_session(user_id, thread_id)
        if record is None or record.thread_name is not None:
            return False
        config = CONFIG.get("sessionTitleGen")
        if not isinstance(config, dict):
            return False
        turn = await _read_earliest_turn(thread_id)
        if turn is None:
            return False
        turn_id, _ = turn
        texts = await _read_turn_title_texts(thread_id, turn_id)
        if not texts:
            return False
        title = await _generate_title(texts, config)
        if not title:
            return False
        return await claim_session_title(user_id, thread_id, title)
    except Exception:
        logger.exception("Session title generation failed: user_id=%s, thread_id=%s", user_id, thread_id)
        return False

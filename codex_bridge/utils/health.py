"""网关心跳与 SDK 线程桥接；不加载配置或读取凭据。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import threading
import time

# 来源：容器内独立 /tmp；每秒刷新，允许十秒调度延迟，不能复用过期心跳。
HEALTH_FILE = Path('/tmp/codex_bridge-health.json')
MAX_AGE = 10


def connected(client: object) -> bool:
    """检查 SDK 当前连接；SDK 接口变化时失败关闭，不误报 ready。"""
    # 来源：lark-oapi Client._conn 与 websockets 的连接状态；升级时由测试覆盖。
    connection = getattr(client, '_conn', None)
    if connection is None:
        return False
    state = getattr(connection, 'state', None)
    return getattr(state, 'name', None) == 'OPEN' or getattr(connection, 'closed', None) is False


def write_health(ready: bool, path: Path = HEALTH_FILE) -> None:
    """原子发布当前进程的心跳，内容不包含凭据或连接 URL。"""
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid': os.getpid(), 'time': time.monotonic(), 'ready': ready}))
    temporary.replace(path)


def healthy(path: Path = HEALTH_FILE) -> bool:
    """要求进程仍存活、心跳新鲜且连接可用。"""
    try:
        state = json.loads(path.read_text())
        age = time.monotonic() - state['time']
        if state['ready'] is not True or not 0 <= age <= MAX_AGE or state['pid'] <= 0:
            return False
        os.kill(state['pid'], 0)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


async def heartbeat(client: object) -> None:
    """主事件循环阻塞或 SDK 断连时，健康检查会失败。"""
    try:
        while True:
            write_health(connected(client))
            await asyncio.sleep(1)
    finally:
        write_health(False)


async def run_websocket(start) -> None:
    """保留 SDK 独立事件循环；守护线程不阻止主进程结束。

    SDK start 没有公开的停止接口。停止时由主网关关闭入站并收尾任务，
    进程退出最终回收连接；不调用 SDK 私有 disconnect 或修改全局循环。
    """
    loop = asyncio.get_running_loop()
    completed = loop.create_future()

    def deliver(error):
        if not completed.done():
            if error is None:
                completed.set_result(None)
            else:
                completed.set_exception(error)

    def worker():
        error = None
        try:
            start()
        except BaseException as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(deliver, error)
        except RuntimeError:
            pass  # 主循环已关闭，守护线程随进程结束。

    threading.Thread(target=worker, name='lark-websocket', daemon=True).start()
    await completed


if __name__ == '__main__':
    raise SystemExit(0 if healthy() else 1)

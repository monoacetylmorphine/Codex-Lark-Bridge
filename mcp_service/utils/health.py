"""通过真实 MCP 协议检查工具注册，不执行任何业务工具。"""

from __future__ import annotations

import asyncio
import os

from mcp import Client


async def probe(url: str) -> None:
    """握手和工具列表须在八秒内完成；超时来源为十秒容器探测预算。"""
    async with asyncio.timeout(8):
        async with Client(url) as client:
            result = await client.list_tools()
            names = {tool.name for tool in result.tools}
            if not {'sending_file', 'image_generator'} <= names:
                raise RuntimeError('MCP tools are not ready')


def main() -> None:
    from mcp_service.configs.loader import CONFIG
    settings = CONFIG['mcp']
    if settings['transport'] != 'streamable-http':
        raise RuntimeError('Production healthcheck requires streamable-http')
    port = int(os.getenv('MCP_PORT', str(settings['port'])))
    path = os.getenv('MCP_PATH', settings['path'])
    asyncio.run(probe(f'http://127.0.0.1:{port}{path}'))


if __name__ == '__main__':
    main()

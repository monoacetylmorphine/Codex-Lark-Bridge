import os
import logging
import sys
from pathlib import Path

from mcp.server import MCPServer

# 同时支持源码目录中的 ``python server.py``，以及包方式启动。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_service.configs.loader import CONFIG
from mcp_service.utils.terminal_log import configure_logging
from mcp_service.tools.lark_tools.sending_file import sending_file
from mcp_service.tools.internal_tools.commodity_sale_analysis import (
    commodity_sale_analysis,
)
from mcp_service.tools.internal_tools.text2image import image_generator

MCP_CONFIG = CONFIG["mcp"]
logging.basicConfig(level=MCP_CONFIG["logLevel"].upper())
configure_logging(MCP_CONFIG["logLevel"])


mcp = MCPServer(
    name=MCP_CONFIG["name"],
    description=MCP_CONFIG["description"],
    version=MCP_CONFIG["version"],
)

mcp.add_tool(
    sending_file,
    name="sending_file",
    title="发送文件到飞书",
    description=(
        "将工作空间Workspace的文件(绝对路径)发送给飞书用户"
        "文件路径中必须包含唯一的 on_ / oc_ 接收方目录"
    ),
)

mcp.add_tool(
    image_generator,
    name="image_generator",
    title="文生图",
    description=(
        "基于提示词进行文生图"
        "成功生成时返回图像URL"
    ),
)

mcp.add_tool(
    commodity_sale_analysis,
    name="commodity_sale_analysis",
    title="销售数据分析",
    description="销售数据分析",
)


def main() -> None:
    mcp.run(
        transport=MCP_CONFIG["transport"],
        host=os.getenv("MCP_HOST", MCP_CONFIG["host"]),
        port=int(os.getenv("MCP_PORT", str(MCP_CONFIG["port"]))),
        streamable_http_path=os.getenv("MCP_PATH", MCP_CONFIG["path"]),
    )


if __name__ == "__main__":
    main()

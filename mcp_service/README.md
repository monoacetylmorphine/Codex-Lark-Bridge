# MCP

MCP 是独立运行的工具服务，默认通过 Streamable HTTP 向 Codex 或其他 MCP 客户端提供两个工具：**将本地文件发送到飞书，以及根据提示词生成图片 URL**。

它不接收飞书对话、不运行 Codex 任务，也不管理聊天会话。对话入口由 [Bridge](../bridge/README.md) 提供；整体部署及网络说明见[仓库 README](../README.md)。两个服务独立启动，客户端需要自行注册 MCP 连接。

## 工具接口

| 工具 | 输入 | 成功结果 |
| --- | --- | --- |
| `sending_file` | `file_path: str`，服务可读取的绝对文件路径 | 包含 `recipient_id`、`file_key`、`message_id` 的对象 |
| `image_generator` | `prompt: str`，非空图片描述 | 图像服务返回的首张图片 URL 字符串 |

### 文件发送

`sending_file` 先展开并解析路径，确认目标是普通文件，再从父目录中识别接收方，上传文件并发送飞书文件消息。

- 接收方目录须完整匹配 `(?:on_|oc_)[A-Za-z0-9]+`，且含前缀总长为 35 个字符；长度来自当前实现采用的参考 ID 规则。
- 解析后的路径中只能识别到一个不同接收方。`on_` 使用飞书 `union_id`，`oc_` 使用 `chat_id`。
- 文件必须在 **MCP 服务所在环境**可读。宿主机路径不等于容器路径；与 Codex 共用工作区挂载后，应传容器内的绝对路径。
- 上传类型由 `lark.upload.nativeFileTypes` 决定，未列出的扩展名使用 `stream`。这是发送规则，不等于网关的入站附件白名单。
- 成功需同时取得上传 `file_key` 和发送 `message_id`。API 失败或响应缺项会抛出异常，不以“上传成功”代替“发送完成”。

**接收方目录不是授权机制。** 当前工具未校验调用者身份，也未限制文件一定属于 `storage.workspaceRoot`。能连接服务的调用者可能指定其他服务可读文件，因此只能向受信任的客户端开放。

发送超时后结果可能不确定，工具不会自动重试整次发送；先核对飞书是否已经收到，避免重复交付。

### 文生图

`image_generator` 使用 `mcp.imageModel` 指定的 API、模型和密钥环境变量，通过 `AsyncOpenAI.images.generate` 请求图片。当前请求固定使用 `2K`、URL 响应，以及提供方特定的生成参数，返回第一张图片的 URL。

工具不下载图片、不将图片存入工作区，也不自动发送到飞书。它只接收文本提示词，不提供图片编辑或视频生成；存在 `mcp.videoModel` 配置不代表服务注册了视频工具。切换提供方前需核实其接口能接受当前参数。

## 配置与凭据

默认业务配置为 `~/.codex_bridge/config.json`，可通过 `CONFIG_FILE` 指定其他已有文件。配置定义见[共享默认配置](../bridge/configs/config_default.json)和 [Schema](../bridge/configs/config_schema.json)。服务启动时完整校验 Schema，再注册工具；不会因为暂时缺少某个工具的 API 凭据而拒绝启动。

应用只加载 `~/.codex_bridge/.env`，已有进程环境变量优先；指定其他 JSON 路径不会改变 dotenv 的位置。不要把真实密钥写入 JSON 或仓库。

| 字段或变量 | 默认值及用途 |
| --- | --- |
| `mcp.transport` | `streamable-http`；当前生产健康检查要求此模式 |
| `mcp.host` / `MCP_HOST` | 默认 `127.0.0.1`，环境变量可覆盖；Compose 显式设为 `0.0.0.0` 供容器外连接 |
| `mcp.port` / `MCP_PORT` | 默认 8000，环境变量可覆盖监听端口 |
| `mcp.path` / `MCP_PATH` | 默认 `/mcp`，环境变量可覆盖 |
| `mcp.logLevel` | 业务日志等级，默认 `DEBUG` |
| `mcp.imageModel` | 文生图的模型、`baseUrl` 及 `apiKeyEnv`；默认密钥变量为 `DOUBAO_API_KEY` |
| `lark.credentials` | 文件工具使用的飞书变量名，默认 `LARK_APP_ID`、`LARK_APP_SECRET` |
| `lark.upload` | 飞书原生上传类型和后备类型 |
| `lark.requestConcurrency` | 默认每进程最多 8 个实际在途飞书请求 |
| `codex.watchdog.cardRequestTimeoutSeconds` | 共享飞书 API 超时，默认 10 秒；MCP 不启动 Codex watchdog |

文生图在每次调用时校验模型参数及密钥；飞书客户端在调用文件工具时才初始化。缺少图像凭据只影响图像调用，缺少飞书凭据只影响文件调用，但任意配置段的 JSON/Schema 错误仍会阻止服务启动。修改配置或凭据文件后需重启服务。

## 启动与连接

以下命令均从**完整仓库根目录**执行。Python 版本要求为 `>=3.13,<3.14`；保留两个项目的相邻位置与相对符号链接。

### 源码运行

```sh
uv sync --project mcp_service --locked
# 只初始化缺失的默认业务配置，不覆盖现有配置
mcp_service/.venv/bin/python -B mcp_service/configs/initialization.py
mcp_service/.venv/bin/python -m mcp_service.server
```

也可在 `mcp_service/` 中执行 `.venv/bin/python server.py`，安装为包后使用 `mcp_service`。按需在 `~/.codex_bridge/.env` 中提供真实业务凭据；启动和协议检查成功并不代表工具凭据已有效。

### Docker

```sh
docker build -f mcp_service/Dockerfile -t mcp_service .

# 挂载目录须预先存在，且允许容器用户读取/写入
# 示例使用默认路径和监听端口；自定义配置须同步调整
mkdir -p "$HOME/.codex_bridge" "$HOME/.codex"
docker run --rm \
  -p 127.0.0.1:8000:8000 \
  -e MCP_HOST=0.0.0.0 \
  -e MCP_PORT=8000 \
  --mount "type=bind,src=$HOME/.codex_bridge,dst=/home/app/.codex_bridge" \
  --mount "type=bind,src=$HOME/.codex,dst=/home/app/.codex" \
  mcp_service
```

容器首次启动会原子初始化缺失的默认配置，已有配置不覆盖。自定义 `CONFIG_FILE` 对应的文件必须已经存在。默认镜像用户为 UID/GID 1000，单独构建可用 `APP_USER`、`APP_UID`、`APP_GID` 调整；根 Compose 将 `LOCAL_UID`、`LOCAL_GID` 映射为对应构建参数。

根 Compose 部署使用 `./compose_initial.sh up -d --build mcp_service`，需要先完成根 README 中的网络、挂载和部署凭据准备；仅启动该服务也会解析整个 Compose。

| 客户端所在位置 | 根 Compose 默认连接地址 |
| --- | --- |
| 宿主机 | `http://127.0.0.1:8000/mcp` |
| 同一 `ai-infra` 网络内的容器 | `http://mcp_service:8000/mcp` |

服务不会替客户端注册连接。容器中的 `127.0.0.1` 指向该容器自身；客户端必须使用自己能够访问的地址。

根 Compose 的 `MCP_PORT` 同时改变宿主机与容器端口，`MCP_BIND_ADDRESS` 默认只绑定宿主机回环地址。生产 Compose 则固定容器端口 8000，`host.json` 中的 `mcp_port` 只改变宿主机映射。不要混用两种端口约定，也不要将无鉴权服务直接暴露到公网。

## 代码与共享关系

```text
mcp_service/
├── server.py                   # MCP 服务入口、工具注册及监听配置
├── tools/
│   ├── lark_tools/sending_file.py
│   └── internal_tools/text2image.py
├── services/lark/              # 延迟初始化的飞书客户端及共享请求封装
├── configs/                    # 配置入口与共享文件的相对符号链接
├── utils/                      # 日志、有界执行器和协议健康检查
├── tests/test_runtime.py       # 工具、配置、打包与健康探测回归
└── Dockerfile、pyproject.toml、uv.lock
```

`configs/` 中的 `config_default.json`、`config_schema.json`、`loader.py`、`validation.py`、`initialization.py` 通过相对符号链接指向 `../../bridge/configs/`。`services/lark/lark_requests.py` 同样引用 Codex 的对应文件；`lark_client.py` 保持独立，以便延迟检查工具凭据。

两个服务分别加载共享源码，配置对象与线程池互不共享。MCP 保留配置中的存储路径原值，不承担 Codex 的工作区初始化。Dockerfile 保持两个源码目录在 `/opt/` 下相邻，使符号链接在构建时可解析；单独复制本项目或改成开发机绝对链接会破坏构建。

已安装 wheel 和镜像使用构建时的内容。修改共享源码后须重新安装或构建，并验证两个服务；仅修改挂载配置则重启即可。

## 健康检查、测试与运维边界

```sh
# 从仓库根目录运行，使用临时配置及模拟外部客户端
mcp_service/.venv/bin/python -B mcp_service/tests/test_runtime.py

# 已启动本地服务时，在同一运行环境中执行
mcp_service/.venv/bin/python -m mcp_service.utils.health

# 或检查根 Compose 内的服务
./compose_initial.sh exec mcp_service python -m mcp_service.utils.health
```

健康检查在 8 秒内完成本地 MCP 握手和工具列表读取，要求两个工具都已注册；不执行工具，不消耗模型额度，也不发送飞书消息。该超时来自容器探测预算，不是工具调用 SLA。根 Compose 未自动配置 healthcheck，生产 Compose 已配置。

飞书请求使用有界执行器，满额立即失败；调用方取消或超时后，已经开始的线程仍占用名额，直到真实请求结束。上传句柄由 worker 持有，避免超时后提前关闭；永久阻塞的线程无法由此机制强制终止。默认并发容量来自项目初始保护策略，未作为压测上限承诺。

默认 DEBUG 日志可能包含提示词片段、生成结果 URL 和运行信息，应限制访问并安排保留策略。MCP 的文件读取权限与挂载范围决定可暴露的数据，回环绑定也不能替代客户端授权控制。

CI 使用 Linux ARM64 构建和测试，默认 Docker 构建升级兼容依赖；CI 先解析本轮锁文件，再以 `UV_SYNC_FLAGS=--locked` 构建，Release 发布同一镜像。生产发布使用仓库根目录 [deploy.sh](../deploy.sh)，部署事务由 [test_deploy.py](../test_deploy.py) 验证。具体流程、备份及回退限制见 [CI/CD 指南](../CICD_GUIDE.md)。协议测试和模拟测试不能证明真实上传、模型或生产网络可用。

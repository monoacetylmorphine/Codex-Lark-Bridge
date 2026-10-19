# Codex-Lark Bridge

Codex-Lark Bridge 是通过飞书使用 Codex 的自托管助手：将私聊或群聊消息转换为 Codex 任务，在持久化工作区中处理代码、图片和文档，并通过飞书卡片持续返回进度与结果。适合受信任的小团队在自己的运行环境中使用。

仓库包含两个 Python 服务：

- **`codex_bridge`** 是飞书消息网关与任务运行器：接收私聊或群聊消息，下载和校验附件，准备持久化工作区，通过 Codex SDK 执行任务，并以飞书流式卡片交付进度和结果。
- **`mcp_service`** 是独立 MCP 工具服务：默认通过 Streamable HTTP 向 Codex 或其他 MCP 客户端提供两个工具——`sending_file` 把本地文件发送到飞书，`image_generator` 根据提示词生成图片 URL。

根 Compose 另带 Langfuse 及其存储组件；生产发布使用独立 Compose，只更新两个应用服务。两个服务独立启动，客户端需要自行注册 MCP 连接。

快速导航：[能做什么](#能做什么) · [项目结构](#项目结构) · [在飞书中使用](#如何在飞书中使用) · [代码职责](#运行链路与代码职责) · [配置与凭据](#配置与凭据) · [启动与连接](#启动与连接) · [MCP 工具接口](#mcp-工具接口) · [工作区初始化](#工作区初始化) · [会话与历史](#会话持久化与历史恢复) · [附件处理](#消息组装与附件处理) · [卡片定制](#流式卡片与样式定制) · [任务控制](#任务控制与资源回收) · [日志与用量](#日志与-token-用量) · [CI/CD](#cicd-与生产发布) · [测试与维护](#测试健康检查与维护) · [能力边界](#能力边界与注意事项)。

## 能做什么

| 能力 | 当前实现 |
| --- | --- |
| 飞书对话 | 通过 WebSocket 长连接接收消息；支持私聊，群聊要求当前消息提及机器人 |
| 多种输入 | 接收文本、富文本、图片、文件和语音；下载附件并校验格式，将音频转写为文本后交给 Codex |
| 连续任务 | 持久化用户与 Codex thread 的绑定；运行中追加消息可通过 steer 补充当前任务 |
| 进度与控制 | 流式卡片、处理中 reaction、`/stop`、`/new`、私聊 `/history`，以及关联消息撤回后的任务停止处理 |
| 工作区 | 按私聊用户或群建立目录，初始化 Git、Python 和 Node 文档处理依赖，保留附件与任务产物 |
| MCP 扩展 | `sending_file(file_path)` 上传并发送本地文件到飞书；`image_generator(prompt)` 调用图像模型并返回图片 URL |
| 运行记录 | SQLite 保存 thread 绑定、会话历史索引与 token 用量；业务日志写入配置的数据目录 |
| 交付与运维 | 两个 Docker 镜像、离线回归测试、GitHub Actions 验证与 GHCR 发布、手动 SSH 部署及有条件的镜像回退 |

代码执行、联网、文件生成和其他工具能力取决于实际配置的 Codex 模型、认证、沙箱及插件。Office 依赖提供执行基础，不保证任意文档都能无损解析或转换；仓库也不会自动安装或启用参考配置中的全部插件。

## 项目结构

```text
.
├── codex_bridge/                 # 主服务
│   ├── main.py                 # 启动、飞书事件注册、后台任务与退出处理
│   ├── middleware/             # 消息路由、附件组装、ASR、控制命令与卡片交付
│   ├── codex/                  # SDK 执行、模型路由、工作区、thread 与 watchdog
│   ├── session/                # 运行缓存、会话恢复与历史索引
│   ├── services/lark/          # 飞书 API、流式和交互卡片
│   ├── configs/                # 共享默认配置、Schema、加载与初始化逻辑
│   ├── utils/                  # 日志、用量、并发限制与健康检查
│   ├── tests/                 # 回归测试
│   └── Dockerfile、pyproject.toml、uv.lock
├── mcp_service/                   # 独立 MCP 服务及两个工具
│   ├── server.py                # Streamable HTTP MCP 入口与工具注册
│   ├── tools/                   # sending_file 与 image_generator
│   ├── services/lark/           # 延迟初始化的飞书客户端与请求封装
│   ├── configs/                 # 通过相对符号链接复用 codex_bridge 共享配置
│   ├── utils/                   # 日志、有界执行器与协议健康检查
│   ├── tests/test_runtime.py    # 工具、配置、打包与健康探测回归
│   └── Dockerfile、pyproject.toml、uv.lock
├── .github/workflows/          # CI、Release、Deploy
├── deploy.sh                   # 生产预检、备份、更新和失败处理
├── test_deploy.py              # 部署事务测试
├── docker-compose.yaml         # 源码构建的应用 + Langfuse 全栈
├── compose.production.yaml     # 基于发布镜像的两个应用
├── compose_initial.sh          # 根 Compose 包装器，首次创建部署 .env
├── entrypoint.sh               # 容器入口，首次初始化默认配置后启动服务
└── LICENSE                     # MIT 许可证
```

两个项目使用独立的 `pyproject.toml` 与 `uv.lock`，均要求 **Python 3.13 系列**。MCP 的五个配置文件及 `services/lark/lark_requests.py` 通过相对符号链接复用 Codex 源码。开发和 Docker 构建须保留完整仓库及链接，不能只复制 `mcp_service/`。两个进程仍各自加载配置和创建请求执行器。

## 如何在飞书中使用

1. 私聊机器人发送任务；群聊中先提及机器人。图片和文件默认进入 **10 秒固定缓冲窗口**，窗口内可继续发送附件；文本、富文本或语音到达时会立即触发历史组装。
2. 用自然语言说明要处理的内容和期望输出，例如“总结这份 PDF”或“分析表格并生成报告”。附件会保存到当前工作区的 `resources/inbound/`。
3. 运行中补充要求会尝试加入当前任务。若追加图片需要切换模型，当前任务无法直接切换；等待完成，或停止后将图片和任务说明一起重发。
4. 查看流式卡片；需要接收产物文件时，Codex 必须能调用已配置的 `sending_file`。仅生成文件或给出服务器路径，不代表文件已发送到飞书。

| 场景 | 当前行为 |
| --- | --- |
| 私聊 | 接收当前用户的任务，按飞书 Union ID 使用工作区和 Codex thread |
| 群聊 | 当前消息需提及机器人；按群 ID 共享工作区、上下文和 thread |
| 文本、富文本、语音 | 立即拉取并组装相关聊天历史；语音先转写为文本 |
| 图片、文件 | 默认进入 10 秒固定缓冲窗口，可与随后发来的任务说明一起处理 |
| 运行中补充要求 | 尝试通过 steer 加入当前任务；追加图片若要求切换模型，会提示等待或停止后重发 |
| 进度与结果 | 添加处理中 reaction、持续更新卡片，在结束或异常收尾时清理状态 |
| 会话持久化 | SQLite 保存 thread 绑定、历史索引和 token 用量；Codex 会话数据保留在其自身数据目录 |

| 命令 | 行为与限制 |
| --- | --- |
| `/stop` | 停止当前任务并取消缓冲；若退出尚未确认，该会话暂时禁止新任务，可稍后再次执行 |
| `/new` | 停止当前工作、归档旧 thread 并重置绑定；下一条消息新建会话，不删除工作区文件 |
| `/history` | **仅私聊**提供历史会话选择卡片，可激活所选会话；默认展示最近 30 条，不通过翻页访问更早记录 |
| 撤回消息 | 尝试移除缓冲消息或停止与该消息关联的任务；不撤销已经发生的文件修改和外部操作 |

命令应作为独立文本发送。默认命令名及消息缓冲行为见 [默认配置](codex_bridge/configs/config_default.json)；`/history` 为固定入口。撤回消息只处理 `message_owner` 事件，停止关联消息所属批次；撤回已接受的 steer 消息会停止整个原任务。群聊成员共享任务状态，不能通过群聊实现成员级数据隔离。

**私聊按 `on_…` 用户隔离目录；群聊按 `oc_…` 群 ID 共享工作区、聊天上下文和 Codex thread。** 同群成员的任务可能互相影响，群聊不能作为成员之间的数据隔离边界。

## 运行链路与代码职责

```text
飞书 WebSocket 事件
  → 有界事件分发、群聊提及检查与消息去重
  → 控制命令，或缓冲/历史组装
  → 下载并校验附件、音频转写
  → 准备用户工作区、恢复或创建 Codex thread
  → 启动任务或 steer，消费 SDK 事件流
  → 更新卡片、记录用量、清理 reaction 和运行状态
```

| 位置 | 职责 |
| --- | --- |
| [main.py](codex_bridge/main.py) | 服务入口、依赖组装、飞书事件注册、健康心跳与关闭处理 |
| `middleware/message_router.py`、`message_collector.py` | 消息过滤、缓冲、命令识别与历史批次收集 |
| `middleware/message_assemble.py`、`resource_validator.py`、`audio_transcription.py` | 输入转换、附件格式校验与 ASR |
| `middleware/gateway_execution.py`、`gateway_runtime.py`、`gateway_commands.py` | 卡片交付、任务索引、停止与重置、历史交互和收尾 |
| `codex/codex_execution.py`、`codex_runtime.py` | `Codex`、模型路由、SDK 生命周期、steer、重试和错误转换 |
| `codex/codex_workspace.py`、`thread_manager.py`、`thread_watchdog.py` | 工作区初始化、thread 绑定及超时控制 |
| `session/` | 会话缓存、恢复与历史索引 |
| `services/lark/` | 飞书 API、流式/交互卡片及受限并发请求 |
| `configs/` | 默认配置、Schema、校验、加载和首次初始化 |
| `utils/` | 日志、token 用量、事件分发、请求执行器和健康检查 |
| `tests/` | 离线回归测试 |

`create_gateway()` 组装共享同一 `SessionCache` 的 runtime、execution、commands 和 router。服务直接使用 `AsyncCodex()`，不会自动注册 MCP、安装插件或覆盖用户 Codex 配置。

### MCP 服务代码与共享关系

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

`configs/` 中的 `config_default.json`、`config_schema.json`、`loader.py`、`validation.py`、`initialization.py` 通过相对符号链接指向 `../../codex_bridge/configs/`。`services/lark/lark_requests.py` 同样引用 Codex 的对应文件；`lark_client.py` 保持独立，以便延迟检查工具凭据。

两个服务分别加载共享源码，配置对象与线程池互不共享。MCP 保留配置中的存储路径原值，不承担 Codex 的工作区初始化。MCP 的 Dockerfile 保持两个源码目录在 `/opt/` 下相邻，使符号链接在构建时可解析；单独复制本项目或改成开发机绝对链接会破坏构建。

已安装 wheel 和镜像使用构建时的内容。修改共享源码后须重新安装或构建，并验证两个服务；仅修改挂载配置则重启即可。

## 配置与凭据

### 共享配置与生命周期

默认读取 `~/.codex_bridge/config.json`，可通过 `CONFIG_FILE` 指定其他现有文件。字段定义见[默认配置](codex_bridge/configs/config_default.json)和 [Schema](codex_bridge/configs/config_schema.json)。两个服务启动时都完整校验同一份 JSON Schema，任何配置段不合法都可能阻止启动；校验通过后各自使用相关字段。校验采用 Draft 2020-12 与格式检查，拒绝非有限数值；直接/缓冲消息类型必须不相交，且并集等于支持类型；两个自定义命令不能相同或含空白；音频目标大小不能超过最大大小。Schema 约束错误报告文件路径、字段路径和约束名，不回显配置值。

应用只从 `~/.codex_bridge/.env` 加载 dotenv，已有进程环境变量优先；更改 JSON 路径不会改变 dotenv 的位置。JSON 中的凭据字段（如 `apiKeyEnv`）填写**环境变量名**，不填写密钥值。不要把真实密钥写入 JSON 或仓库。

- `CONFIG_FILE` 可指定业务 JSON 文件；它不改变应用 dotenv 的默认位置。容器首次启动只在默认配置缺失时原子创建文件，已有文件不覆盖；自定义路径必须预先存在。
- 两个服务都完整校验 Schema；MCP 不会因为暂时缺少某个工具的 API 凭据而拒绝启动，但任意配置段的 JSON/Schema 错误仍会阻止启动。
- 修改挂载的业务配置后需重启应用；修改源码或镜像依赖后需重建镜像并重建容器，仅重启旧容器不会更新代码。

### 网关凭据与环境变量

| 配置或环境变量 | 作用 |
| --- | --- |
| `LARK_APP_ID`、`LARK_APP_SECRET` | 默认飞书应用凭据，网关启动时必须提供 |
| `LARK_ROBOT_UNION_ID`、`LARK_ROBOT_NAME` | 至少一项非空，用于群聊提及识别；推荐使用 Union ID，名称也可参与匹配 |
| `DOUBAO_API_KEY` | 默认 ASR 与文生图凭据；由 `audio.asr.apiKeyEnv` / `mcp.imageModel.apiKeyEnv` 指定实际变量名，在调用时使用 |
| `DEEPSEEK_API_KEY` | 会话标题生成凭据；由顶层 `sessionTitleGen.apiKeyEnv` 指定实际变量名，在 turn 完成后生成标题时使用 |
| `codex.models` | 文本、图片和多模态输入的模型/provider 路由；对应认证和连接由 Codex 配置管理 |
| `codex.sandbox` | 默认 `workspace-write` |
| `storage` | 工作区、附件子目录与 SQLite 路径 |
| `messaging`、`resources` | 缓冲、历史、支持的消息类型与附件扩展名 |
| `codex.watchdog`、`lark.requestConcurrency` | 任务期限、请求超时及飞书请求并发限制 |

网关启动要求 `LARK_APP_ID`、`LARK_APP_SECRET`，且 `LARK_ROBOT_UNION_ID` 或 `LARK_ROBOT_NAME` 至少有一项非空；推荐使用 Union ID。ASR 在调用时读取 `audio.asr.apiKeyEnv` 指定的变量；会话标题在调用时读取 `sessionTitleGen.apiKeyEnv` 指定的变量；MCP 文生图在每次调用时校验模型参数及密钥，飞书客户端在调用文件工具时才初始化，缺少某类凭据只影响对应工具调用。模型是否可用还取决于提供方与账户权限。

Codex 主任务的模型及 provider 来自 `codex.models`，认证与 provider 连接配置由实际运行环境中的 Codex 管理。仓库不会自动注册 MCP 或覆盖 Codex 配置。

### MCP 配置

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

文生图在每次调用时校验模型参数及密钥；飞书客户端在调用文件工具时才初始化。缺少图像凭据只影响图像调用，缺少飞书凭据只影响文件调用。

### 默认数据位置

默认使用两个宿主机目录，容器中分别挂载到 `/home/app/.codex_bridge` 与 `/home/app/.codex`：

| 位置 | 用途 |
| --- | --- |
| `~/.codex_bridge/config.json` | 共享业务配置：消息、存储、模型路由、ASR、MCP、超时与日志 |
| `~/.codex_bridge/.env` | 飞书、ASR 和图像服务等应用环境变量；加载器只读取此处的 dotenv，已有进程环境变量优先 |
| `~/.codex_bridge/state.sqlite` | 默认 SQLite 数据库，保存 thread 绑定、会话索引和用量 |
| `~/.codex_bridge/logs/` | 默认业务日志目录 |
| `~/.codex/` | Codex 的配置、认证、插件、会话及用户工作区 |
| `~/.codex/workspace/<on_…或oc_…>/` | 工作目录，含 `AGENTS.md`、Git、`.venv`、Node 依赖及任务文件 |
| 仓库根目录 `.env` | **Compose 插值用的部署配置**，主要包含 Langfuse 及存储凭据；不是应用 dotenv |

工作区内部结构：

```text
~/.codex/workspace/<on_…或oc_…>/
├── AGENTS.md               # 缺失时创建空文件，已有内容保留
├── .git/                  # 已有 Git/worktree 则复用
├── .venv/                 # 当前用户/群的 Python 依赖
├── package.json、pnpm-lock.yaml、node_modules/
└── resources/inbound/     # 默认入站附件位置
```

Codex 自身的认证、配置和会话也保存在实际 Codex 数据目录。相对存储路径按业务 JSON 所在目录解析；容器中的 `~` 指容器用户 HOME。token 用量仅写 SQLite，旧 `storage.tokenUsagePath` 仅兼容保留、不再用于生成 CSV。

## 启动与连接

以下命令均从**仓库根目录**执行。两个服务要求 Python `>=3.13,<3.14`。首次任务的工作区初始化还需要 Git、uv、Node.js ≥22、pnpm 及包源网络访问；音频处理需要 ffmpeg/ffprobe，文档转换、OCR 等还依赖相应系统工具。工作区初始化使用 POSIX 文件锁和进程组，原生 Windows 不是当前运行目标。

### 源码运行

先配置真实飞书凭据和可用的 Codex 认证/provider，再执行：

```sh
uv sync --project codex_bridge --locked
uv sync --project mcp_service --locked

# 仅初始化缺失的默认业务配置，不生成应用凭据
codex_bridge/.venv/bin/python -B codex_bridge/configs/initialization.py

# 分别在两个终端运行
codex_bridge/.venv/bin/python -m codex_bridge.main
mcp_service/.venv/bin/python -m mcp_service.server
```

也可在 `codex_bridge/` 中执行 `.venv/bin/python main.py`，在 `mcp_service/` 中执行 `.venv/bin/python server.py`；安装为包后分别提供 `codex_bridge` 和 `mcp_service` 入口。本地入口不会自动初始化配置，需先完成上述初始化或提供自定义配置文件。按需在 `~/.codex_bridge/.env` 中提供真实业务凭据；启动和协议检查成功并不代表工具凭据已有效。

### Docker 与 Compose

推荐使用 Docker，Codex 镜像已包含 Python、Node.js、uv、pnpm、Git、ffmpeg/ffprobe、LibreOffice、Poppler（含 CMap 数据）、qpdf、Pandoc、Tesseract 中英日 OCR 和 Inter/Noto CJK 字体；不预装 Apple 专有字体或 GCC。用户级 Python/npm 文档依赖仍在首次任务时安装。

```sh
# 从仓库根目录执行；不覆盖既有文件
mkdir -p "$HOME/.codex" "$HOME/.codex_bridge"
docker network inspect ai-infra >/dev/null 2>&1 || docker network create ai-infra
```

在 `~/.codex_bridge/.env` 中配置自己的真实应用凭据，并按需要设置 `config.json`；可参考[默认值](codex_bridge/configs/config_default.json)和 [Schema](codex_bridge/configs/config_schema.json)。不要用占位密钥启动服务，也不要覆盖已有部署的凭据。

在飞书应用后台启用机器人和长连接事件接收，配置消息接收、消息撤回及卡片交互回调所需能力，并授予读取消息/资源、发送与更新卡片、上传文件和操作 reaction 等对应权限。事件注册以 [main.py](codex_bridge/main.py) 为准；本项目不自动创建应用、申请权限或发布应用版本。

同时准备实际容器使用的 Codex 认证与 provider 配置。宿主机的路径、`localhost` 地址及原生二进制插件不能直接假定在 Linux 容器内有效。挂载目录须允许容器用户写入。预构建镜像的用户 UID/GID 为 1000；宿主机 UID 不是 1000 时，在根 `.env` 中设置 `APP_UID` 为宿主机 UID，Compose 会以该 UID、镜像组 1000 运行容器，并将依赖缓存重定向到 `.codex_bridge` 挂载内。

```sh
# 首次调用会在根 .env 不存在时生成 Langfuse/存储凭据；已有文件不覆盖
./compose_initial.sh config --quiet
./compose_initial.sh build --no-cache

# 启动根 Compose 的两个应用与 Langfuse 全栈
./compose_initial.sh up -d
./compose_initial.sh ps
./compose_initial.sh logs --tail=100 codex_bridge mcp_service
```

包装器显式使用根 `.env` 和 `docker-compose.yaml`，可从其他目录调用；直接运行 `docker compose` 不会触发凭据初始化。已有 `.env` 不会补写缺项。必须提供的部署变量为 `NEXTAUTH_SECRET`、`SALT`、`ENCRYPTION_KEY`、`POSTGRES_PASSWORD`、`CLICKHOUSE_PASSWORD`、`MINIO_ROOT_PASSWORD`、`REDIS_AUTH`；`ENCRYPTION_KEY` 应为 64 位十六进制字符串。

已有部署丢失 `.env` 时应恢复原凭据，不能重新生成后连接旧数据卷。即使只启动 `codex_bridge mcp_service`，根 Compose 解析时也仍要求上述部署变量。脚本不会创建飞书/模型 API 凭据、外部网络或宿主机挂载目录。

`HOST_CODEX_DIR`、`HOST_DIR` 可改变挂载来源，必须通过 shell 或 Compose 的环境文件提供。Compose 不自动读取 `~/.codex_bridge/.env`；shell 中已导出的空值也可能覆盖 dotenv 中的有效值。

两个镜像始终从仓库根目录构建：

```sh
docker build -f codex_bridge/Dockerfile -t codex_bridge .
docker build -f mcp_service/Dockerfile -t mcp_service .
```

容器入口会原子创建缺失的默认业务配置，已有配置保留；默认挂载 `~/.codex_bridge` 和 `~/.codex` 到 `/home/app/` 下。MCP 单独构建仍可用 `APP_USER`、`APP_UID`、`APP_GID` 调整镜像用户。

### MCP 连接地址与就绪检查

| 调用位置 | 默认 MCP 地址 |
| --- | --- |
| 宿主机客户端 | `http://127.0.0.1:8000/mcp` |
| 同一 `ai-infra` 网络内的容器 | `http://mcp_service:8000/mcp` |

在实际使用的 Codex 配置中注册对应地址。容器内的 `127.0.0.1` 指向该容器自身；客户端必须使用自己能够访问的地址。服务不会替客户端注册连接。

根 Compose 的 `MCP_PORT` 同时控制宿主机映射和容器监听端口，`MCP_BIND_ADDRESS` 默认 `127.0.0.1`；`MCP_PATH` 可覆盖默认路径 `/mcp`。生产 Compose 则固定容器端口 8000，`host.json` 中的 `mcp_port` 只改变宿主机映射。不要混用两种端口约定，也不要将无鉴权服务直接暴露到公网。

```sh
./compose_initial.sh exec codex_bridge python -m codex_bridge.utils.health
./compose_initial.sh exec mcp_service python -m mcp_service.utils.health
```

Codex 探测主循环心跳及飞书连接，MCP 探测协议握手和工具注册；两者均不验证真实模型调用、文件上传或 Office 任务。启动后还需用真实对话验证目标环境的完整链路。根 Compose 未配置自动 healthcheck，生产 Compose 已配置；仅显示容器运行中不等于服务就绪。

## MCP 工具接口

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

飞书请求使用有界执行器，满额立即失败；调用方取消或超时后，已经开始的线程仍占用名额，直到真实请求结束。上传句柄由 worker 持有，避免超时后提前关闭；永久阻塞的线程无法由此机制强制终止。默认并发容量来自项目初始保护策略，未作为压测上限承诺。

### 文生图

`image_generator` 使用 `mcp.imageModel` 指定的 API、模型和密钥环境变量，通过 `AsyncOpenAI.images.generate` 请求图片。当前请求固定使用 `2K`、URL 响应，以及提供方特定的生成参数，返回第一张图片的 URL。

工具不下载图片、不将图片存入工作区，也不自动发送到飞书。它只接收文本提示词，不提供图片编辑或视频生成；存在 `mcp.videoModel` 配置不代表服务注册了视频工具。切换提供方前需核实其接口能接受当前参数。

## 工作区初始化

- **首次任务需要安装依赖。** 即使只进行文本对话，也会检查当前用户的 Office 环境。Python/npm 包清单定义在 `codex/codex_workspace.py`，首次解析最新兼容版本；成功后按环境状态复用，不会因上游发布新版而自动升级。
- **初始化有边界。** 默认总期限 600 秒，Git 初始化期限 10 秒，来自当前初始化策略与默认配置。同用户初始化串行，跨进程使用文件锁；安装失败、非受管环境、清单冲突或 Python 次版本不兼容时明确报错，不自动删除工作区或覆盖用户文件。
- **任务环境独立设置。** 通过 `shell_environment_policy.set` 为任务设置工作区 `VIRTUAL_ENV` 和 `PATH`，不修改服务全局环境。依赖和目录隔离不等于强多租户安全隔离。

首次处理每个用户或群的任务时，会准备 Git 和 Office 依赖环境，即使任务只是文本对话也会执行该检查。初始化需要 Git、uv、Node.js ≥22、pnpm 和包源网络访问，默认总期限 600 秒。已有完整环境可复用；现存非受管或不兼容环境会报错，不自动删除。缺失的 `AGENTS.md` 只创建空文件，使用规则需要自行配置。

## 会话持久化与历史恢复

### 活跃绑定与首次命名

[thread_manager.py](codex_bridge/codex/thread_manager.py) 使用 `aiosqlite` 在 `storage.databasePath` 下保存 `user_thread` 表：`user_id TEXT PRIMARY KEY NOT NULL`、`thread_id TEXT NULL`。`get_user_thread(user_id)` 在记录不存在或绑定已重置时返回 `None`；`set_user_thread(user_id, thread_id)` 采用参数化 SQL 和原子 upsert，传 `None` 持久化为 SQL NULL。每次操作独立打开并关闭连接，不降级为内存绑定。

WAL 与建表初始化遇到 `SQLITE_BUSY`（含扩展错误码）时，先关闭失败连接，再异步等待后重试；总预算沿用原来的 30 秒，50ms 退避与历史会话模块一致。初始化期间禁用 SQLite 内部锁等待，避免每轮各等待 30 秒；成功后恢复业务 SQL 的 30 秒锁等待。超过预算、非 BUSY 错误和取消直接传播。重试只发生在连接交给调用方之前，不重新执行绑定读写或提交，不修改表结构或迁移数据。

每次请求恢复或创建 thread，在启动 turn 前保存绑定；即使 turn 启动失败，下次仍可恢复该 thread。`/new` 先确认旧任务停止，再归档旧 thread 并清空绑定，失败时不报告成功；它不删除历史或工作区。旧版本仅在内存中的绑定不会自动迁移。

[session_history.py](codex_bridge/session/session_history.py) 在同一数据库中创建 `session_history` 表和排序索引，不替换活跃绑定表：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | TEXT NOT NULL | 私聊 Union ID 或群 chat ID，与 `thread_id` 组成主键 |
| `thread_id` | TEXT NOT NULL | Codex thread ID |
| `thread_name` | TEXT NULL | 大模型根据该 thread 最早 turn 的 userMessage/agentMessage 生成的会话标题；尚未生成时暂为空 |
| `updated_at` | INTEGER NULL | `thread_items` 中该 thread 最早 turn 的 `created_at_ms`（毫秒） |

名称与排序时间都来自 Codex 自动维护的 `~/.codex/thread_history_1.sqlite` 的 `thread_items` 表，不再读取 `session_index.jsonl`。新线程登记时 `thread_name` 固定写空；turn 完成后若仍为空，则按 `created_at_ms` 取该 thread 最早的一个 turn，仅提取其中 `item_type` 为 `userMessage` 与 `agentMessage` 的 `item_json` 文本，调用 `sessionTitleGen` 配置的模型生成标题并写入，标题一旦写入不再替换。`updated_at` 直接写入最早 turn 的 `created_at_ms` 毫秒值，只允许时间前进。

收到 `turn/completed`（包括成功、失败或中断）时同步最早 turn 的时间，并在后台生成标题；恢复历史只同步时间、不生成标题，此时名称应已存在。标题生成失败或无可提取内容时保持 `thread_name` 为空，等待下一次 turn 完成后重试，不影响本轮任务结果；缺少 `DEEPSEEK_API_KEY` 时同样跳过并留空。列表按 `updated_at` 降序、同秒按 `thread_id` 降序、空时间最后排序；断流或强制退出不保证同步到最新时间，同步失败不重发输入、不改写已经确定的模型结果。

### 历史选择与恢复接口

`/history` 使用 [lark_interactive_card.py](codex_bridge/services/lark/lark_interactive_card.py) 的 Card 2.0 表单：下拉选择本身不回调，点击 `Activate this session` 后由 `confirm` 确认提交；取消不停止任务、不解除归档、不写数据库。空名称显示 `Untitled session`。

`messaging.sessionHistoryLimit` 默认 30，是用户指定的展示策略，独立于聊天历史/附件上限 `historyPageSize`。网关先截取最近记录，卡片 `PAGE_SIZE` 使用同一上限，所以当前 `/history` 不产生多页；底层虽保留分页与 revision 检查，也不能据此访问截取范围之外的旧记录。卡片保存发送时的选项快照，需要最新列表时重新发送 `/history`。

服务端保存随机 ticket、操作者 Union ID、原私聊 chat ID、message ID 和选项。回调核对 `operator.union_id`、`context.open_chat_id`、`context.open_message_id`、ticket 与 revision，仅接受激活按钮及 `action.form_value.history_thread` 中的可见选项；不接受任意前端 user ID、`action.option` 或选择事件直接切换。恢复前再次查询数据库归属，每张卡的确认只消费一次，失败后须重新获取卡片。

历史卡片只保存在当前进程内，24 小时失效，容量 1024；满时淘汰最早的非处理中卡片，全部忙碌则拒绝创建。这些值是初始内存策略，不是平台限额。重启后旧卡失效，不支持多网关共享卡片状态。SDK 回调通过控制事件容量异步提交；处理中提示不代表恢复成功。结果卡交付失败不重放恢复操作，也不撤销已经提交的绑定。

供网关适配层复用的入口是异步 Python 方法，不是 HTTP API：

```python
from codex_bridge.middleware.gateway_commands import GatewayCommands

async def restore_selected_session(
    commands: GatewayCommands, data, selected_thread_id: str
):
    # commands 来自启动层共享实例；data 必须来自已验证的消息身份。
    options = await commands.history_options(data)
    result = await commands.processing_history_restore(data, selected_thread_id)
    return options, result
```

`history_options` 返回带 `label`、`value`、`updated_at` 的选项，查询失败直接抛错，不能伪装成空列表；恢复结果用 `ok` 表示成功与否，成功包含 `thread_id`。选择当前绑定直接成功，不停止或重复解除归档。切换其他 thread 时，先通过 reset 门禁、停止确认、退出等待及提交锁，再调用 SDK `thread_unarchive` 并完成客户端清理，最后同步排序时间并替换绑定。

恢复不额外归档此前的活跃 thread，不创建替代 thread，也不重新命名已完成命名的目标。未归档目标仍按约定调用 `thread_unarchive`，SDK 拒绝则失败。失败保留原绑定，但 SDK 状态可能已经变化，不能保证回滚远端操作。底层 `Codex.restore_session` 只供已完成停止与锁保护的调用方使用；并发保证基于单网关、按 chat ID 串行提交。

## 消息组装与附件处理

### 命令、缓冲与去重

`/new`、`/stop` 仅匹配原始 `text` 消息的完整文本，忽略大小写和首尾空白，不接受参数，不从富文本或 ASR 结果中识别。实际配置字段为 `messaging.newThreadCommand` 与 `messaging.stopThreadCommand`。群聊仍要求事件提及机器人；命令解析不会自动剥离 mention 占位符。私聊 `/history` 优先匹配且是固定命令，其他命令不应配置成同名。

历史批次以触发消息为上界，遇到机器人消息、`/new`、`/stop` 或私聊 `/history` 停止向前收集。不支持的消息类型独立提示并清理 reaction，不取消已有附件缓冲。重置期间新输入等待，停止/重置前的缓冲和待处理输入通过会话 generation 失效。

每个会话的已接收/已处理 ID 集合各保留最多 `messaging.recallCacheMaxEntries` 条，默认 1024，最长 24 小时；重复事件不续期。历史重叠和已接受的 steer 消息不会重复提交，但去重不跨进程持久化，超过容量或期限后不保证去重。失败旧消息不会被后续历史自动重试，需要重新发送。

### 富文本附件与格式验证

富文本按标题、正文文字/图片、顶层 `files` 数组的顺序组装；附件记录使用 `file_key`、`file_name`、`is_folder`，使用所属消息 ID 下载，以 `MentionInput` 提交文件。文件名缺失时显示名使用消息 ID 和序号，实际落盘名由下载响应与格式校验决定。`is_folder=true` 明确拒绝，不递归下载；异常数组、条目、布尔类型或资源 key 都产生拒绝说明。不因缺少 `files` 强制把图片富文本改成 text。

优先解析 `content_v2`，否则使用 `content`，两份兼容字段不重复累计。`historyPageSize` 默认 10，同时限制每次历史请求条数和每批附件总数；图片、文件、音频以及解析拒绝项都计数。超限在下载/ASR/模型提交之前整批拒绝。部分附件成功时保留正文和有效附件，全部不可用时连同任务正文一起不提交。

[resource_validator.py](codex_bridge/middleware/resource_validator.py) 联合检查扩展名、MIME 和内容：

- 文本/代码先检查 UTF-8（可含 BOM）和二进制控制字符，通过后保留原扩展名；不猜测编码、不转码、不执行代码或检查语言语法。JSON 必须能够解析，不能用 `text/plain` 绕过；JSONL 按文本处理。
- DOCX/XLSX/PPTX 检查 ZIP 目录、`[Content_Types].xml` 和 `_rels/.rels`，要求唯一内部主文档关系、非空主部件和匹配的内容类型；支持安全的自定义主部件路径，检查时不解压到工作区。
- 每份 Office XML 元数据最多读取 1 MiB，支持 UTF-8 或带 BOM 的 UTF-16，拒绝实体声明、重复部件及不安全路径。这是项目预算，不是格式上限；不遍历全部正文部件、不提供杀毒或宏扫描，也不保证文档可被所有应用打开。
- 图片、音频和 PDF 按实际实现进行签名/元数据校验，仍须满足扩展名白名单。格式拒绝会保留具体原因，不伪装为网络下载失败。

### 音频转换与文件清理

`audio.limits.conversionTimeoutSeconds` 默认 540 秒，覆盖单个音频从 ffprobe 检测到全部 ffmpeg 转码/切片完成，不含 ASR 网络时间，切片不重置计时。超时或取消先终止子进程，2 秒未退出则强制回收并清理临时目录；现有任务总期限可能更早触发。转换失败通过附件通知反馈，不把错误文案作为转写结果提交。

进入 `ASR.transfer()` 的原始 `.ogg` 文件（忽略大小写）在成功、失败、空文本、配置缺失、超时或取消后都会尝试删除，包括以普通文件上传的 OGG；其他格式的原文件保留，临时 M4A/切片随临时目录清理。OGG 删除失败只记日志，不覆盖原结果。停止/撤回沿用中断流程，不额外发送转写失败通知。

## 流式卡片与样式定制

### 发送接口与交付语义

[lark_message_card.py](codex_bridge/services/lark/lark_message_card.py) 的 `sending_card(union_id, content, session=...)` 接受私聊 Union ID 或 `oc_` 群 ID；字符串发送静态通知，异步迭代器中的字符串追加正文，`CardReplace` 替换当前正文与缓冲，`CardSteer` 用作内部写入屏障。`CardStreamSession` 可共享单个任务的卡片、控制和停止状态。成功返回最后一张卡片的消息 ID，空内容返回 `None`。

```python
from codex_bridge.services.lark.lark_message_card import CardReplace, sending_card

async def send_example(recipient_id: str):
    async def content_stream():
        yield "开始处理"
        yield "\n正在整理结果"
        yield CardReplace("**处理完成**")

    return await sending_card(recipient_id, content_stream())
```

流式发送先通过 CardKit 创建卡片，再发送 interactive 引用，按递增序号提交累计正文，最后关闭 `streaming_mode`。首段立即发送，其后按 250ms 合并；独立计时器会刷新静默期间的尾部，正常结束强制刷新，停止/撤回丢弃未发送缓冲。Markdown 正文不按长度自动截断或分卡，完整 Markdown 图片语法在写入时降级成裸地址。

每张卡从创建请求起计算 540 秒生命周期，到期刷新并关闭，后续有文本才创建续卡；新卡只承接后续文本，不重复旧卡全文。明确收到 `300309` 拒绝时，续卡承接未发送后缀，正文替换则承接完整新正文。其他交付错误不盲目重发：停止卡片输出、继续消费模型流，最后抛出 `CardDeliveryError` 并记录 `delivery_failed`，不直接中断模型或把已完成的模型任务改判失败，原任务期限仍生效。

输出规则以当前事件转换和网关代码为准：

| 事件 | 卡片行为 |
| --- | --- |
| reasoning、summary delta、commentary | 最终答案开始前持续展示；不在 item 完成时重复追加推理全文 |
| 工具开始/完成 | 只显示工具名称及成功、失败或状态未知；不输出命令、参数、目录、输出、diff 或详细错误 |
| 最终答案 | 首段替换当前正文与缓冲，后续累积；已关闭旧卡保持原内容 |
| usage、hook、plan、工具输出及未知事件 | 不通过通用 JSON 兜底进入卡片；usage 仅写数据库 |
| 任务级错误 | 尚无答案时替换正文，已有答案时追加错误；命令通知、附件通知另按通知处理 |

工具命名：命令为 `commandExecution`，MCP 为 `server.tool`，Dynamic 为 `namespace.tool` 或 tool，其余已识别工具按实际名称/类型显示。明确失败、拒绝、中断、非零退出码、`success=False` 或错误标志优先判失败；明确完成、零退出码或 `success=True` 才表示协议成功；缺少可靠结果则标记状态未知，无完成事件不补造结果。协议成功不等于业务结果正确。最终答案开始后不再追加推理、commentary 和工具状态。

### Card 2.0 样式

当前样式由 `_card`、`_card_config`、`_card_body` 构造，历史选择卡片另由 `lark_interactive_card.py` 构造；没有独立的主题配置加载器。普通卡片标题固定为 `Codex`，蓝色模板、`lark-logo_colorful` 图标，正文元素 ID 为 `response_content`；当前宽度为 `fill`，并非旧资料描述的默认宽度。

下面是按当前构造函数简化的结构示例，省略分隔线和提示元素：

```json
{
  "schema": "2.0",
  "config": {
    "update_multi": true,
    "width_mode": "fill",
    "streaming_mode": false,
    "summary": {"content": "处理完成"}
  },
  "header": {
    "title": {"tag": "plain_text", "content": "Codex"},
    "template": "blue",
    "icon": {"tag": "standard_icon", "token": "lark-logo_colorful"}
  },
  "body": {
    "direction": "vertical",
    "padding": "12px 12px 12px 12px",
    "elements": [
      {"tag": "markdown", "content": "处理完成", "element_id": "response_content"}
    ]
  }
}
```

`header` 位于卡片根部，不放在 `body.elements` 中。流式摘要为等待提示，静态摘要取正文前 100 字符；样式中分别定义 desktop/mobile 字号。修改模板时须保留正文元素 ID 与更新请求的一致性、请求序号及流式开关逻辑；消息发送和 CardKit 创建/更新都需要对应飞书权限。

原样式资料还列出如下扩展字段，当前生成器并未全部使用。本节保留其定制方向，不把旧资料中的平台限额当作本轮验证结论：

| 范围 | 可供定制时核对的字段 |
| --- | --- |
| 标题 | `title`、`subtitle`、`template`、`text_tag_list` / `i18n_text_tag_list`、`icon`、`padding`；标签使用 `plain_text`，图标分标准 token 与自定义 `img_key` |
| 卡片配置 | `streaming_config`、`summary.i18n_content`、`locales`、`enable_forward`、`update_multi`、`width_mode`、`use_custom_translation`、`enable_forward_interaction` |
| 自定义样式 | `style.text_size` 的 default/pc/mobile；`style.color` 的 light_mode/dark_mode RGBA |
| 整卡链接 | `card_link` 的 url/android_url/ios_url/pc_url |
| 正文布局 | direction、padding、horizontal/vertical spacing 和 align，组件 margin 与唯一 element_id |

旧资料中的颜色方向为 blue=信息、green=完成、orange=警告、red=错误、grey=归档；它们不是当前按任务状态自动切换的行为。其他主题颜色、标签数量、行数、padding 和 element ID 的平台限制未在本轮外部核验，新增样式需在飞书客户端联调；原资料引用但仓库不存在的颜色/样式文件不作为依赖。

## 任务控制与资源回收

### 停止与 steer

`Codex.interrupt_and_confirm(run_id)` 先登记中断意图，防止启动中继续提交；已有 turn 时发送 interrupt 并查询同一客户端，确认 thread idle。宽限期内无法确认时，尝试关闭 SDK 客户端并确认所属 app-server 进程退出。停止未确认则阻止同会话新任务，可再次 `/stop`；`/stop` 保留 thread 绑定，`/new` 才继续归档和重置。

任务启动的 `started` 事件之后释放提交锁，由原请求消费唯一回复流。steer 复用原 `AsyncThread` 与 `AsyncTurnHandle`，不额外创建 app-server；调用前读实际 thread 状态，active 才提交，idle 等原流收尾后走新 turn。明确的 turn 不匹配/无 active turn 拒绝，也只有重新确认 idle 后才回退；其他失败和未知状态不自动重发。状态查询、steer 与客户端关闭互斥。

发送层先通过控制屏障暂停卡片写入，只有明确接受 steer 才关闭旧卡、丢弃未发送正文并立即创建新卡。失败或 idle 则恢复旧卡；新卡先显示 `messages.steerAccepted`，后续正文替换占位，无后续正文则以 `messages.steerCompleted` 结束。最多预取一个事件，不取消上游读取；停止/撤回优先。换卡失败不重放已接受输入，也不主动中断模型。

steer 消息归属原 run，与原任务统一处理撤回、reaction 及用量分组，不延长绝对任务期限。不能切换模型/provider；含图片输入要求与当前 image 路由一致，文本、文件和转写沿用当前模型。

### Watchdog 与收尾期限

[thread_watchdog.py](codex_bridge/codex/thread_watchdog.py) 按 run ID 记录接收时间、阶段、thread/turn ID、消息归属、最后活动、工具执行、终态和交付结果。以下秒数来自当前默认配置，是项目运行策略而非外部平台保证：

| `codex.watchdog` 字段 | 默认值 | 口径 |
| --- | ---: | --- |
| `maxRunSeconds` | 900 | 从接收起计时，含缓冲、排队、准备、启动和执行；必须为正整数 |
| `startupTimeoutSeconds` | 120 | Codex 启动阶段期限 |
| `idleTimeoutSeconds` | 0 | 0 关闭；启用时检测无事件时长，工具执行期间暂停 |
| `checkIntervalSeconds` | 1 | 独立 watchdog 和维护检查间隔 |
| `interruptGraceSeconds` | 10 | interrupt 与状态确认宽限 |
| `cleanupTimeoutSeconds` | 5 | 单阶段资源清理预算 |
| `finalizationTimeoutSeconds` | 30 | 模型终态或停止请求后的独立收尾期限 |
| `cardRequestTimeoutSeconds` | 10 | 单次飞书请求期限 |

工具执行不暂停总期限，steer 不续期；超时前会核对服务端终态，避免仅因卡片延迟误判。只有 `turn/completed` 或已核对的服务端终态能确定完成，异常 EOF 不算成功。模型完成后 watchdog 仍监督收尾；超过收尾期限则取消输出，再用清理预算强制关闭、释放索引并记录 `cleanup_timeout` / `released`。已完成的模型结果不会因交付或清理失败改判为模型失败。

停止确认在独立任务中执行，残留 worker 不配合取消或进程退出未确认时，会话保持阻塞。索引释放、缓存过期不等于协程/进程已经停止；事件循环或磁盘调用永久阻塞时不保证准点超时。SDK 进程确认依赖内部进程引用，升级 SDK 后须重跑关闭/中断测试；不能据此宣称撤销远端操作或终止脱离父进程的后台任务。

### Reaction、请求容量与内存状态

每条消息成功添加的 reaction 以会话/消息 ID 保存，统一在任务输出和关闭收尾后删除，包含聚合附件与接受的 steer 消息；首卡、中途更新或换卡不提前清除。添加失败只记日志，任务可继续。删除快照按 `(chat_id, message_id, reaction_id)` 放入独立队列，成功才移除，失败/取消保留重试，不阻碍普通会话缓存释放。

reaction 删除每轮维护至多重试一条，退避为 5、30、120 秒，之后每 600 秒；容量复用 `recallCacheMaxEntries`，超容淘汰最早记录，24 小时过期均记错误。这些是初始策略；不根据接口错误文案猜测“已删除”。队列不持久化，重启不能补删之前的遗留表情。

普通入站事件上限默认 32，控制事件另有 4 个名额，繁忙提示最多一个在途；模型批次还计入活动与残留任务容量。每进程飞书请求最多 8 个实际在途请求，满额立即失败，调用方超时后线程仍占容量，直到实际结束。上述值源于内部小团队初始保护策略，不是压测结论；结果不确定的发送不自动重试，所有事件 Future 的异常均消费并记录。

任务终结释放活动索引、时间戳、卡片和引用；会话没有待处理输入、缓冲、重置和运行时回收锁及 generation，等待中的新消息仍持有会话。轻量消息 ID 与停止未确认的元数据最长保留 24 小时，任务也受 24 小时硬上限约束。过期时尝试有界关闭，即使关闭失败也清理索引并告警；数据库、工作区和已写日志保留。

`/new` 和历史恢复使用唯一 `control-` SDK 会话登记客户端、初始化任务和进程，不新增模型 turn 或用量。正常关闭失败可强制关闭，只有确认清理后才继续绑定更新；重复取消不允许越过取消写库。未完成清理进入维护集合，每轮至多一个，失败后 30 秒重试，只清理不重放归档/解除归档，最长保留 24 小时。SDK 操作已经成功而清理/写库失败时，绑定不变不代表远端状态回滚。

## 日志与 Token 用量

### 运行日志

业务终端日志由 `logging.logLevel` 控制，当前默认 **DEBUG**；飞书 SDK HTTP/WebSocket 日志也保持 DEBUG，仅针对连接 URL 的 `access_key`、`ticket`、`access_token` 做遮蔽，并非任意日志的完整脱敏。业务异常保留堆栈，应限制日志访问。

生命周期和 `item/completed` 白名单摘要仅写入终端日志，不记录 delta 或 item 中间事件。摘要保留类型、ID、状态、耗时和退出码；图片可含保存路径/透明标记，Agent 消息含 phase/字符数；不展开命令输出、Base64、图片提示词、消息正文、工具结果或 turn.items。错误可保留 message、additionalDetails、willRetry，摘要字符串截取前 500 字符；所有事件仍更新内存活动时间和计数，`lastEvent` 仅表示最近可记录事件。

原先 watchdog 写入 `YYYY-MM-DD_logs.jsonl` 的运行日志已移除，Codex 自身的 rollout 会话文件已覆盖该用途；配置中的 `storage.runLogPath` 也已一并移除。

### Token 统计口径

[utils/token_usage.py](codex_bridge/utils/token_usage.py) 的 `SavingLog` 提交 SQLite 明细，`finalize_usage` 只回填已知任务耗时。每个 `thread/tokenUsage/updated` 将 `token_usage.last` 的六个增量字段写一行并提交，不保存或再次累计 `token_usage.total`：`cache_write_input_tokens`、`cached_input_tokens`、`input_tokens`、`output_tokens`、`reasoning_output_tokens`、`total_tokens`。

同任务的记录共享网关 run ID；直接调用执行流而不提供 run ID 时生成独立 UUID。`SavingLog` 本身只补默认字段，不生成 UUID。旧表首次写入时补充 `runId` 列，旧行保留空值，不据此重建此前漏记用量。`timeStamp` 为收到用量时的本地时间，`model` 取任务配置。

`taskDuration_ms` 初始为 0，得到 SDK 终态耗时后回填该任务全部明细；断流或取消后仍为 0 只表示未知，不能解释为实际零耗时。任务耗时用 `MAX`，不能对各事件行 `SUM`；缓存和推理字段是细分指标，不再次加到 total 中。默认表名下可使用：

```sql
SELECT runId, userId, threadId,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(total_tokens) AS total_tokens,
       MAX(taskDuration_ms) AS taskDuration_ms
FROM token_usage
WHERE runId <> ''
GROUP BY runId, userId, threadId;
```

自定义 `logging.tokenUsageTable` 时同步替换表名。任务退出不重复插入末条用量，没有用量事件就不补零行；每事件写一次，但没有上游重复事件去重。写入失败记录异常，不自动重试不确定的提交，以免重复计数。正常取消会保护正在进行的单次写入，强杀或存储故障不保证未提交数据持久化。当前只存 SQLite，不导出 CSV；历史 CSV 保留，`storage.tokenUsagePath` 仅兼容旧配置。

## Langfuse 与观测

根 Compose 启动 Langfuse Web、Worker、PostgreSQL、ClickHouse、MinIO 和 Redis，仅发布 Web 到 `127.0.0.1:3000`。`NEXTAUTH_URL` 默认 `http://localhost:3000`，应与实际浏览器访问地址一致。数据库、Redis 和 S3 不发布宿主机端口。

事件存储默认走内部 `http://minio:9000` 的 `langfuse` 桶；自定义桶须预先存在。当前未配置可选的 S3 媒体上传，浏览器直传或读取 MinIO 媒体附件不在这套编排的默认能力内。启动 Langfuse 不会自动启用 Codex tracing，还需在实际 Codex 环境配置对应插件、地址和凭据。

默认数据库连接串由 PostgreSQL 环境变量构造。凭据含 URI 特殊字符时，通过 `DATABASE_URL` 提供正确编码的完整连接串；修改环境变量不会自动修改已有数据库账户。内置 MinIO 的事件存储统一使用 `MINIO_ROOT_USER` 与 `MINIO_ROOT_PASSWORD`。

已有观测服务时，应先核对端口、容器名和数据卷，避免重复启动。基础设施使用滚动镜像标签，升级前需独立评估数据兼容性并备份。

## CI/CD 与生产发布

| Workflow | 触发条件 | 实际工作 |
| --- | --- | --- |
| [CI](.github/workflows/ci.yml) | 面向 `main` 的 PR、手动、可复用调用 | 在原生 ARM64 runner 升级解析 Python 依赖，构建两个镜像，在容器运行测试，并验证部署逻辑 |
| [Release](.github/workflows/release.yml) | 推送 `main` 或 `vMAJOR.MINOR.PATCH` tag、手动、每周一 02:23 UTC | 调用 CI；将同一批通过测试的镜像发布到 GHCR，不重新构建；生成 `release` artifact |
| [Deploy](.github/workflows/deploy.yml) | 仅 `main` 手动触发 | 验证指定的成功 Release，下载对应部署包，通过 SSH 更新生产应用 |

生产目标为 Apple Silicon 对应的 **Linux ARM64 Docker**；流水线没有构建 amd64 镜像。镜像使用 `ghcr.io/<owner>/<repo>/codex_bridge` 和 `mcp_service`，部署按 `release.json` 中的 digest 和 revision 校验。源码中的 workflow 不代表目标仓库和生产主机已经配置或运行成功。

推送 `v1.0.0` 这样的 Git tag，会为两个镜像增加 `1.0.0` 标签，并将同一镜像推送为 `latest`，同时保留 `sha-…-run-…` 追溯标签。版本号由发布者指定，不自动递增；仅支持无前导零的三段数字正式版本。普通 `main` 推送、手动和定时发布不更新 `latest`。`latest` 指向各镜像最近成功推送的正式发布，不按版本号大小排序；重跑旧版本或并行发布也可能改变其指向，精确部署应使用版本号或 digest。Deploy 仍只接受来自 `main` 的 Release run，不能填入 tag 触发的 run ID。

先提交并推送 workflow 修改，再在包含该修改的目标提交上执行 `git tag v0.1.2` 和 `git push origin v0.1.2`（版本号为示例，请使用尚未发布的实际版本）。已有 Git tag 的 workflow 不会随 `main` 更新；新的发布成功后，Compose 即可使用两个镜像的 `:latest`。私有镜像须先登录 GHCR，再运行 `docker compose pull codex_bridge mcp_service` 和 `docker compose up -d --no-build codex_bridge mcp_service` 更新应用。

使用前在 GitHub `production` Environment 配置 Secrets：`DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`、`DEPLOY_KNOWN_HOSTS`，以及 Variable `DEPLOY_ROOT`。目标主机需准备 `host.json`、应用配置、挂载目录、网络、GHCR 拉取授权，以及 Docker Compose 和 Python 3.9+。SSH 使用端口 22 并严格核验主机公钥。

生产 Compose 只管理两个应用，容器用户固定为 `app`，MCP 容器端口固定为 8000，`host.json` 的 `mcp_port` 仅改变宿主机回环端口。首次切换前需停止旧项目的两个应用，保留基础设施与数据；不能让两个网关同时消费消息。

部署脚本先预检并拉取镜像，再停止旧应用、备份 `config.json` 与 SQLite、启动并等待就绪，成功后写入 `current.json`。新版本切换失败时，只有存在旧发布且明确确认 `rollback_compatible` 才回退旧镜像；**不自动恢复数据库，不备份整个工作区，也不提供零停机或自动配置/数据迁移**。应用自身仍会按实现初始化表或补充兼容字段，镜像回退前须确认实际数据兼容性。

部署事务的实现见 [deploy.sh](deploy.sh)，其预检、备份、失败与回退分支由 [test_deploy.py](test_deploy.py) 离线验证；首次切换所需的 `host.json` 结构、必填变量与预检步骤以该脚本及 [Deploy workflow](.github/workflows/deploy.yml) 为准。源码中的 workflow 与测试不代表目标仓库和生产主机已配置或运行成功。

## 测试、健康检查与维护

```sh
# 在仓库根目录、完成 uv sync 后运行；使用临时配置及模拟外部客户端
codex_bridge/.venv/bin/python -B codex_bridge/tests/run_tests.py
# 可按文件筛选
codex_bridge/.venv/bin/python -B codex_bridge/tests/run_tests.py --pattern test_workspace.py
mcp_service/.venv/bin/python -B mcp_service/tests/test_runtime.py
codex_bridge/.venv/bin/python -B test_deploy.py -v

git diff --check
```

测试覆盖消息/附件处理、会话与停止控制、并发及超时、工作区初始化、用量、MCP 和部署失败/回退逻辑。测试使用临时配置和模拟外部客户端，MCP 另含本地 HTTP 握手验证；不能替代真实飞书、模型、GHCR、SSH 和生产切换验收。Codex 测试入口支持 `--reverse` 反向执行以检查模块缓存和全局状态；测试范围与运行约定见 [测试说明](codex_bridge/tests/README.md)。

```sh
# 对已运行的根 Compose 容器进行检查
./compose_initial.sh exec codex_bridge python -m codex_bridge.utils.health
./compose_initial.sh exec mcp_service python -m mcp_service.utils.health
```

Codex 健康检查验证进程、主循环心跳及飞书 WebSocket 连接；不调用模型，也不验证附件上传和 Office 输出。MCP 健康检查在 8 秒内完成本地 MCP 握手和工具列表读取，要求两个工具都已注册；不执行工具，不消耗模型额度，也不发送飞书消息。该超时来自容器探测预算，不是工具调用 SLA。根 Compose 未自动配置 healthcheck，生产 Compose 已配置。SIGTERM 会关闭入站并取消缓冲、进入任务中断收尾，开发与生产 Compose 的退出宽限分别为 30 秒和 60 秒，不能保证所有在途外部请求正常完成。

CI 在 Linux ARM64 镜像内运行回归。默认 Docker 构建使用 `uv sync --upgrade`；需要重新获取基础镜像及依赖时使用 `--pull --no-cache`。CI 中面向 `main` 的手动或可复用调用会执行 `uv lock --upgrade --python 3.13`，PR 则用 `uv lock --locked` 校验现有锁文件；随后传入 `--build-arg UV_SYNC_FLAGS=--locked` 构建与测试，使两阶段使用同一份依赖快照。CI 锁文件作为 artifact 保存，不自动提交回仓库；Release 直接发布测试过的镜像。生产由根目录 [deploy.sh](deploy.sh) 使用发布包部署，测试入口为 [test_deploy.py](test_deploy.py)，不在生产主机重新构建。

Python 基础镜像跟随 3.13 补丁，Node 跟随 LTS，uv 与 pnpm 跟随滚动版本；用户工作区首次安装时也解析最新兼容依赖，已有完整环境不会自动追新。镜像 digest 可追溯产物，不代表构建可逐字节复现。包版本由 `setuptools-scm` 推导，无 Git/分发元数据时回退为 `0.0.0`，不作为生产发布依据。

根 [.dockerignore](.dockerignore) 使用源码白名单并排除测试、虚拟环境、凭据和缓存；新增 Codex 源码目录或资源时须同步检查构建输入。共享模块变化需验证两个服务，并重新安装或构建才能更新已有 wheel/镜像。

## 能力边界与注意事项

- **安全边界：** MCP 当前无鉴权，文件工具从解析后的绝对路径中提取唯一接收方目录；目录名须匹配 `(?:on_|oc_)[A-Za-z0-9]+` 且总长为 35。它没有额外校验调用者身份或文件是否属于指定工作区，因此目录规则不等于授权控制。MCP 的文件读取权限与挂载范围决定可暴露的数据，回环绑定也不能替代客户端授权控制。保持本机绑定，仅向受信任的客户端和网络开放。
- **凭据：** 密钥通过环境变量或受控 Secret 配置，不提交真实值；业务配置、日志和工作区在分享前应检查敏感信息。默认 DEBUG 日志可能包含提示词片段、生成结果 URL 和运行信息，应限制访问并安排保留策略。
- **沙箱：** 默认 `workspace-write` 需要运行器允许非特权 user namespace，且 seccomp、AppArmor/SELinux 不阻止 Bubblewrap。当前 Compose 为 Codex 设置 `seccomp=unconfined`，会放宽容器限制；这不是独立多租户隔离方案。应在目标运行器验证沙箱，不自动修改宿主机内核策略。
- **附件：** 默认每批最多 10 个附件，与 `messaging.historyPageSize` 共用参数。支持范围由 `resources.acceptedExtensions` 和实际格式校验共同决定；不因扩展名合法就接受内容。旧 `.doc/.xls/.ppt`、通用压缩包和视频不在默认输入白名单中。部分附件失败会明确提示并继续处理可用内容；附件全部不可用时，不单独提交伴随文本。
- **音频与图像：** 音频先经 ffmpeg/ffprobe 处理，再调用外部 ASR；不提供本地离线识别。图像工具只接收提示词并返回首张图片 URL，不自动下载或发送文件；`mcp.videoModel` 配置存在不代表视频工具已实现。图像请求使用提供方特定参数，不能保证任意兼容接口都接受。
- **时间与容量：** 默认任务总期限 900 秒、启动期限 120 秒、空闲超时关闭；普通入站事件上限 32，每进程飞书请求并发上限 8。这些来自当前默认配置及小团队初始保护策略，不是吞吐或 SLA 承诺。超时、取消不能强制终止已经阻塞的同步请求线程。
- **停止与恢复：** 开发全栈 Compose 的退出宽限为 30 秒，生产为 60 秒；进程结束可能中断外部请求或卡片收尾。没有持久化任务队列，重启不保证恢复在途工作。停止或回退镜像不能撤销已发送消息和其他外部副作用。
- **外部能力需要另行配置：** 文档处理质量取决于输入、字体、模型与工具；安装系统依赖不等于插件或 MCP 已可用。业务配置更新需重启；源码或依赖更新需重建镜像与容器。
- **数据维护：** 工作区、附件、日志、会话和发布备份需要运维安排容量、访问控制及备份策略。token 用量仅写 SQLite，不再生成 CSV；旧 `storage.tokenUsagePath` 仅兼容保留。备份正在使用 WAL 的 SQLite 时，不应只复制主数据库文件。

## 历史维护记录

以下合并自 2026-09-09 清理记录，仅保留变更背景，**未在本轮重做当时的构建和测试，不作为当前部署步骤**。

- 当时 Codex 顶层依赖由 50 项减至 7 项、锁文件包数 53→35；MCP 移除 `cli` extra、锁文件包数 53→45，并显式声明 `jsonschema`。这些是当时计数，当前依赖以 `pyproject.toml` 和 `uv.lock` 为准。
- 当时采用各服务独立校验、各子目录独立 Docker 构建及任务结束后导出 CSV。后续已改为共享源码、完整 Schema 校验、仓库根构建上下文和仅 SQLite 用量记录；不要沿用旧命令或 CSV 调用约定。
- 当时移除 `storage.logdatabasePath`、`codex.models.file`、`lark.upload.imageType`，分别使用当前 `storage.databasePath`、`codex.models.multimodal` 和现有上传规则；后续机器人身份字段使用 `robotUnionIdEnv`，旧 `robotOpenIdEnv` 不受当前 Schema 支持。业务配置不自动整体迁移，旧字段需先对照 Schema 检查；`mcp.videoModel` 的保留不代表已实现视频工具。
- 清理同时修复同步 `cli()` 入口、保留包级惰性导出，移除重复路由、无效赋值和未使用导入；公共 `Codex.interrupt()` 与 cmd 兼容行为保留。MCP 文件发送的绝对路径、接收方校验和明确错误语义延续，完整边界见 [MCP 工具接口](#mcp-工具接口)。
- 当时记录 Codex 正向/反向各 248 项、MCP 10 项测试通过，以及锁文件、Compose、差异、独立构建和禁网只读容器检查；测试镜像标签为 `independent-check-20260909`。原记录说明未修改宿主运行目录、未替换服务，未调用真实飞书或模型。这些不是当前版本的验证结果。

原线程资料曾记录适配 `openai-codex 0.147.0`，这是历史观测值，不是当前固定版本。当前升级依赖后，仍需验证 SDK 状态查询、进程退出、卡片交付和用量事件口径。

## 许可证

本仓库使用 [MIT License](LICENSE)。模型服务、飞书、插件及其他第三方组件遵循各自的许可与服务条款。

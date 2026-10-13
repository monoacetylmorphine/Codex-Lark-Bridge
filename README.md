# Codex-Lark Bridge

Codex-Lark Bridge 是通过飞书使用 Codex 的自托管助手：将私聊或群聊消息转换为 Codex 任务，在持久化工作区中处理代码、图片和文档，并通过飞书卡片持续返回进度与结果。适合受信任的小团队在自己的运行环境中使用。

仓库包含两个 Python 服务：**`codex_bridge` 是飞书消息网关与任务运行器，`mcp_service` 提供文件发送和文生图工具**。根 Compose 另带 Langfuse 及其存储组件；生产发布使用独立 Compose，只更新两个应用服务。

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

## 如何在飞书中使用

1. 私聊机器人发送任务；群聊中先提及机器人。图片和文件默认进入 **10 秒固定缓冲窗口**，窗口内可继续发送附件；文本、富文本或语音到达时会立即触发历史组装。
2. 用自然语言说明要处理的内容和期望输出，例如“总结这份 PDF”或“分析表格并生成报告”。附件会保存到当前工作区的 `resources/inbound/`。
3. 运行中补充要求会尝试加入当前任务。若追加图片需要切换模型，当前任务无法直接切换；等待完成，或停止后将图片和任务说明一起重发。
4. 查看流式卡片；需要接收产物文件时，Codex 必须能调用已配置的 `sending_file`。仅生成文件或给出服务器路径，不代表文件已发送到飞书。

| 操作 | 行为与限制 |
| --- | --- |
| `/stop` | 停止当前任务并取消缓冲；若退出尚未确认，该会话暂时禁止新任务，可稍后再次执行 |
| `/new` | 停止当前工作、归档旧 thread 并重置绑定；下一条消息新建会话，不删除工作区文件 |
| `/history` | **仅私聊**提供历史会话选择卡片，可激活所选会话；默认展示最近 30 条，不通过翻页访问更早记录 |
| 撤回消息 | 尝试移除缓冲消息或停止与该消息关联的任务；不撤销已经发生的文件修改和外部操作 |

命令应作为独立文本发送。默认命令名及消息缓冲行为见 [默认配置](codex_bridge/configs/config_default.json)；`/history` 为固定入口。

**私聊按 `on_…` 用户隔离目录；群聊按 `oc_…` 群 ID 共享工作区、聊天上下文和 Codex thread。** 同群成员的任务可能互相影响，群聊不能作为成员之间的数据隔离边界。

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
│   ├── tests/                 # 回归测试；专题说明统一在项目 README
│   └── Dockerfile、pyproject.toml、uv.lock
├── mcp_service/                   # 独立 MCP 服务及两个工具
├── .github/workflows/          # CI、Release、Deploy
├── deploy.sh                   # 生产预检、备份、更新和失败处理
├── test_deploy.py              # 部署事务测试
├── docker-compose.yaml         # 源码构建的应用 + Langfuse 全栈
├── compose.production.yaml     # 基于发布镜像的两个应用
├── compose_initial.sh          # 根 Compose 包装器，首次创建部署 .env
└── CICD_GUIDE.md                # CI/CD 配置和首次切换指南
```

两个项目使用独立的 `pyproject.toml` 与 `uv.lock`，均要求 **Python 3.13 系列**。MCP 的五个配置文件及 `services/lark/lark_requests.py` 通过相对符号链接复用 Codex 源码。开发和 Docker 构建须保留完整仓库及链接，不能只复制 `mcp_service/`。两个进程仍各自加载配置和创建请求执行器。

## 配置与数据

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

配置优先级与生命周期：

- `CONFIG_FILE` 可指定业务 JSON 文件；它不改变应用 dotenv 的默认位置。容器首次启动只在默认配置缺失时原子创建文件，已有文件不覆盖；自定义路径必须预先存在。
- 两个服务都完整校验同一份 JSON Schema，任何配置段不合法都可能阻止启动；校验通过后各自使用相关字段。
- JSON 中的 `apiKeyEnv` 等字段填写**环境变量名**，不填写密钥。网关启动要求 `LARK_APP_ID`、`LARK_APP_SECRET`，且 `LARK_ROBOT_UNION_ID` 或 `LARK_ROBOT_NAME` 至少有一项非空；推荐使用 Union ID。
- ASR 与文生图默认读取 `DOUBAO_API_KEY`。MCP 在调用具体工具时才检查其凭据：缺少图像凭据不阻止文件工具使用，反之亦然。模型是否可用还取决于提供方与账户权限。
- Codex 主任务的模型及 provider 来自 `codex.models`，认证与 provider 连接配置由实际运行环境中的 Codex 管理。仓库不会自动注册 MCP 或覆盖 Codex 配置。
- 修改挂载的业务配置后需重启应用；修改源码或镜像依赖后需重建镜像并重建容器，仅重启旧容器不会更新代码。

首次处理每个用户或群的任务时，会准备 Git 和 Office 依赖环境，即使任务只是文本对话也会执行该检查。初始化需要 Git、uv、Node.js ≥22、pnpm 和包源网络访问，默认总期限 600 秒。已有完整环境可复用；现存非受管或不兼容环境会报错，不自动删除。缺失的 `AGENTS.md` 只创建空文件，使用规则需要自行配置。

## 本地部署与启动

推荐使用 Docker，Codex 镜像已包含 Python、Node.js、uv、pnpm、Git、ffmpeg/ffprobe、LibreOffice、Poppler、qpdf、Pandoc、Tesseract 中英日 OCR 和 Inter/Noto CJK 字体。用户级 Python/npm 文档依赖仍在首次任务时安装。

### 1. 准备运行环境

需要 Docker 与 Compose、用于首次生成部署凭据的 OpenSSL，以及可访问飞书、模型服务、镜像和包源的网络。

```sh
# 从仓库根目录执行；不覆盖既有文件
mkdir -p "$HOME/.codex" "$HOME/.codex_bridge"
docker network inspect ai-infra >/dev/null 2>&1 || docker network create ai-infra
```

在 `~/.codex_bridge/.env` 中配置自己的真实应用凭据，并按需要设置 `config.json`；可参考[默认值](codex_bridge/configs/config_default.json)和 [Schema](codex_bridge/configs/config_schema.json)。不要用占位密钥启动服务，也不要覆盖已有部署的凭据。

在飞书应用后台启用机器人和长连接事件接收，配置消息接收、消息撤回及卡片交互回调所需能力，并授予读取消息/资源、发送与更新卡片、上传文件和操作 reaction 等对应权限。事件注册以 [main.py](codex_bridge/main.py) 为准；本项目不自动创建应用、申请权限或发布应用版本。

同时准备实际容器使用的 Codex 认证与 provider 配置。宿主机的路径、`localhost` 地址及原生二进制插件不能直接假定在 Linux 容器内有效。挂载目录须允许容器用户写入。预构建镜像的用户 UID/GID 为 1000；宿主机 UID 不是 1000 时，在根 `.env` 中设置 `APP_UID` 为宿主机 UID，Compose 会以该 UID、镜像组 1000 运行容器，并将依赖缓存重定向到 `.codex_bridge` 挂载内。

### 2. 构建并启动

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

### 3. 连接 MCP 并检查就绪

| 调用位置 | 默认 MCP 地址 |
| --- | --- |
| 宿主机客户端 | `http://127.0.0.1:8000/mcp` |
| 同一 `ai-infra` 网络内的容器 | `http://mcp_service:8000/mcp` |

在实际使用的 Codex 配置中注册对应地址。容器内的 `127.0.0.1` 指向该容器自身。根 Compose 的 `MCP_PORT` 同时控制宿主机映射和容器监听端口，`MCP_BIND_ADDRESS` 默认 `127.0.0.1`；`MCP_PATH` 可覆盖默认路径 `/mcp`。

```sh
./compose_initial.sh exec codex_bridge python -m codex_bridge.utils.health
./compose_initial.sh exec mcp_service python -m mcp_service.utils.health
```

Codex 探测主循环心跳及飞书连接，MCP 探测协议握手和工具注册；两者均不验证真实模型调用、文件上传或 Office 任务。启动后还需用真实对话验证目标环境的完整链路。根 Compose 未配置自动 healthcheck，生产 Compose 已配置；仅显示容器运行中不等于服务就绪。

### 源码运行

从完整仓库根目录执行，先准备 Python 3.13、uv 及前述系统工具、应用凭据和 Codex 配置：

```sh
uv sync --project codex_bridge --locked
uv sync --project mcp_service --locked

# 仅初始化缺失的默认业务配置，不生成应用凭据
codex_bridge/.venv/bin/python -B codex_bridge/configs/initialization.py

# 分别在两个终端运行
codex_bridge/.venv/bin/python -m codex_bridge.main
mcp_service/.venv/bin/python -m mcp_service.server
```

安装为包后也提供 `codex_bridge` 和 `mcp_service` 入口。工作区初始化使用 POSIX 文件锁和进程组，原生 Windows 不是当前运行目标。

## Langfuse 与观测

根 Compose 启动 Langfuse Web、Worker、PostgreSQL、ClickHouse、MinIO 和 Redis，仅发布 Web 到 `127.0.0.1:3000`。`NEXTAUTH_URL` 默认 `http://localhost:3000`，应与实际浏览器访问地址一致。数据库、Redis 和 S3 不发布宿主机端口。

事件存储默认走内部 `http://minio:9000` 的 `langfuse` 桶；自定义桶须预先存在。当前未配置可选的 S3 媒体上传，浏览器直传或读取 MinIO 媒体附件不在这套编排的默认能力内。启动 Langfuse 不会自动启用 Codex tracing，还需在实际 Codex 环境配置对应插件、地址和凭据。

默认数据库连接串由 PostgreSQL 环境变量构造。凭据含 URI 特殊字符时，通过 `DATABASE_URL` 提供正确编码的完整连接串；修改环境变量不会自动修改已有数据库账户。内置 MinIO 的事件存储统一使用 `MINIO_ROOT_USER` 与 `MINIO_ROOT_PASSWORD`。

已有观测服务时，应先核对端口、容器名和数据卷，避免重复启动。基础设施使用滚动镜像标签，升级前需独立评估数据兼容性并备份。

## CI/CD 与生产发布

| Workflow | 触发条件 | 实际工作 |
| --- | --- | --- |
| [CI](.github/workflows/ci.yml) | 面向 `main` 的 PR、手动、可复用调用 | 在原生 ARM64 runner 升级解析 Python 依赖，构建两个镜像，在容器运行测试，并验证部署逻辑 |
<<<<<<< HEAD
| [Release](.github/workflows/release.yml) | 推送 `main` 或 `vMAJOR.MINOR.PATCH` tag、手动、每周一 02:23 UTC | 调用 CI；将同一批通过测试的镜像发布到 GHCR，不重新构建；生成 `release` artifact |
=======
| [Release](.github/workflows/release.yml) | 推送 `main`、手动、每周一 02:23 UTC | 调用 CI；将同一批通过测试的镜像发布到 GHCR，不重新构建；生成 `release` artifact |
>>>>>>> main
| [Deploy](.github/workflows/deploy.yml) | 仅 `main` 手动触发 | 验证指定的成功 Release，下载对应部署包，通过 SSH 更新生产应用 |

生产目标为 Apple Silicon 对应的 **Linux ARM64 Docker**；流水线没有构建 amd64 镜像。镜像使用 `ghcr.io/<owner>/<repo>/codex_bridge` 和 `mcp_service`，部署按 `release.json` 中的 digest 和 revision 校验。源码中的 workflow 不代表目标仓库和生产主机已经配置或运行成功。

<<<<<<< HEAD
推送 `v1.0.0` 这样的 Git tag，会为两个镜像增加 `1.0.0` 标签，并将同一镜像推送为 `latest`，同时保留 `sha-…-run-…` 追溯标签。版本号由发布者指定，不自动递增；仅支持无前导零的三段数字正式版本。普通 `main` 推送、手动和定时发布不更新 `latest`。`latest` 指向各镜像最近成功推送的正式发布，不按版本号大小排序；重跑旧版本或并行发布也可能改变其指向，精确部署应使用版本号或 digest。Deploy 仍只接受来自 `main` 的 Release run，不能填入 tag 触发的 run ID。

先提交并推送 workflow 修改，再在包含该修改的目标提交上执行 `git tag v0.1.2` 和 `git push origin v0.1.2`（版本号为示例，请使用尚未发布的实际版本）。已有 Git tag 的 workflow 不会随 `main` 更新；新的发布成功后，Compose 即可使用两个镜像的 `:latest`。私有镜像须先登录 GHCR，再运行 `docker compose pull codex_bridge mcp_service` 和 `docker compose up -d --no-build codex_bridge mcp_service` 更新应用。

使用前在 GitHub `production` Environment 配置 Secrets：`DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`、`DEPLOY_KNOWN_HOSTS`，以及 Variable `DEPLOY_ROOT`。目标主机需准备 `host.json`、应用配置、挂载目录、网络、GHCR 拉取授权，以及 Docker Compose 和 Python 3.9+。SSH 使用端口 22 并严格核验主机公钥。

生产 Compose 只管理两个应用，容器用户固定为 `app`，MCP 容器端口固定为 8000，`host.json` 的 `mcp_port` 仅改变宿主机回环端口。首次切换前需停止旧项目的两个应用，保留基础设施与数据；不能让两个网关同时消费消息。

=======
使用前在 GitHub `production` Environment 配置 Secrets：`DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`、`DEPLOY_KNOWN_HOSTS`，以及 Variable `DEPLOY_ROOT`。目标主机需准备 `host.json`、应用配置、挂载目录、网络、GHCR 拉取授权，以及 Docker Compose 和 Python 3.9+。SSH 使用端口 22 并严格核验主机公钥。

生产 Compose 只管理两个应用，容器用户固定为 `app`，MCP 容器端口固定为 8000，`host.json` 的 `mcp_port` 仅改变宿主机回环端口。首次切换前需停止旧项目的两个应用，保留基础设施与数据；不能让两个网关同时消费消息。

>>>>>>> main
部署脚本先预检并拉取镜像，再停止旧应用、备份 `config.json` 与 SQLite、启动并等待就绪，成功后写入 `current.json`。新版本切换失败时，只有存在旧发布且明确确认 `rollback_compatible` 才回退旧镜像；**不自动恢复数据库，不备份整个工作区，也不提供零停机或自动配置/数据迁移**。应用自身仍会按实现初始化表或补充兼容字段，镜像回退前须确认实际数据兼容性。

部署详情、首次切换和失败恢复见 [CI/CD 指南](CICD_GUIDE.md)。其中的历史测试数量、实施记录和原始规划不应视为当前版本或当前部署的验证结果。

## 能力边界与注意事项

- **安全边界：** MCP 当前无鉴权，文件工具从解析后的绝对路径中提取唯一接收方目录；目录名须匹配 `(?:on_|oc_)[A-Za-z0-9]+` 且总长为 35。它没有额外校验调用者身份或文件是否属于指定工作区，因此目录规则不等于授权控制。保持本机绑定，仅向受信任的客户端和网络开放。
- **凭据：** 密钥通过环境变量或受控 Secret 配置，不提交真实值；业务配置、日志和工作区在分享前应检查敏感信息。
- **沙箱：** 默认 `workspace-write` 需要运行器允许非特权 user namespace，且 seccomp、AppArmor/SELinux 不阻止 Bubblewrap。当前 Compose 为 Codex 设置 `seccomp=unconfined`，会放宽容器限制；这不是独立多租户隔离方案。应在目标运行器验证沙箱，不自动修改宿主机内核策略。
- **附件：** 默认每批最多 10 个附件，与 `messaging.historyPageSize` 共用参数。支持范围由 `resources.acceptedExtensions` 和实际格式校验共同决定；不因扩展名合法就接受内容。旧 `.doc/.xls/.ppt`、通用压缩包和视频不在默认输入白名单中。部分附件失败会明确提示并继续处理可用内容；附件全部不可用时，不单独提交伴随文本。
- **音频与图像：** 音频先经 ffmpeg/ffprobe 处理，再调用外部 ASR；不提供本地离线识别。图像工具只接收提示词并返回首张图片 URL，不自动下载或发送文件；`mcp.videoModel` 配置存在不代表视频工具已实现。图像请求使用提供方特定参数，不能保证任意兼容接口都接受。
- **时间与容量：** 默认任务总期限 900 秒、启动期限 120 秒、空闲超时关闭；普通入站事件上限 32，每进程飞书请求并发上限 8。这些来自当前默认配置及小团队初始保护策略，不是吞吐或 SLA 承诺。超时、取消不能强制终止已经阻塞的同步请求线程。
- **停止与恢复：** 开发全栈 Compose 的退出宽限为 30 秒，生产为 60 秒；进程结束可能中断外部请求或卡片收尾。没有持久化任务队列，重启不保证恢复在途工作。停止或回退镜像不能撤销已发送消息和其他外部副作用。
- **数据维护：** 工作区、附件、日志、会话和发布备份需要运维安排容量、访问控制及备份策略。token 用量仅写 SQLite，不再生成 CSV；旧 `storage.tokenUsagePath` 仅兼容保留。备份正在使用 WAL 的 SQLite 时，不应只复制主数据库文件。

## 开发与验证

```sh
# 在仓库根目录、完成 uv sync 后运行
codex_bridge/.venv/bin/python -B codex_bridge/tests/run_tests.py
mcp_service/.venv/bin/python -B mcp_service/tests/test_runtime.py
codex_bridge/.venv/bin/python -B test_deploy.py -v

git diff --check
```

测试覆盖消息/附件处理、会话与停止控制、并发及超时、工作区初始化、用量、MCP 和部署失败/回退逻辑。测试使用临时配置和模拟外部客户端，MCP 另含本地 HTTP 握手验证；不能替代真实飞书、模型、GHCR、SSH 和生产切换验收。

两个镜像始终从仓库根目录构建：

```sh
docker build -f codex_bridge/Dockerfile -t codex_bridge .
docker build -f mcp_service/Dockerfile -t mcp_service .
```

默认 Docker 构建使用 `uv sync --upgrade`；需要重新获取基础镜像及依赖时使用 `--pull --no-cache`。CI 每轮先执行 `uv lock --upgrade --python 3.13`，再传入 `--build-arg UV_SYNC_FLAGS=--locked`，使构建和测试使用本轮同一依赖快照。CI 锁文件作为 artifact 保存，不自动提交回仓库。

Python 基础镜像跟随 3.13 补丁，Node 跟随 LTS，uv 与 pnpm 跟随滚动版本；用户工作区首次安装时也解析最新兼容依赖，已有完整环境不会自动追新。镜像 digest 可追溯产物，不代表构建可逐字节复现。包版本由 `setuptools-scm` 推导，无 Git/分发元数据时回退为 `0.0.0`，不作为生产发布依据。

根 [.dockerignore](.dockerignore) 使用源码白名单并排除测试、虚拟环境、凭据和缓存；新增 Codex 源码目录或资源时须同步检查构建输入。共享模块变化需验证两个服务，并重新安装或构建才能更新已有 wheel/镜像。

进一步阅读：[Codex 服务说明](codex_bridge/README.md)、[MCP 服务说明](mcp_service/README.md)、[测试说明](codex_bridge/tests/README.md)、[会话与历史恢复](codex_bridge/README.md#会话持久化与历史恢复)、[流式卡片与样式](codex_bridge/README.md#流式卡片与样式定制)、[日志与 Token 用量](codex_bridge/README.md#日志与-token-用量)。实现细节统一收录于各项目 README，历史维护记录不代表当前部署验证结果。

## 许可证

本仓库使用 [MIT License](LICENSE)。模型服务、飞书、插件及其他第三方组件遵循各自的许可与服务条款。

# Codex-Lark-Bridge

Codex-Lark-Bridge 是飞书与 Codex 之间的任务网关：接收私聊或群聊消息，下载和校验附件，准备持久化工作区，通过 Codex SDK 执行任务，并以飞书流式卡片交付进度和结果。

它负责消息、会话和任务生命周期；文件发送与文生图由独立的 [MCP](../mcp_service/README.md) 提供。完整部署拓扑、Langfuse 和生产配置见[仓库 README](../README.md)，发布流程见 [CI/CD 指南](../CICD_GUIDE.md)。

快速导航：[功能与使用](#功能与使用) · [配置与凭据](#配置与凭据) · [启动](#启动) · [会话与历史](#会话持久化与历史恢复) · [附件处理](#消息组装与附件处理) · [卡片定制](#流式卡片与样式定制) · [任务控制](#任务控制与资源回收) · [日志与用量](#日志与-token-用量) · [测试与维护](#测试健康检查与维护)。

## 功能与使用

| 场景 | 当前行为 |
| --- | --- |
| 私聊 | 接收当前用户的任务，按飞书 Union ID 使用工作区和 Codex thread |
| 群聊 | 当前消息需提及机器人；按群 ID 共享工作区、上下文和 thread |
| 文本、富文本、语音 | 立即拉取并组装相关聊天历史；语音先转写为文本 |
| 图片、文件 | 默认进入 10 秒固定缓冲窗口，可与随后发来的任务说明一起处理 |
| 运行中补充要求 | 尝试通过 steer 加入当前任务；追加图片若要求切换模型，会提示等待或停止后重发 |
| 进度与结果 | 添加处理中 reaction、持续更新卡片，在结束或异常收尾时清理状态 |
| 会话持久化 | SQLite 保存 thread 绑定、历史索引和 token 用量；Codex 会话数据保留在其自身数据目录 |

在私聊中直接发送任务；群聊中提及机器人并说明任务。需要交付文件时，运行环境必须配置可用的文件发送工具，生成服务器路径本身不等于文件已送达。

| 命令 | 用途 |
| --- | --- |
| `/stop` | 停止当前任务、取消消息缓冲；退出未确认时会暂时阻止该会话继续提交任务 |
| `/new` | 停止任务、归档旧 thread 并清除当前绑定，下一条消息创建新会话；保留工作区文件 |
| `/history` | 仅私聊显示历史选择卡片并激活所选会话；默认只展示最近 30 条，不通过翻页访问更早记录 |

命令以独立文本发送。撤回消息会尝试移除缓冲内容或停止关联任务，但不会撤销已经发生的文件修改、消息发送或其他外部操作。群聊成员共享任务状态，不能通过群聊实现成员级数据隔离。

## 运行链路与代码结构

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
| [main.py](main.py) | 服务入口、依赖组装、飞书事件注册、健康心跳与关闭处理 |
| `middleware/message_router.py`、`message_collector.py` | 消息过滤、缓冲、命令识别与历史批次收集 |
| `middleware/message_assemble.py`、`resource_validator.py`、`audio_transcription.py` | 输入转换、附件格式校验与 ASR |
| `middleware/gateway_execution.py`、`gateway_runtime.py`、`gateway_commands.py` | 卡片交付、任务索引、停止与重置、历史交互和收尾 |
| `codex/codex_execution.py`、`codex_runtime.py` | `Codex`、模型路由、SDK 生命周期、steer、重试和错误转换 |
| `codex/codex_workspace.py`、`thread_manager.py`、`thread_watchdog.py` | 工作区初始化、thread 绑定及超时控制 |
| `session/` | 会话缓存、恢复与历史索引 |
| `services/lark/` | 飞书 API、流式/交互卡片及受限并发请求 |
| `configs/` | 默认配置、Schema、校验、加载和首次初始化 |
| `utils/` | 日志、token 用量、事件分发、请求执行器和健康检查 |
| `tests/` | 离线回归测试；实现细节和维护说明统一收录于本文 |

`create_gateway()` 组装共享同一 `SessionCache` 的 runtime、execution、commands 和 router。服务直接使用 `AsyncCodex()`，不会自动注册 MCP、安装插件或覆盖用户 Codex 配置。

## 配置与凭据

默认读取 `~/.codex_bridge/config.json`，可通过 `CONFIG_FILE` 指向其他现有文件。字段定义见[默认配置](configs/config_default.json)和 [Schema](configs/config_schema.json)。完整 Schema 校验失败会阻止启动，包括本服务不直接使用的配置段。校验采用 Draft 2020-12 与格式检查，拒绝非有限数值；直接/缓冲消息类型必须不相交，且并集等于支持类型；两个自定义命令不能相同或含空白；音频目标大小不能超过最大大小。Schema 约束错误报告文件路径、字段路径和约束名，不回显配置值。

应用只从 `~/.codex_bridge/.env` 加载 dotenv，已有进程环境变量优先；更改 JSON 路径不会改变 dotenv 的位置。JSON 中的凭据字段保存环境变量名，不保存密钥值。

| 配置或环境变量 | 作用 |
| --- | --- |
| `LARK_APP_ID`、`LARK_APP_SECRET` | 默认飞书应用凭据，网关启动时必须提供 |
| `LARK_ROBOT_UNION_ID`、`LARK_ROBOT_NAME` | 至少一项非空，用于群聊提及识别；推荐使用 Union ID，名称也可参与匹配 |
| `DOUBAO_API_KEY` | 默认 ASR 凭据；由 `audio.asr.apiKeyEnv` 指定实际变量名，在调用时使用 |
| `codex.models` | 文本、图片和多模态输入的模型/provider 路由；对应认证和连接由 Codex 配置管理 |
| `codex.sandbox` | 默认 `workspace-write` |
| `storage` | 工作区、日志、附件子目录与 SQLite 路径 |
| `messaging`、`resources` | 缓冲、历史、支持的消息类型与附件扩展名 |
| `codex.watchdog`、`lark.requestConcurrency` | 任务期限、请求超时及飞书请求并发限制 |

飞书后台需启用机器人和 WebSocket 长连接，配置消息、撤回和交互卡片事件，并授予读取消息/资源、发送与更新卡片、文件上传和 reaction 等相应权限。应用创建、权限申请及发布不由代码自动完成。

默认数据位置：

```text
~/.codex_bridge/
├── config.json
├── .env
├── state.sqlite                # thread 绑定、会话索引、token 用量
└── logs/                       # 业务运行日志
~/.codex/
└── workspace/<on_…或oc_…>/
    ├── AGENTS.md               # 缺失时创建空文件，已有内容保留
    ├── .git/                  # 已有 Git/worktree 则复用
    ├── .venv/                 # 当前用户/群的 Python 依赖
    ├── package.json、pnpm-lock.yaml、node_modules/
    └── resources/inbound/     # 默认入站附件位置
```

Codex 自身的认证、配置和会话也保存在实际 Codex 数据目录。相对存储路径按业务 JSON 所在目录解析；容器中的 `~` 指容器用户 HOME。token 用量仅写 SQLite，旧 `storage.tokenUsagePath` 不再用于生成 CSV。

## 启动

以下命令均从**仓库根目录**执行。服务要求 Python `>=3.13,<3.14`，首次任务的工作区初始化还需要 Git、uv、Node.js ≥22、pnpm 及包源网络访问。音频处理需要 ffmpeg/ffprobe，文档转换、OCR 等还依赖相应系统工具。

### 源码运行

先配置真实飞书凭据和可用的 Codex 认证/provider，再执行：

```sh
uv sync --project bridge --locked
# 只创建缺失的默认配置，不覆盖现有配置，也不生成业务凭据
bridge/.venv/bin/python -B bridge/configs/initialization.py
bridge/.venv/bin/python -m bridge.main
```

也可在 `bridge/` 中执行 `.venv/bin/python main.py`，安装为包后使用 `bridge`。本地入口不会自动初始化配置，需先完成上述初始化或提供自定义配置文件。

### Docker

```sh
docker build -f bridge/Dockerfile -t bridge .
# 已完成根 Compose 的网络、挂载和凭据准备后
./compose_initial.sh up -d --build bridge
```

Docker 构建上下文必须为完整仓库根目录。镜像含 Python 3.13、Node LTS、uv、pnpm、Git、音频工具、LibreOffice、Poppler（含 CMap 数据）、qpdf、Pandoc、Tesseract 中英日 OCR 和 Inter/Noto CJK 字体；不预装 Apple 专有字体或 GCC。

容器入口会原子创建缺失的默认业务配置，已有配置保留；默认挂载 `~/.codex_bridge` 和 `~/.codex` 到 `/home/app/` 下。根 Compose 即使只启动本服务，也要求完整的 Langfuse/存储部署变量，详见[仓库启动说明](../README.md)。

## 工作区与能力边界

- **首次任务需要安装依赖。** 即使只进行文本对话，也会检查当前用户的 Office 环境。Python/npm 包清单定义在 `codex/codex_workspace.py`，首次解析最新兼容版本；成功后按环境状态复用，不会因上游发布新版而自动升级。
- **初始化有边界。** 默认总期限 600 秒，Git 初始化期限 10 秒，来自当前初始化策略与默认配置。同用户初始化串行，跨进程使用文件锁；安装失败、非受管环境、清单冲突或 Python 次版本不兼容时明确报错，不自动删除工作区或覆盖用户文件。
- **任务环境独立设置。** 通过 `shell_environment_policy.set` 为任务设置工作区 `VIRTUAL_ENV` 和 `PATH`，不修改服务全局环境。依赖和目录隔离不等于强多租户安全隔离。
- **输入不是任意文件。** 默认每批最多 10 个附件，与 `messaging.historyPageSize` 共用参数。扩展名与实际内容都要通过检查；部分附件失败时提示并保留可用输入，全部附件不可用时不会只提交伴随文本。语音转写依赖外部 ASR，不是离线识别。
- **沙箱依赖宿主机能力。** 默认 `workspace-write` 需要 user namespace 与 Bubblewrap 可用。Compose 设置了 `seccomp=unconfined`，会放宽容器限制；不能代替对运行器及任务权限的评估，代码不自动修改宿主机内核策略。
- **停止不撤销副作用。** 默认任务总期限 900 秒、启动期限 120 秒，空闲超时关闭；这些是配置策略，不是 SLA。飞书同步请求已进入线程后，取消或超时不能强制结束线程。没有持久化任务队列，进程重启不保证恢复在途任务。
- **外部能力需要另行配置。** 文档处理质量取决于输入、字体、模型与工具；安装系统依赖不等于插件或 MCP 已可用。业务配置更新需重启；源码或依赖更新需重建镜像与容器。

## 会话持久化与历史恢复

### 活跃绑定与首次命名

[thread_manager.py](codex/thread_manager.py) 使用 `aiosqlite` 在 `storage.databasePath` 下保存 `user_thread` 表：`user_id TEXT PRIMARY KEY NOT NULL`、`thread_id TEXT NULL`。`get_user_thread(user_id)` 在记录不存在或绑定已重置时返回 `None`；`set_user_thread(user_id, thread_id)` 采用参数化 SQL 和原子 upsert，传 `None` 持久化为 SQL NULL。每次操作独立打开并关闭连接，不降级为内存绑定。

WAL 与建表初始化遇到 `SQLITE_BUSY`（含扩展错误码）时，先关闭失败连接，再异步等待后重试；总预算沿用原来的 30 秒，50ms 退避与历史会话模块一致。初始化期间禁用 SQLite 内部锁等待，避免每轮各等待 30 秒；成功后恢复业务 SQL 的 30 秒锁等待。超过预算、非 BUSY 错误和取消直接传播。重试只发生在连接交给调用方之前，不重新执行绑定读写或提交，不修改表结构或迁移数据。

每次请求恢复或创建 thread，在启动 turn 前保存绑定；即使 turn 启动失败，下次仍可恢复该 thread。`/new` 先确认旧任务停止，再归档旧 thread 并清空绑定，失败时不报告成功；它不删除历史或工作区。旧版本仅在内存中的绑定不会自动迁移。

[session_history.py](session/session_history.py) 在同一数据库中创建 `session_history` 表和排序索引，不替换活跃绑定表：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | TEXT NOT NULL | 私聊 Union ID 或群 chat ID，与 `thread_id` 组成主键 |
| `thread_id` | TEXT NOT NULL | Codex thread ID |
| `thread_name` | TEXT NULL | 首次文本生成的名称；纯附件输入可暂为空 |
| `updated_at` | INTEGER NULL | 从 SDK 同步的 Unix 秒级更新时间 |

首次命名合并换行及连续空白，取前 **15 个 Unicode 字素簇**，超长追加 `…`；使用 `regex` 的 `\X` 保留组合字符与 emoji，不截断实际模型输入。输入列表只提取 `TextInput`（含语音转写），忽略附件路径；纯附件会话在首次收到文本或 steer 文本时命名。已完成的名称不随后续消息变化。

名称候选先落库，再同步 SDK 名称和时间。`thread_name` 非空但 `updated_at` 为空表示命名尚未同步完成，普通时间同步不会跳过该状态；重试前若远端名称已一致，就不重复命名。旧绑定不会自动回填历史，避免将新消息错当首次提示词。SQLite 与 SDK 没有共同事务，不保证故障时网络操作严格只发生一次。

收到 `turn/completed`（包括成功、失败或中断）及恢复历史时同步 SDK 时间，只允许时间前进，不用本地时钟伪造。列表按 `updated_at` 降序、同秒按 `thread_id` 降序、空时间最后排序；解除归档若未推进 SDK 时间，不会人为置顶。断流或强制退出不保证同步到最新时间，同步失败不重发输入、不改写已经确定的模型结果。

### 历史选择与恢复接口

`/history` 使用 [lark_interactive_card.py](services/lark/lark_interactive_card.py) 的 Card 2.0 表单：下拉选择本身不回调，点击 `Activate this session` 后由 `confirm` 确认提交；取消不停止任务、不解除归档、不写数据库。空名称显示 `Untitled session`。

`messaging.sessionHistoryLimit` 默认 30，是用户指定的展示策略，独立于聊天历史/附件上限 `historyPageSize`。网关先截取最近记录，卡片 `PAGE_SIZE` 使用同一上限，所以当前 `/history` 不产生多页；底层虽保留分页与 revision 检查，也不能据此访问截取范围之外的旧记录。卡片保存发送时的选项快照，需要最新列表时重新发送 `/history`。

服务端保存随机 ticket、操作者 Union ID、原私聊 chat ID、message ID 和选项。回调核对 `operator.union_id`、`context.open_chat_id`、`context.open_message_id`、ticket 与 revision，仅接受激活按钮及 `action.form_value.history_thread` 中的可见选项；不接受任意前端 user ID、`action.option` 或选择事件直接切换。恢复前再次查询数据库归属，每张卡的确认只消费一次，失败后须重新获取卡片。

历史卡片只保存在当前进程内，24 小时失效，容量 1024；满时淘汰最早的非处理中卡片，全部忙碌则拒绝创建。这些值是初始内存策略，不是平台限额。重启后旧卡失效，不支持多网关共享卡片状态。SDK 回调通过控制事件容量异步提交；处理中提示不代表恢复成功。结果卡交付失败不重放恢复操作，也不撤销已经提交的绑定。

供网关适配层复用的入口是异步 Python 方法，不是 HTTP API：

```python
from bridge.middleware.gateway_commands import GatewayCommands

async def restore_selected_session(
    commands: GatewayCommands, data, selected_thread_id: str
):
    # commands 来自启动层共享实例；data 必须来自已验证的消息身份。
    options = await commands.history_options(data)
    result = await commands.processing_history_restore(data, selected_thread_id)
    return options, result
```

`history_options` 返回带 `label`、`value`、`updated_at` 的选项，查询失败直接抛错，不能伪装成空列表；恢复结果用 `ok` 表示成功与否，成功包含 `thread_id`。选择当前绑定直接成功，不停止或重复解除归档。切换其他 thread 时，先通过 reset 门禁、停止确认、退出等待及提交锁，再调用 SDK `thread_unarchive`，读取元数据并完成客户端清理，最后同步时间和绑定。

恢复不额外归档此前的活跃 thread，不创建替代 thread，也不重新命名已完成命名的目标。未归档目标仍按约定调用 `thread_unarchive`，SDK 拒绝则失败。失败保留原绑定，但 SDK 状态可能已经变化，不能保证回滚远端操作。底层 `Codex.restore_session` 只供已完成停止与锁保护的调用方使用；并发保证基于单网关、按 chat ID 串行提交。

## 消息组装与附件处理

### 命令、缓冲与去重

`/new`、`/stop` 仅匹配原始 `text` 消息的完整文本，忽略大小写和首尾空白，不接受参数，不从富文本或 ASR 结果中识别。实际配置字段为 `messaging.newThreadCommand` 与 `messaging.stopThreadCommand`。群聊仍要求事件提及机器人；命令解析不会自动剥离 mention 占位符。私聊 `/history` 优先匹配且是固定命令，其他命令不应配置成同名。

历史批次以触发消息为上界，遇到机器人消息、`/new`、`/stop` 或私聊 `/history` 停止向前收集。不支持的消息类型独立提示并清理 reaction，不取消已有附件缓冲。重置期间新输入等待，停止/重置前的缓冲和待处理输入通过会话 generation 失效。

每个会话的已接收/已处理 ID 集合各保留最多 `messaging.recallCacheMaxEntries` 条，默认 1024，最长 24 小时；重复事件不续期。历史重叠和已接受的 steer 消息不会重复提交，但去重不跨进程持久化，超过容量或期限后不保证去重。失败旧消息不会被后续历史自动重试，需要重新发送。撤回只处理 `message_owner` 事件，停止关联消息所属批次；撤回已接受的 steer 消息会停止整个原任务。

### 富文本附件与格式验证

富文本按标题、正文文字/图片、顶层 `files` 数组的顺序组装；附件记录使用 `file_key`、`file_name`、`is_folder`，使用所属消息 ID 下载，以 `MentionInput` 提交文件。文件名缺失时显示名使用消息 ID 和序号，实际落盘名由下载响应与格式校验决定。`is_folder=true` 明确拒绝，不递归下载；异常数组、条目、布尔类型或资源 key 都产生拒绝说明。不因缺少 `files` 强制把图片富文本改成 text。

优先解析 `content_v2`，否则使用 `content`，两份兼容字段不重复累计。`historyPageSize` 默认 10，同时限制每次历史请求条数和每批附件总数；图片、文件、音频以及解析拒绝项都计数。超限在下载/ASR/模型提交之前整批拒绝。部分附件成功时保留正文和有效附件，全部不可用时连同任务正文一起不提交。

[resource_validator.py](middleware/resource_validator.py) 联合检查扩展名、MIME 和内容：

- 文本/代码先检查 UTF-8（可含 BOM）和二进制控制字符，通过后保留原扩展名；不猜测编码、不转码、不执行代码或检查语言语法。JSON 必须能够解析，不能用 `text/plain` 绕过；JSONL 按文本处理。
- DOCX/XLSX/PPTX 检查 ZIP 目录、`[Content_Types].xml` 和 `_rels/.rels`，要求唯一内部主文档关系、非空主部件和匹配的内容类型；支持安全的自定义主部件路径，检查时不解压到工作区。
- 每份 Office XML 元数据最多读取 1 MiB，支持 UTF-8 或带 BOM 的 UTF-16，拒绝实体声明、重复部件及不安全路径。这是项目预算，不是格式上限；不遍历全部正文部件、不提供杀毒或宏扫描，也不保证文档可被所有应用打开。
- 图片、音频和 PDF 按实际实现进行签名/元数据校验，仍须满足扩展名白名单。格式拒绝会保留具体原因，不伪装为网络下载失败。

### 音频转换与文件清理

`audio.limits.conversionTimeoutSeconds` 默认 540 秒，覆盖单个音频从 ffprobe 检测到全部 ffmpeg 转码/切片完成，不含 ASR 网络时间，切片不重置计时。超时或取消先终止子进程，2 秒未退出则强制回收并清理临时目录；现有任务总期限可能更早触发。转换失败通过附件通知反馈，不把错误文案作为转写结果提交。

进入 `ASR.transfer()` 的原始 `.ogg` 文件（忽略大小写）在成功、失败、空文本、配置缺失、超时或取消后都会尝试删除，包括以普通文件上传的 OGG；其他格式的原文件保留，临时 M4A/切片随临时目录清理。OGG 删除失败只记日志，不覆盖原结果。停止/撤回沿用中断流程，不额外发送转写失败通知。

## 流式卡片与样式定制

### 发送接口与交付语义

[lark_message_card.py](services/lark/lark_message_card.py) 的 `sending_card(union_id, content, session=...)` 接受私聊 Union ID 或 `oc_` 群 ID；字符串发送静态通知，异步迭代器中的字符串追加正文，`CardReplace` 替换当前正文与缓冲，`CardSteer` 用作内部写入屏障。`CardStreamSession` 可共享单个任务的卡片、控制和停止状态。成功返回最后一张卡片的消息 ID，空内容返回 `None`。

```python
from bridge.services.lark.lark_message_card import CardReplace, sending_card

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

[thread_watchdog.py](codex/thread_watchdog.py) 按 run ID 记录接收时间、阶段、thread/turn ID、消息归属、最后活动、工具执行、终态和交付结果。以下秒数来自当前默认配置，是项目运行策略而非外部平台保证：

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

`storage.runLogPath` 默认 `~/.codex_bridge/logs`，按 `runtime.timezoneOffsetHours` 时区的入队日期写入 `YYYY-MM-DD_logs.jsonl`，跨日自动分文件，失败重试不改日期。单后台线程批量追加并 flush，按约 100ms 调度，不整文件重写、不限制最近 10000 条；该间隔不是落盘时效保证，旧 `log.json` 不再写入。

生命周期和 `item/completed` 白名单摘要会写入终端/JSONL，不记录 delta 或 item 中间事件，不每秒写 probe。摘要保留类型、ID、状态、耗时和退出码；图片可含保存路径/透明标记，Agent 消息含 phase/字符数；不展开命令输出、Base64、图片提示词、消息正文、工具结果或 turn.items。错误可保留 message、additionalDetails、willRetry，摘要字符串截取前 500 字符；所有事件仍更新内存活动时间和计数，`lastEvent` 仅表示最近可记录事件。

已 flush 记录立即离队，失败保留重试；未写记录入队满 24 小时后丢弃并明确告警，不伪报写入成功。这仅清理内存，不删除历史文件。单网关设计不支持多进程共同写该日志；断电可能丢失未落盘记录或留下不完整末行。

### Token 统计口径

[utils/token_usage.py](utils/token_usage.py) 的 `SavingLog` 提交 SQLite 明细，`finalize_usage` 只回填已知任务耗时。每个 `thread/tokenUsage/updated` 将 `token_usage.last` 的六个增量字段写一行并提交，不保存或再次累计 `token_usage.total`：`cache_write_input_tokens`、`cached_input_tokens`、`input_tokens`、`output_tokens`、`reasoning_output_tokens`、`total_tokens`。

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

## 测试、健康检查与维护

```sh
# 从仓库根目录运行；使用临时配置及模拟外部客户端
bridge/.venv/bin/python -B bridge/tests/run_tests.py
# 可按文件筛选
bridge/.venv/bin/python -B bridge/tests/run_tests.py --pattern test_workspace.py
# 部署事务测试同样位于仓库根目录
bridge/.venv/bin/python -B test_deploy.py -v

# 对已运行的根 Compose 容器进行检查
./compose_initial.sh exec bridge python -m bridge.utils.health
```

健康检查验证进程、主循环心跳及飞书 WebSocket 连接；不调用模型，也不验证附件上传和 Office 输出。生产 Compose 已配置 healthcheck；根 Compose 需手动调用。SIGTERM 会关闭入站并取消缓冲、进入任务中断收尾，开发与生产 Compose 的退出宽限分别为 30 秒和 60 秒，不能保证所有在途外部请求正常完成。

CI 在 Linux ARM64 镜像内运行回归。默认 Docker 构建升级兼容依赖；CI 先升级解析锁文件，再用 `UV_SYNC_FLAGS=--locked` 构建并测试同一快照，Release 直接发布测试过的镜像。生产由根目录 [deploy.sh](../deploy.sh) 使用发布包部署，测试入口为 [test_deploy.py](../test_deploy.py)，不在生产主机重新构建。

本服务的五个配置文件和飞书请求模块由 MCP 通过相对符号链接复用。修改共享源码需同时验证两个服务；已安装 wheel/已构建镜像不会自动跟随源码变化。新增源码目录或资源还需核对根 `.dockerignore` 白名单。包版本由 `setuptools-scm` 推导，无 Git/分发元数据时回退为 `0.0.0`；生产追溯使用镜像 digest 与 revision。

测试范围与运行约定见[测试说明](tests/README.md)。会话、消息、卡片、用量及历史维护资料已统一收录于本文；历史验证记录不替代当前版本的实际执行结果。

## 历史维护记录

以下合并自 2026-09-09 清理记录，仅保留变更背景，**未在本轮重做当时的构建和测试，不作为当前部署步骤**。

- 当时 Codex 顶层依赖由 50 项减至 7 项、锁文件包数 53→35；MCP 移除 `cli` extra、锁文件包数 53→45，并显式声明 `jsonschema`。这些是当时计数，当前依赖以 `pyproject.toml` 和 `uv.lock` 为准。
- 当时采用各服务独立校验、各子目录独立 Docker 构建及任务结束后导出 CSV。后续已改为共享源码、完整 Schema 校验、仓库根构建上下文和仅 SQLite 用量记录；不要沿用旧命令或 CSV 调用约定。
- 当时移除 `storage.logdatabasePath`、`codex.models.file`、`lark.upload.imageType`，分别使用当前 `storage.databasePath`、`codex.models.multimodal` 和现有上传规则；后续机器人身份字段使用 `robotUnionIdEnv`，旧 `robotOpenIdEnv` 不受当前 Schema 支持。业务配置不自动整体迁移，旧字段需先对照 Schema 检查；`mcp.videoModel` 的保留不代表已实现视频工具。
- 清理同时修复同步 `cli()` 入口、保留包级惰性导出，移除重复路由、无效赋值和未使用导入；公共 `Codex.interrupt()` 与 cmd 兼容行为保留。MCP 文件发送的绝对路径、接收方校验和明确错误语义延续，完整边界见 [MCP README](../mcp_service/README.md)。
- 当时记录 Codex 正向/反向各 248 项、MCP 10 项测试通过，以及锁文件、Compose、差异、独立构建和禁网只读容器检查；测试镜像标签为 `independent-check-20260909`。原记录说明未修改宿主运行目录、未替换服务，未调用真实飞书或模型。这些不是当前版本的验证结果。

原线程资料曾记录适配 `openai-codex 0.147.0`，这是历史观测值，不是当前固定版本。当前升级依赖后，仍需验证 SDK 状态查询、进程退出、卡片交付和用量事件口径。

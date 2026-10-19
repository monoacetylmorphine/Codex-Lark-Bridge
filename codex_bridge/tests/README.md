# 测试

## 运行方式

在已安装 `codex_bridge/pyproject.toml`／`uv.lock` 依赖的 Python 3.13+ 环境中，从仓库根目录运行：

```sh
codex_bridge/.venv/bin/python -B codex_bridge/tests/run_tests.py
codex_bridge/.venv/bin/python -B codex_bridge/tests/run_tests.py --pattern test_message_assemble.py -v
codex_bridge/.venv/bin/python -B codex_bridge/tests/run_tests.py --reverse
```

`run_tests.py` 位于 `codex_bridge/tests/`，也可先 `cd codex_bridge` 后执行 `python -B tests/run_tests.py`。测试入口使用 `configs/config_default.json` 生成临时配置，将数据库、工作区和运行日志定向到临时目录，不加载运行环境的 `.env`。不需要启动飞书、Codex 或语音识别服务。原有进程清理测试会启动短暂的本地 Python 子进程。

`--reverse` 反向执行测试，用于检查模块缓存、全局状态和 mock 是否影响其他用例。未匹配任何测试或测试失败时返回非零退出码。仍可使用原来的 `unittest discover -s tests`，但该方式需要自行设置包导入路径和 `CONFIG_FILE`。

## 当前覆盖范围

Watchdog 的启动、空闲与终态期限测试使用统一的可控单调时钟，覆盖零起点、短运行和长运行时钟。终态后仍须验证独立收尾期限：原总运行期限不再生效，重复终态不重置收尾计时，达到收尾边界时返回 `cleanup_timeout`。不要将 `expired(2000)` 等固定测试时间与 `finish()` 读取的真实时钟混用；该混用曾使 GitHub 新 runner 上失败、在本地长时间运行的机器上误通过。

历史会话连接初始化在并发启用 WAL 或建表遇到 `SQLITE_BUSY` 时关闭失败连接后异步重试，沿用 30 秒锁等待预算；调用方业务操作不重放，其他数据库错误和取消直接传播。回归覆盖真实并发注册、一次性初始化竞争、持续竞争超时、非 BUSY 错误、取消及业务错误不重放。该历史会话修复不修改表结构或持久化数据。

线程绑定模块采用相同的初始化重试边界。`test_thread_management.py` 保留真实并发绑定测试，并覆盖真实读事务阻止 WAL 切换后释放恢复、扩展 BUSY 错误码、持续竞争超时、非 BUSY 错误、取消传播、业务错误不重放，以及绑定与历史表在同一数据库中的并发初始化。真实锁测试在观察到 SQLite 返回 BUSY 后释放读事务，不依赖固定睡眠碰并发时序；失败连接必须关闭，初始化成功后必须恢复业务 SQL 的锁等待预算。

工作区依赖直接内置于 `codex_workspace.py`，不读取参考 skills 或额外资源文件。`test_office_workspace.py` 验证安装命令直接接收包名，Node 首次生成用户锁文件且后续复用。部署验证应在不包含 skills-office 的镜像中初始化用户环境。

Office 工作区离线测试位于 `test_office_workspace.py`：覆盖首次初始化、重复调用、旧 Git/worktree 和 AGENTS.md 保留、非受管环境保护、清单冲突、安装失败重试、取消以及同用户串行/不同用户并行。依赖安装由 stub 模拟，不连接包仓库。`test_workspace.py` 保留真实 Git 子进程超时和回收检查。Linux 的 LibreOffice、Poppler、OCR、字体和 npm 原生模块仍需在实际构建镜像中做功能验证。

新增覆盖包括：资源签名/MIME/文件名校验，历史排序与消息边界，富文本和多模态组装，下载与转写失败、取消清理，音频分段边界，配置错误，飞书 SDK 请求参数，SQLite 持久化以及重试退避。

用量测试覆盖逐事件提交 `last` 增量、`runId` 分组、旧表追加字段且保留历史行、结束后耗时回填、异常退出与重复取消不重复插入，以及无用量事件时不写零值行。字段口径见 [用量记录说明](../../README.md#日志与-token-用量)。

本次回归覆盖共享配置的完整校验与符号链接来源、并发首次启动不覆盖配置、`maxRunSeconds` 正整数约束、慢 Git 初始化期间另一会话正常完成、Git 超时及取消回收、附件写入不阻塞事件循环，以及任务结束不生成 CSV。模型流程测试模拟 `prepare_workspace`，真实子进程由工作区专用测试验证。

缓存生命周期回归覆盖：结束后的状态释放与 ID 去重、等待请求保留锁和 generation、steer 所属任务保护、无新消息时的缓冲过期、停止未确认且关闭失败时强制清理、延迟初始化进程清理、历史和卡片请求失败、日志写失败保留与 24 小时到期丢弃，以及共享分页参数、富文本双 content 去重和整批附件上限。24 小时场景使用可控时钟，无需实际等待；不做容器启动测试。

可靠性回归增加：终态后流挂起、不配合取消的卡片协程、停止确认挂起、线程超时后仍占用名额、5 个同步请求并发、入站控制事件预留容量、业务日志等级和 SDK DEBUG 凭据脱敏。`test_request_limits.py` 同时验证 Codex 与 MCP 的执行器；MCP 自身的 `test_runtime.py` 另验证上传超时后的文件句柄生命周期。全部使用模拟请求，不调用真实飞书服务或模型。`test_daily_logs.py` 现仅保留一句说明：原 watchdog JSONL 运行日志已移除，由 Codex rollouts 会话文件覆盖。

控制会话、reaction 和附件校验修复的回归分别位于 `test_sdk_session.py`、`test_session_cache.py`、`test_resource_validator.py`，并复用原有停止、steer、附件组装与下载测试。控制会话测试验证关闭未确认时不写绑定、取消不被吞掉及后台只重试清理；reaction 测试验证卡片结束前不删除、交付失败后也删除、失败记录跨任务释放保留及无新输入时后台重试。Office 样例在内存中构造真实 ZIP 包及必要元数据，覆盖伪 ZIP、缺失部件、类型不匹配、元数据大小和实体声明；文本覆盖保留扩展名、二进制伪装和 JSON/JSONL 区分。DOCX、XLSX、PPTX 的主部件类型均优先采用 `Override`，不存在时按部件扩展名匹配 `Default`，兼容 WPS 的默认类型声明。回归覆盖 UTF-8 BOM、根相对主部件路径、扩展名大小写、Override 优先级，以及缺失、错误和重复声明的拒绝；不依赖用户原始文件。

Codex 拆分后的回归直接使用 `codex_execution.Codex`；SDK 客户端在 `codex_runtime` 中 mock，会话元数据同步在 `session_codex` 中 mock，执行与用量依赖在 `codex_execution` 中 mock，线程绑定在 `thread_manager` 中 mock。

网关拆分测试通过 `main.create_gateway()` 创建独立实例，mock 指向 runtime、execution、commands 或 router 的实际依赖，不再替换旧 `gateway` 模块的全局变量。共享缓存、停止与撤回、steer、卡片交付、历史恢复和过期清理沿用原回归场景；启动测试验证组件隔离、事件注册和后台任务退出。

## 历史验证记录

以下为当时的本地/容器验证快照，**不代表当前版本或当前部署的验证结果**；当前行为以本文件上述章节及仓库根 README 为准。

2026-09-29 线程绑定修复验证：专项 16 项通过；使用已有检查镜像、禁网且只读挂载本轮源码，Linux ARM64 全量 400 项通过；本地反向顺序 400 项通过。原并发绑定及绑定/历史混合初始化各在独立临时数据库上重复 100 次，共 200 次通过。容器使用 SQLite 3.40.1、aiosqlite 0.22.1。未重新构建发布镜像，未推送或运行远端 PR CI，也未操作生产数据库。

2026-09-29 历史验证记录（watchdog 与历史会话修复）：先用固定单调时钟复现原 watchdog 断言失败，再修复测试时钟；全量复核发现并修复历史会话 WAL 初始化竞争。修复后的 Linux ARM64 容器全量 393 项通过，原并发注册用例在每次独立临时数据库上重复 100 次全部通过。GitHub 远端新提交尚未运行，这些结果仅代表本轮本地与容器验证，不代表生产部署已经完成。

单文件初始化改动的代码验证：`test_office_workspace.py` 12 项、`test_workspace.py` 7 项、`test_model_routing.py` 2 项通过；语法解析和 `git diff --check` 通过。本次后续仅执行代码检查，Docker 构建及部署验证由用户自行完成。

2026-09-21 初轮 Office 集成验证（当时使用锁定依赖）：Linux arm64 镜像构建成功，缓存重建命中系统工具和主服务依赖层。非 root 容器完成两个用户环境初始化/复用、DOCX/PPTX 校验和渲染、XLSX 公式缓存值检查、PDF 提取/渲染及中英文 OCR；本地 Codex 线程 shell 命令确认 Python 使用用户 `.venv`。补齐兄弟项目测试夹具后，全量 Linux 回归为 375/376 通过；唯一失败是原有 `HistoryTests.test_idempotent_registration_and_same_second_sorting` 的 SQLite WAL 锁竞争。修改前版本对同一用例重复 30 次出现 2 次相同失败，故未在本次 Office 改动中修改数据库逻辑。未执行 Linux amd64 或真实模型调用验证；文档样例不代表所有复杂 Office 格式都已覆盖。

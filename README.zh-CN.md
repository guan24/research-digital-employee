# 长时运行的研究数字员工

基于 **Deep Agents + LangGraph + FastAPI + SQLite** 的本地长时自主研究系统。用户提交研究目标后，研究 Agent 自主规划任务、选择工具、检索和阅读资料、登记证据、保存中间成果，并生成有来源依据的 Markdown 报告。独立 Worker 在后台执行任务，关闭浏览器不会中断正在运行的研究。

当前版本是聚焦可靠执行与研究可追溯性的 MVP：默认只注册 `research` 一个岗位，同一时刻只允许一个 Worker 执行任务。

## 已实现能力

- **自主研究与动态规划**：Agent 自主生成整体研究计划，并根据新证据进行实质性重规划；策略版本与步骤进度分别持久化，不把普通进度更新误记为新计划版本。
- **研究工具**：Tavily 网络搜索、公开网页和 PDF 读取、只读 GitHub MCP 仓库检索、arXiv Atom API 检索及论文读取。
- **中间成果与证据**：任务级 Markdown、文本、JSON、CSV、YAML 文件；版本化研究记忆；来源快照；来源与 Finding 登记簿。
- **证据驱动研究控制**：Research Controller 管理研究维度、Finding—来源关联、证据覆盖率和停滞状态，并返回受限的下一步行动建议。
- **报告质量门禁**：ReportValidator 检查报告结构与来源引用；ResearchEvaluator 将规则评分与独立上下文的 LLM 评审结合，从五个维度评价报告。报告不合格时进行有限修复；无法自动解决的问题可转交人工处理。
- **长任务可靠性**：SQLite 任务状态和事件日志、LangGraph SQLite Checkpoint、Worker 租约与心跳、错误分类重试、累计资源预算和过期任务核验恢复。
- **保守恢复**：工具调用具有持久化生命周期记录。Worker 中断后，只读请求允许重新执行；已知写操作通过产物、Finding、计划或进度事件核验。无法判断的恢复冲突及损坏的 Checkpoint 会进入人工处理。
- **执行过程可观测**：模型/工具/系统事件时间线、SSE 实时推送与断线回放、计划历史、研究状态、预算、证据、记忆和成果查看。

## 系统架构

| 层次 | 职责 |
|---|---|
| FastAPI 操作接口 | 任务创建与控制、岗位发现、事件/成果/研究状态查询、人工决策和预算扩展 |
| Research Agent | 自主计划与工具调用、证据收集、分析、研究记忆和报告撰写 |
| 公共任务工具 | 任务级成果读写、持久化记忆、预算查询与扩展申请 |
| 研究专属工具 | 网络搜索、网页/PDF、GitHub MCP 和 arXiv |
| 运行时 | 单 Worker、租约/心跳、重试、LangGraph Checkpoint、回调、预算闸门和恢复 |
| 持久化 | SQLite 任务/事件/控制记录、SQLite 图状态、任务工作区 |

任务数据库、LangGraph Checkpoint 和任务工作区职责不同：任务数据库负责任务状态、所有权、预算和审计事件；Checkpoint 保存 Agent 图状态；工作区保存计划、记忆、来源快照和成果。恢复时会核对这些数据源，而不是简单用其中一个覆盖其他状态。

## 环境要求

- Python 3.12。
- OpenAI 或 OpenAI 兼容的 Chat Completions 模型接口。
- Tavily API Key（网络搜索需要）。
- GitHub 为可选能力，需要只读权限的 Fine-grained PAT；arXiv 不需要 API Key。
- 支持 Windows、macOS 和 Linux。

## 本地启动

在项目目录打开两个终端，创建环境、安装依赖并复制配置文件：

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
~~~

macOS/Linux 可使用 `python3.12 -m venv .venv`、`source .venv/bin/activate` 和 `cp .env.example .env`。

在项目根目录的 `.env` 中配置：

- **OPENAI_API_KEY**、**OPENAI_MODEL**。
- **TAVILY_API_KEY**（网络搜索需要）。
- 使用兼容网关时配置 **OPENAI_BASE_URL**。

如需启用 GitHub 工具，再配置 **GITHUB_PERSONAL_ACCESS_TOKEN**。默认使用 GitHub 官方托管的只读 MCP 远程端点；连接方式、远程地址、超时、重启次数和最大文件大小均在同一个 `.env` 中配置，也可选择本地 stdio MCP 服务。真实凭据不得提交到 Git。

终端 1：启动 API：

~~~powershell
uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000
~~~

终端 2：启动 Worker：

~~~powershell
.\.venv\Scripts\Activate.ps1
python -m app.runtime.worker
~~~

浏览器访问 <http://127.0.0.1:8000/>，通过控制台创建研究任务。Worker 与浏览器会话相互独立。

## 研究执行流程

1. Agent 理解研究目标并生成结构化研究计划。
2. 建立研究覆盖矩阵，明确研究维度、问题、预期证据、优先级和最低来源数量。
3. Agent 自主搜索、阅读资料，将来源快照登记到证据库，使用来源 ID 记录 Finding，并保存长期研究记忆。
4. Research Controller 在观测到的研究阶段边界，以及 Agent 主动查询研究状态时，评估维度覆盖率和停滞情况，返回 `FILL_EVIDENCE_GAP`、`REPLAN`、`ENTER_CONVERGENCE`、`REQUEST_HUMAN` 等建议。大部分研究动作仍由 Agent 执行；阶段边界要求人工决策时，运行时会自动暂停任务并创建待办。
5. Agent 撰写报告。ReportValidator 检查 Markdown 结构、URL、已登记来源 ID 和段落级引用覆盖率。
6. ResearchEvaluator 先执行规则评估；没有硬阻断时，再调用独立上下文的 LLM 评审。默认评价目标覆盖度、证据充分性、来源可靠性、结论一致性和报告完整性五个维度。默认加权总分门槛为 80 分，单维度门槛为 60 分。
7. Executor 对最终成果进行持久化和读回核验。研究岗位最多允许三轮交付修订；仍无法解决的质量问题会保留现场，并可能转人工决策。

## 任务资源预算

研究岗位默认预算为 30 次工具调用、3600 秒和 100000 Token。创建任务时可显式覆盖岗位默认值。

- 工具调用次数在工具开始前通过 SQLite 原子计数。
- 累计运行时间在模型/工具/回调控制边界检查，跨重试累计；暂停期间不计入已用运行时间。
- 主 Agent 每次模型调用前，系统预留启发式输入 Token 估算值、4096 Token 输出上限和 256 Token 固定开销；模型也配置为最多输出 4096 Token。结算优先使用 Provider 返回的实际 usage。
- 收敛阶段预留 Token 总额的 30%、工具调用次数的 25% 和运行时间的 20%（同时受最小/最大预留规则约束）。任一探索额度触及收敛边界后，系统禁止继续搜索和读取新来源，将剩余额度用于综合、持久化、校验和修复。
- Agent 可申请预算扩展，必须由操作人员批准。批准后增加对应额度；因硬预算闸门失败的任务可复用原 Checkpoint 和工作区重新入队。

Token 估算属于启发式估算，并非 Provider 精确计量；当前控制台尚未提供美元成本展示。

## API

- 任务与岗位：`POST /tasks`、`GET /tasks`、`GET /tasks/{task_id}`、`GET /agents`
- 事件：`GET /tasks/{task_id}/events`、`GET /tasks/{task_id}/events/stream`
- 任务控制：`POST /tasks/{task_id}/pause`、`resume`、`retry`、`cancel`；`DELETE /tasks/{task_id}`
- 成果：`GET /tasks/{task_id}/artifacts`、`GET /tasks/{task_id}/artifacts/{path}`
- 研究状态：`GET /tasks/{task_id}/research`、`GET /tasks/{task_id}/evidence`、`GET /tasks/{task_id}/memory`
- 预算：`GET /tasks/{task_id}/budget`、`POST /tasks/{task_id}/budget/approve`
- 人工干预：`POST /tasks/{task_id}/human-intervention/resolve`
- 运行状态：`GET /health`、`GET /system/overview`

SSE 支持 `Last-Event-ID` 或 `after_id`。事件先写入 SQLite，再按单调递增的 event_id 推送；断线后可从指定事件 ID 继续回放。

## 数据存储

- `data/tasks.sqlite`：任务状态、Worker 所有权、预算、事件日志、研究控制状态、质量评估、工具调用记录、恢复决策和人工干预。
- `data/checkpoints.sqlite`：以 task_id 为 thread_id 的 LangGraph Checkpoint。
- `workspaces/{task_id}/`：研究计划、记忆、来源快照、证据登记簿、中间成果和最终报告。

任务数据保留到用户主动删除。备份时应同时备份两个 SQLite 数据库和工作区目录。首次增量迁移会将旧版 runtime generation 1 的活动任务归档；旧活动任务不会被当前 Worker 自动恢复。

## 测试

运行离线测试：

~~~powershell
python -m pytest -v
node --check app/static/app.js
~~~

GitHub Actions 使用 Python 3.12 执行 pytest，并使用 Node 22 检查前端 JavaScript。测试依赖本地 SQLite 和测试替身，不证明真实模型、Tavily、GitHub 服务的端到端研究效果。真实冒烟测试需要配置相应凭据。

## 当前边界与已知限制

- 单机单 Worker。全局 Worker 租约阻止多个 Worker 同时运行；多主机调度和多岗位并行执行尚未实现。
- 默认只注册 Research Agent。注册表支持岗位独立 builder、产物路径、校验器、后处理器、终态收尾函数和预算默认值，但 Executor 仍有研究岗位专属生命周期分支。
- 取消为协作式机制，正在执行的模型或网络请求可能需要运行到下一个控制点。
- Controller 负责计算证据覆盖并给出行动建议，大部分研究动作仍由 Agent 选择和执行；运行时直接强制执行的是预算闸门和人工停车，而不是所有 Controller 建议。
- 来源快照保存 SHA-256 元数据。当前完整性检查验证快照存在性以及快照中的哈希/URL 标记，但不会重新计算来源正文的 SHA-256，也没有实现防篡改的只读存储。
- LLM 评审使用与 Agent 相同的模型配置，但通过独立调用和上下文执行；评审只读取有限的报告和来源摘录，不会完整重读全部来源。
- CI 为离线测试。真实模型研究质量、外部服务稳定性以及真实外部调用期间的崩溃恢复，仍需部署后进行冒烟和故障注入验证。

## GitHub 与 arXiv 数据源

GitHub 工具仅注册给研究岗位。默认使用 GitHub 官方托管只读 MCP 端点 <https://api.githubcopilot.com/mcp/readonly>。适配器采用只读工具白名单，要求先建立目录索引，再将文件读取固定到索引对应的 commit SHA，并保存任务级快照。PAT 应使用仅覆盖目标仓库必要读取权限的 Fine-grained PAT。

arXiv 适配器调用官方 Atom API，设置进程级 3 秒请求间隔和 5 分钟检索缓存。论文 PDF 读取复用公开 PDF 提取链路，并沿用 SSRF 与下载大小检查。

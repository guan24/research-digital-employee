# 长时运行的研究数字员工

基于 **Deep Agents + LangGraph + FastAPI + SQLite** 的本地长时自主研究系统。浏览器控制台是任务提交入口；独立 Worker 在后台执行任务，关闭浏览器不会中断研究。

## 功能

- Agent 自主生成整体研究计划，并根据新证据进行策略性重规划；每个步骤独立展示待执行、进行中、已完成、已跳过、失败等状态，进度变化不产生新的策略版本。规划历史记录触发原因、新信息、原方案问题分析、调整决策和具体变化。
- 任务进入完成、失败或取消等终态时，自动归档最终计划及全部有效版本到 `notes/agent_execution_plan.json`。
- 支持 Tavily 搜索、公开网页读取，以及任务级 Markdown/JSON 中间成果。
- SQLite 持久化任务状态和执行事件；LangGraph SQLite Checkpoint 以 task_id 为 thread_id。
- SSE 实时执行轨迹：事件先持久化，再推送；支持 Last-Event-ID 断线续传和历史回放。前端对新增事件执行局部 DOM 更新，不重建整个时间线，保留展开状态与详情滚动位置。
- 可观测模型/工具过程：模型名称、调用目的、输入摘要、模型完整响应（含工具调用结构）、决策摘要、Token、耗时、工具参数、返回结果、异常和检查点恢复。
- Agent 执行计划：Agent 通过结构化计划工具提交步骤、目标、预期成果和状态；计划更新按版本持久化，工具活动可补充步骤进行中状态，控制台展示最新计划及历史变更。计划内容是可观察的任务步骤，不包含隐藏思维链。
- 成果预览：支持 Markdown 渲染和 JSON 格式化，默认展示渲染视图，可切换原始源码；保存新成果后文件列表和当前预览自动刷新。
- 凭据脱敏：事件写入前自动遮蔽 API Key、Bearer Token、密码、Authorization 和凭据字段；研究正文保留。
- 彻底删除任务：排队、暂停、等待重试及终态任务先阻止 Worker 领取再删除。运行中任务先请求协作式取消，等待 Worker 最多 60 秒；确认停止后删除任务事件、Checkpoint 和工作区。超时则中止删除并保留任务数据。
- 中文任务目标通过 UTF-8 JSON 保存，并有 HTTP 往返测试。

## 环境要求

- 推荐 Python 3.12。
- OpenAI API Key 和 Tavily API Key。
- 支持 Windows、macOS、Linux；Docker 可选。

## Windows 本地启动

在项目目录打开两个 PowerShell 窗口。

终端 1：创建环境并安装依赖：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，配置 `OPENAI_API_KEY` 和 `TAVILY_API_KEY`。

终端 1：启动 API：

```powershell
uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000
```

终端 2：启动单 Worker：

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.runtime.worker
```

浏览器访问 <http://127.0.0.1:8000/>。**通过网页表单创建任务**，中文内容由浏览器以 UTF-8 JSON 发送；无需通过终端编码提交任务目标。

## 控制台使用

1. 在网页创建任务，填写研究目标、步数、运行时限和重试次数。
2. 选择任务查看状态及按时间排序的执行轨迹。
3. 执行事件通过 SSE 推送并持久化。断线重连使用最后事件 ID；历史事件也可通过 API 查询。
4. 执行轨迹按模型调用、工具调用和系统事件分层展示，并将同一次模型/工具调用的开始、完成或失败合并为一张卡片。默认只显示关键摘要、状态、耗时和 Token；模型原始 Prompt/响应默认收起，点击“查看完整请求 / 响应”后查看。工具参数和返回结果也可单独展开。SSE 新事件采用局部更新，不会重置已展开的卡片或详情滚动位置。系统展示模型实际生成的可观察输出，不采集隐藏思维链；凭据在持久化前脱敏。
5. 在执行轨迹上方查看 Agent 的整体执行计划、每个步骤的执行进度及规划演进历史。步骤状态通过独立事件持久化，不会产生策略版本。点击任务后，执行实例详情展示 Worker、心跳、Checkpoint 恢复状态、重试次数、模型/工具调用统计及最近异常。任务进入终态后，最终计划、步骤进度和全部有效版本归档至 `notes/agent_execution_plan.json`。
6. 成果预览支持 Markdown 渲染/源码切换；JSON 仅显示原始文本，不做对象渲染、格式化或语法高亮。Markdown 与 JSON 共用固定宽度的预览容器，长代码或表格只在内容区域内部滚动。
7. 可重试失败任务、暂停、恢复或取消任务。
8. 永久删除任务。运行中任务会显示“正在停止并清理”，API 最多等待 60 秒；Worker 未确认停止则取消删除并保留任务数据。

## API

- `POST /tasks`、`GET /tasks`、`GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`、`GET /tasks/{task_id}/events/stream`
- `POST /tasks/{task_id}/pause`、`POST /tasks/{task_id}/resume`
- `POST /tasks/{task_id}/retry`、`POST /tasks/{task_id}/cancel`
- `DELETE /tasks/{task_id}`
- `GET /tasks/{task_id}/artifacts`、`GET /tasks/{task_id}/artifacts/{path}`
- `GET /system/overview`

SSE 客户端可使用 `Last-Event-ID` 请求头或 `after_id` 参数。事件 ID 单调递增。

## 数据存储

- `data/tasks.sqlite`：任务及持久化事件；删除任务时事件级联删除。
- `data/checkpoints.sqlite`：LangGraph 检查点；删除时清理 thread_id 对应记录。
- `workspaces/<task_id>/`：搜索结果、网页资料、笔记和最终报告。

执行数据会一直保留到用户主动删除任务。备份时应同时备份两个 SQLite 数据库和 `workspaces/`。

## 测试

```powershell
python -m pytest -v
```

测试不需要真实 OpenAI/Tavily 密钥，覆盖任务持久化、中文 UTF-8 往返、事件回放、凭据脱敏、删除状态隔离与级联清理、Worker 生命周期。真实研究任务冒烟测试需要有效 API Key。

## 计划历史与成果预览

Agent 执行计划描述整体研究策略。只有初始规划和基于证据的实质性重规划才产生新版本；普通步骤进度保留在执行轨迹中。每次重规划记录触发原因、新信息、原方案问题分析、调整决策和具体变化。任务进入完成、失败或取消等终态后，最终计划及全部有效版本归档至 `notes/agent_execution_plan.json`。归档保存简明决策依据，不保存隐藏思维链。

Markdown 支持渲染和源码两种模式。JSON 仅展示原始文本，不做 JSON 渲染、重新格式化或语法高亮。两种格式共用固定宽度预览框，长行在预览框内部滚动。

## MVP 边界

当前是单机单 Worker MVP。SQLite 不适用于多主机高并发场景。取消采用协作式机制，正在进行的模型或网络请求可能需要运行到下一个控制点。系统不采集或展示隐藏的模型思维链，只记录模型生成的可观察输出和决策。


## 可靠性与质量控制

- `max_runtime_seconds` 是跨 Worker 重试累计的任务运行预算；Worker 心跳将已运行时间结算到 SQLite，暂停和重试不会重置预算。
- `max_steps` 定义为任务生命周期内允许的工具调用次数，由 SQLite 原子计数保证；LangGraph recursion limit 仅作为图循环的第二道保护。
- Agent 最终报告由独立 ReportValidator 检查 Markdown 标题、最低内容长度、来源章节和可核验 URL；失败时最多反馈 Agent 修订两轮，仍不合格则任务失败并保留诊断事件。
- 支持公开 PDF 文本提取，扫描版 PDF 需要 OCR 时会明确报告无法提取，不会伪造正文。
- Worker 在已有租约时等待租约释放或过期，再尝试接管。
- Agent 通过 `agent_type` 注册表构建；当前默认并内置 `research`，新岗位提供独立 builder、提示词与工具集后可注册。


## 长任务能力增强

- **岗位注册表**：通过 `app/agents/registry.py` 注册岗位 Agent，定义岗位提示词、产物路径和质量门禁；API `GET /agents` 返回可用岗位。
- **上下文压缩与长期记忆**：Deep Agents 的 `SummarizationMiddleware` 在上下文约 78% 时自动摘要并保留近期消息；LangGraph SQLite Checkpoint 负责运行状态恢复，任务工作区的 `notes/research_memory.md` 保存跨重启研究摘要。
- **证据溯源**：成功读取的网页/PDF 自动写入 `evidence/registry.json` 和不可变内容快照，来源分配 `[S0001]` ID；Agent 可用 `record_finding` 将发现关联来源。报告质量门禁检查 URL、章节、来源 ID、快照和段落级引用覆盖率。
- **任务预算强制执行**：工具调用步数与累计运行时长是硬闸门，分别在工具调用前及模型/工具/控制回调边界检查；超时终止为终态失败，不进入自动重试。每次模型调用前预留 UTF-8 输入字节数启发式估算 + 2048 Token 输出上限 + 128 Token 固定开销，ChatOpenAI 同时设置 2048 Token 输出上限；成功后按 Provider usage 结算，异常时释放预留，重复回调幂等。预留不足时不发起模型请求，任务以 `TOKEN_BUDGET_EXHAUSTED` 终态失败。达到 80% 告警、95% 禁止搜索/抓取，但产物/证据/记忆工具仍可用。人工扩额叠加额度并从 Checkpoint 恢复。API：`GET /tasks/{task_id}/budget`、`POST /tasks/{task_id}/budget/approve`。
- **可观测性**：`GET /tasks/{task_id}/evidence` 与 `GET /tasks/{task_id}/memory` 提供证据和记忆查看，控制台展示预算、证据数量和最新记忆。
- **兼容性**：SQLite 使用增量迁移；既有任务、事件、Checkpoint 和工作区无需重建。

Token 预留采用保守的 UTF-8 字节数启发式估算，包含 2048 Token 输出上限和 128 Token 固定开销；结算以 Provider 返回的 usage metadata 为准，Provider 未提供 usage 时按 0 结算并释放剩余预留。研究岗位默认步数/运行时长/Token 配额唯一声明于 `app/agents/research/profile.toml`，API 创建任务直接使用该配置，显式任务参数优先。当前尚未接入金额成本界面。


## 岗位契约与错误策略

岗位注册可选声明产物路径（默认 `result.md`）、校验器、受控后处理/修订次数、终态收尾函数和默认预算。公共 Executor 不再固定研究报告文件名或研究校验器；研究岗位保留 `report.md`、原证据覆盖率判据、最多两次定向修订及终态计划归档。

错误分为可重试、终态、控制信号和未知类。鉴权/权限/非法请求/交付校验耗尽直接失败，不消耗自动重试；网络与上游临时错误按退避策略重试；未知错误标记为 `UNKNOWN_EXCEPTION` 并默认可重试。任务保存最新错误、首个根因及完整错误历史。


## 研究数据源：GitHub MCP 与 arXiv

研究岗位新增六个专属工具：github_repo、github_tree、github_read、github_releases、arxiv_search 和 arxiv_paper。GitHub MCP 采用只读模式：Worker 首次调用时懒启动官方 github/github-mcp-server，同一 Worker 生命周期复用；本地 stdio 启动参数同时包含 --read-only 和精确的 --tools 白名单，写操作工具不会注册。未配置 PAT 或 MCP 服务不可用时，工具返回结构化配置错误，研究任务仍可继续使用网页和 PDF 工具。可选远程模式使用 X-MCP-Tools 请求头。

本地模式需要安装官方 github-mcp-server 并加入 PATH，配置 GITHUB_PERSONAL_ACCESS_TOKEN；Docker 或没有本地二进制的环境可设置 GITHUB_MCP_TRANSPORT=remote，使用 https://api.githubcopilot.com/mcp/。PAT 不得提交到仓库。一次 github_read 最多读取 5 个文件，只计一个 Agent 工具步数；适配器要求先通过 github_tree 为每个文件父目录建立大小索引，并使用索引返回的 commit SHA 固定读取版本；在任何文件读取请求前完成路径与文件大小检查。文本快照保存至 sources/github/{commit_sha}/，登记 github_file 类型证据并沿用 [Sxxxx] 编号。未建立索引的路径和超限文件会在发起 MCP 请求前被拒绝。二进制、密钥、危险路径和超过硬大小限制的文件会被拒绝；超过快照阈值但未超过硬限制的文本会截断并明确标记。

arxiv_search 和 arxiv_paper 使用 Python 标准库调用 arXiv 官方 Atom API，全局请求间隔至少 3 秒，相同检索式缓存 5 分钟。检索结果和论文元数据登记为 arxiv 类型证据；全文读取复用现有 SSRF 校验、大小限制和 PDF 文本提取流程。不新增 Python 解析依赖。

新增事件包括 github_repo_lookup_started、github_repo_lookup_completed、github_tree_index_started、github_tree_indexed、github_files_read_started、github_file_read_completed、github_files_read_completed、github_releases_listed、github_repo_failed、github_tree_failed、github_files_read_failed、github_releases_failed、arxiv_search_completed、arxiv_search_failed、arxiv_paper_read_completed 和 arxiv_paper_failed。失败事件包含 error_type、error_code、retryable；限流时还包含 retry_after_seconds。工具层错误同步写入任务错误历史，但不会自动终止整个研究任务。

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

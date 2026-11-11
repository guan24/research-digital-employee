# 研究型数字员工 MVP

一个最小化的、可长期运行的自主研究员工，基于 **Deep Agents + LangGraph + FastAPI + SQLite** 构建。系统使用一个研究 Agent 和一个 Worker 进程。Agent 会自主规划、搜索、阅读网页、保存证据、修订计划，并最终撰写一份 Markdown 报告。

## 1. 功能特性

- 由 Deep Agents 驱动的自主研究循环（而非固定的搜索/阅读/总结脚本）。
- 外部工具：Tavily 网页搜索、公开网页抓取/阅读、任务作用域内的作品读取/写入/列表。
- FastAPI 任务 API：创建、查看、列出事件、暂停、恢复、手动重试失败任务、取消、列出/下载作品。
- SQLite 任务生命周期管理、事件日志、单 Worker 租约、心跳、重试调度。
- 以 `task_id` 为键的 LangGraph SQLite 检查点；过期的 `RUNNING` 任务会被重新排队，并从最新的图检查点恢复。
- 在 `workspaces/<task_id>/` 下以原子方式写入作品。
- 运行时限制、递归限制、协作式暂停/取消、有界重试，以及结构化的模型/工具事件。
- 源码中不提交任何 API 密钥。

## 2. 环境要求

- 推荐使用 Python **3.12**（包版本范围支持 Python 3.12 与 3.13）。
- 具备访问所配置聊天模型权限的 OpenAI API 密钥。
- 用于网页搜索的 Tavily API 密钥：<https://tavily.com/>。
- Docker Desktop 为可选；本地虚拟环境即可满足需求。

> OpenAI 模型 API 与搜索 API 是相互独立的服务。`OPENAI_API_KEY` 不提供网页搜索能力；本 MVP 使用 `TAVILY_API_KEY` 进行搜索。

## 3. 本地配置

```bash
# 1) 创建并激活虚拟环境
python -m venv .venv
# Windows PowerShell
.venv\\Scripts\\Activate.ps1
# macOS / Linux / WSL2
source .venv/bin/activate

# 2) 安装依赖
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3) 配置密钥与模型/搜索设置
cp .env.example .env
# 编辑 .env，设置 OPENAI_API_KEY 和 TAVILY_API_KEY

# 4) 启动 API（终端 1）
uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000

# 5) 启动单个 Worker（终端 2）
python -m app.runtime.worker
```

研究控制台：<http://127.0.0.1:8000/>  
Swagger UI：<http://127.0.0.1:8000/docs>

内置控制台每 3 秒轮询一次 API，支持创建任务、任务队列与状态筛选、暂停/恢复/取消/重试、执行事件追踪、Worker 租约概览、工作区文件浏览及 Markdown/文本预览。前端使用原生 HTML/CSS/JavaScript，由 FastAPI 从 `app/static/` 直接托管，无需单独安装 Node 或执行前端构建。

API 与 Worker 是刻意分离的两个进程。HTTP 请求只会创建一个任务并返回 `task_id`；Worker 独立执行该任务。

## 4. 提交研究任务

```bash
curl -X POST http://127.0.0.1:8000/tasks \\
  -H "Content-Type: application/json" \\
  -d '{
    "goal": "调研当前主流 Coding Agent 的技术架构，比较任务规划、工具使用、上下文管理、执行隔离和长任务恢复机制，并形成有依据的 Markdown 研究报告。优先使用官方文档、GitHub 仓库和发布记录。",
    "max_steps": 12,
    "max_runtime_seconds": 3600,
    "max_attempts": 3
  }'
```

示例响应：

```json
{
  "task_id": "<uuid>",
  "goal": "...",
  "status": "queued",
  "attempts": 0
}
```

轮询任务状态：

```bash
curl http://127.0.0.1:8000/tasks/<task_id>
```

查看执行轨迹：

```bash
curl http://127.0.0.1:8000/tasks/<task_id>/events
```

列出已保存的文件：

```bash
curl http://127.0.0.1:8000/tasks/<task_id>/artifacts
```

读取最终报告：

```bash
curl http://127.0.0.1:8000/tasks/<task_id>/artifacts/report.md
```

报告同样存储在本地 `workspaces/<task_id>/report.md`。中间文件也存放在同一个任务工作区中，包括搜索结果 JSON 和抓取到的页面 Markdown。

## 5. 任务控制

```bash
# 在下一个模型/工具边界处协作式暂停
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/pause

# 从其 LangGraph 检查点恢复一个已暂停的任务
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/resume

# 手动重试失败任务（保留 task_id、检查点线程和工作区）
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/retry \
  -H "Content-Type: application/json" \
  -d '{"reason":"OpenAI API 额度已恢复"}'

# 协作式取消
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/cancel
```

手动重试接口仅接受 `failed` 状态的任务，成功后返回 **202**。接口会原子地将任务重新放入队列，重置尝试次数并清除终态错误，同时保留任务工作区，并使用原 `task_id` 对应的 LangGraph 检查点继续执行。如果不存在检查点，Executor 会从原始目标开始执行。其他状态返回 **409**，不存在的任务返回 **404**。`task_manually_requeued` 事件记录重试原因和此前的尝试次数。

暂停/取消是协作式的：在到达下一个控制点之前，正在进行的 HTTP 请求或模型生成可能会先执行完毕。

## 6. Docker Compose

```bash
cp .env.example .env
# 编辑 .env：设置 OPENAI_API_KEY 和 TAVILY_API_KEY

docker compose up --build
```

- API：<http://localhost:8000/docs>
- API 与 Worker 共享 `./data` 和 `./workspaces` 卷。
- Worker 使用 SQLite 租约，因此如果意外启动了第二个 Worker，也不会创建第二个活动的执行器。

## 7. 常用环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 空 | 必需的模型凭据 |
| `OPENAI_BASE_URL` | 空 | 可选的 OpenAI 兼容端点 |
| `OPENAI_MODEL` | `gpt-4.1-mini` | 聊天模型 |
| `TAVILY_API_KEY` | 空 | 必需的搜索凭据 |
| `DATABASE_PATH` | `./data/tasks.sqlite` | 任务/事件数据库 |
| `CHECKPOINT_PATH` | `./data/checkpoints.sqlite` | LangGraph 检查点数据库 |
| `WORKSPACE_DIR` | `./workspaces` | 任务作品 |
| `DEFAULT_MAX_STEPS` | `12` | 默认图递归预算（转换为 LangGraph 递归限制） |
| `DEFAULT_MAX_RUNTIME_SECONDS` | `3600` | 单次尝试的运行时预算 |
| `DEFAULT_MAX_ATTEMPTS` | `3` | Worker 层级的尝试次数限制 |
| `WORKER_LEASE_SECONDS` | `45` | Worker 单例租约及过期任务阈值 |

## 8. 测试

单元测试不会调用 OpenAI 或 Tavily，也不要求安装 Agent SDK；它们使用 fake 对象验证 SQLite 任务生命周期、租约/恢复行为、作品隔离以及检查点恢复决策。

```bash
pytest
```

实时冒烟测试需要两个 API 密钥：

1. 启动 API 和 Worker。
2. 提交上述 Coding Agent 研究任务。
3. 检查 `plan`、`sources` 和 `report.md` 是否出现在任务工作区下。
4. 在任务运行时终止 Worker；等待租约过期后重新启动 Worker。
5. 检查事件中是否出现 `worker_recovery_scheduled` 和 `checkpoint_resume`（前提是图在之前已经持久化了某个 pending 节点）。

## 9. 重要的 MVP 边界

- 单机、一个活动 Worker、每个任务一个研究 Agent。
- SQLite 适用于本地验证和单 Worker 的小型负载，而非高并发生产环境。
- 搜索依赖 Tavily。页面提取使用 BeautifulSoup 并存储文本/Markdown，而非完整的浏览器渲染器。
- Agent 具备任务作用域内的虚拟文件系统状态，以及显式的持久化作品工具。它没有 shell 执行工具。
- 恢复后可以重放失败的进行中工具。搜索/抓取为只读操作；作品写入具有原子性且可安全覆盖。在添加动作型工具之前，外部副作用应使用幂等键。
- 不要将这个本地 MVP 直接暴露给不受信任的用户。在共享部署之前，请添加认证、按用户隔离工作区、更严格的出站网络策略以及沙箱机制。

参见 [DESIGN.md](DESIGN.md) 了解架构与取舍。
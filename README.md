# Research Digital Employee MVP

A minimal, long-running autonomous research employee built with **Deep Agents + LangGraph + FastAPI + SQLite**. It uses one Research Agent and one Worker process. The Agent autonomously plans, searches, reads pages, saves evidence, revises its plan, and writes a Markdown report.

## 1. Features

- Autonomous research loop powered by Deep Agents (not a fixed search/read/summarize script).
- External tools: Tavily web search, public webpage fetch/read, task-scoped artifact read/write/list.
- FastAPI task API: create, inspect, list events, pause, resume, manually retry failed tasks, cancel, list/download artifacts.
- SQLite task lifecycle, event log, single-worker lease, heartbeat, retry scheduling.
- LangGraph SQLite checkpointing keyed by `task_id`; stale `RUNNING` tasks are re-queued and resumed from the latest graph checkpoint.
- Atomic artifact writes under `workspaces/<task_id>/`.
- Runtime limits, recursion limits, cooperative pause/cancel, bounded retries, and structured model/tool events.
- No API keys committed to source code.

## 2. Requirements

- Python **3.12** recommended (the package range supports Python 3.12 and 3.13).
- OpenAI API key with access to the configured chat model.
- Tavily API key for web search: <https://tavily.com/>.
- Docker Desktop is optional; local virtualenv is sufficient.

> The OpenAI model API and search API are separate services. `OPENAI_API_KEY` does not provide web search; this MVP uses `TAVILY_API_KEY` for search.

## 3. Local setup

```bash
# 1) Create and activate a virtual environment
python -m venv .venv
# Windows PowerShell
.venv\\Scripts\\Activate.ps1
# macOS / Linux / WSL2
source .venv/bin/activate

# 2) Install dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3) Configure secrets and model/search settings
cp .env.example .env
# Edit .env and set OPENAI_API_KEY and TAVILY_API_KEY

# 4) Start the API (terminal 1)
uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000

# 5) Start the single Worker (terminal 2)
python -m app.runtime.worker
```

Swagger UI: <http://127.0.0.1:8000/docs>

The API and Worker are intentionally separate processes. An HTTP request only creates a task and returns a `task_id`; the Worker executes it independently.

## 4. Submit a research task

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

Example response:

```json
{
  "task_id": "<uuid>",
  "goal": "...",
  "status": "queued",
  "attempts": 0
}
```

Poll task status:

```bash
curl http://127.0.0.1:8000/tasks/<task_id>
```

Inspect execution trace:

```bash
curl http://127.0.0.1:8000/tasks/<task_id>/events
```

List saved files:

```bash
curl http://127.0.0.1:8000/tasks/<task_id>/artifacts
```

Read the final report:

```bash
curl http://127.0.0.1:8000/tasks/<task_id>/artifacts/report.md
```

The report is also stored locally at `workspaces/<task_id>/report.md`. Intermediate files are stored in the same task workspace, including search result JSON and fetched page Markdown.

## 5. Task control

```bash
# Cooperative pause at the next model/tool boundary
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/pause

# Resume a paused task from its LangGraph checkpoint
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/resume

# Manually retry a failed task (preserves task_id, checkpoint thread, and workspace)
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/retry \
  -H "Content-Type: application/json" \
  -d '{"reason":"OpenAI API quota replenished"}'

# Cooperative cancellation
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/cancel
```

The manual retry endpoint accepts only tasks in `failed` state and returns **202** after atomically re-queueing the task. It resets the attempt counter and terminal error, preserves the task workspace, and reuses the LangGraph checkpoint under the original `task_id` when available. If no checkpoint exists, the executor starts from the original goal. Other states return **409**; unknown task IDs return **404**. The `task_manually_requeued` event records the reason and previous attempt count.

Pause/cancel are cooperative: an in-flight HTTP request or model generation may finish before the next control boundary is reached.

## 6. Docker Compose

```bash
cp .env.example .env
# Edit .env: OPENAI_API_KEY and TAVILY_API_KEY

docker compose up --build
```

- API: <http://localhost:8000/docs>
- API and Worker share `./data` and `./workspaces` volumes.
- The Worker uses a SQLite lease so accidentally starting a second Worker does not create a second active executor.

## 7. Useful environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | empty | Required model credential |
| `OPENAI_BASE_URL` | empty | Optional OpenAI-compatible endpoint |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Chat model |
| `TAVILY_API_KEY` | empty | Required search credential |
| `DATABASE_PATH` | `./data/tasks.sqlite` | Task/event database |
| `CHECKPOINT_PATH` | `./data/checkpoints.sqlite` | LangGraph checkpoint database |
| `WORKSPACE_DIR` | `./workspaces` | Task artifacts |
| `DEFAULT_MAX_STEPS` | `12` | Default graph recursion budget (converted to LangGraph recursion limit) |
| `DEFAULT_MAX_RUNTIME_SECONDS` | `3600` | Per-attempt runtime budget |
| `DEFAULT_MAX_ATTEMPTS` | `3` | Worker-level attempt limit |
| `WORKER_LEASE_SECONDS` | `45` | Worker singleton lease and stale-task threshold |

## 8. Tests

The unit tests do not call OpenAI or Tavily and do not require the Agent SDK to be installed; they validate SQLite task lifecycle, lease/recovery behavior, artifact isolation, and checkpoint-resume decisions with fakes.

```bash
pytest
```

A live smoke test requires both API keys:

1. Start API and Worker.
2. Submit the Coding Agent research task above.
3. Check that `plan`, `sources`, and `report.md` appear under the task workspace.
4. Terminate the Worker while a task is running; wait for the lease to expire; restart the Worker.
5. Inspect events for `worker_recovery_scheduled` and `checkpoint_resume` (if the graph had already persisted a pending node).

## 9. Important MVP boundaries

- Single machine, one active Worker, one Research Agent per task.
- SQLite is appropriate for local validation and a small single-worker workload, not high-concurrency production.
- Search requires Tavily. Page extraction uses BeautifulSoup and stores text/Markdown, not a full browser renderer.
- The Agent has task-scoped virtual filesystem state plus explicit persistent artifact tools. It has no shell execution tool.
- A failed in-flight tool can be replayed after recovery. Search/fetch are read-only; artifact writes are atomic and overwrite-safe. External side effects should use idempotency keys before adding action-taking tools.
- Do not expose this local MVP directly to untrusted users. Add authentication, per-user workspace isolation, stricter outbound network policy, and sandboxing before a shared deployment.

See [DESIGN.md](DESIGN.md) for architecture and trade-offs.

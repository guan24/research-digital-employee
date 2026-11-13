# Research Digital Employee

A single-machine, long-running autonomous research worker built with **Deep Agents, LangGraph, FastAPI and SQLite**. A user submits a research goal; the research Agent plans the work, chooses tools, reads sources, records evidence, saves intermediate artifacts and produces a Markdown report. A separate Worker executes tasks, so closing the browser does not stop a running task.

This repository is an MVP focused on durable execution and research traceability. It currently registers one role (`research`) and runs one active Worker at a time.

## What is implemented

- **Autonomous research loop:** the Agent creates and materially revises its own research plan. It is not a fixed search/read/write pipeline. Plan strategy versions and per-step progress are persisted separately.
- **Research tools:** Tavily web search; public HTML and PDF retrieval; read-only GitHub MCP repository inspection; arXiv Atom API search and paper retrieval.
- **Durable work products:** task-scoped Markdown, text, JSON, CSV and YAML artifacts; versioned research memory; source snapshots; a source/Finding registry.
- **Evidence-aware control:** a deterministic Research Controller tracks requested dimensions, Finding-to-source links, coverage and stagnation, then returns bounded next-action recommendations.
- **Delivery quality gate:** a deterministic ReportValidator checks report structure and provenance. A ResearchEvaluator combines rule-based scores with an isolated LLM review across five dimensions. Failed reports receive bounded repair attempts; unresolved review or quality issues can be sent to a human operator.
- **Long-running execution:** SQLite task state and event journal, LangGraph SQLite checkpoints, Worker lease/heartbeat, retry classification, cumulative budgets and stale-task reconciliation.
- **Conservative recovery:** tool calls are journaled. After a Worker interruption, read-only calls may be retried; known writes are checked against persisted artifacts, Findings, plans or progress events. Ambiguous recovery and corrupted checkpoints are parked for human review.
- **Operational visibility:** chronological model/tool/system events, SSE streaming with event replay, plan history, research state, budget, evidence, memory and artifact inspection.

## Architecture

| Layer | Responsibility |
|---|---|
| FastAPI operator API | Task creation and controls, role discovery, event/artifact/research inspection, human decisions and budget extensions |
| Research Agent | Open-ended plan/tool loop, evidence gathering, analysis, memory and report writing |
| Shared task tools | Task-scoped artifact read/write, durable memory, budget status and extension requests |
| Research tools | Search, webpage/PDF retrieval, GitHub MCP and arXiv |
| Runtime | Single Worker, lease/heartbeat, retries, LangGraph checkpoint, execution callbacks, budget gates and recovery |
| Persistence | SQLite task/event/control records, SQLite graph checkpoints and per-task workspace |

The task database, LangGraph checkpoint and workspace have different responsibilities. The task database owns task status, ownership, budgets and audit events; the checkpoint stores graph state; the workspace stores files and evidence. Recovery reconciles these sources rather than treating one as a complete replacement for the others.

## Requirements

- Python 3.12.
- An OpenAI or OpenAI-compatible chat-completions endpoint.
- A Tavily API key for web search.
- GitHub access is optional. GitHub tools require a read-only fine-grained PAT. arXiv retrieval does not require a key.
- Windows, macOS or Linux.

## Local setup

Create a virtual environment, install dependencies and copy the example configuration:

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
~~~

On macOS/Linux, use `python3.12 -m venv .venv`, `source .venv/bin/activate` and `cp .env.example .env`.

Set the required values in the project-root `.env`:

- **OPENAI_API_KEY** and **OPENAI_MODEL**.
- **TAVILY_API_KEY** for web search.
- **OPENAI_BASE_URL** when using a compatible gateway rather than the default OpenAI endpoint.

To enable GitHub tools, also set **GITHUB_PERSONAL_ACCESS_TOKEN**. The default transport is the official hosted read-only MCP endpoint. Transport, endpoint, timeout, restart limit and maximum file size are configurable in the same `.env`. A local stdio GitHub MCP server is an optional alternative. Never commit real credentials.

Start the API and Worker in separate terminals:

~~~powershell
uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000
~~~

~~~powershell
.\.venv\Scripts\Activate.ps1
python -m app.runtime.worker
~~~

Open <http://127.0.0.1:8000/> and submit tasks through the browser console. The Worker runs independently of the browser session.

## Research workflow

1. The Agent identifies the deliverable and creates a structured research plan.
2. It defines a coverage matrix (research dimensions, questions, expected evidence, priority and minimum source count).
3. It searches and reads sources, saves source snapshots, records Findings against source IDs and updates durable memory.
4. The Research Controller assesses dimension coverage and stagnation at observed research phase boundaries and when the Agent requests research status. It returns actions such as `FILL_EVIDENCE_GAP`, `REPLAN`, `ENTER_CONVERGENCE` and `REQUEST_HUMAN`. The Agent performs most research actions; the runtime automatically parks the task when a phase-boundary decision requires human input.
5. The Agent writes the report. ReportValidator checks structure, URLs, registered source IDs and paragraph-level citation coverage.
6. ResearchEvaluator performs rule-based assessment and, when hard blocks do not already prevent review, a separate LLM review call. The default five dimensions are goal coverage, evidence sufficiency, source reliability, conclusion consistency and report completeness. The weighted total threshold is 80 and each dimension must reach 60 by default.
7. The Executor persists and reads back the deliverable. The research role allows up to three delivery repair rounds; unresolved quality issues are preserved and may require an operator decision.

## Runtime budgets

The research role profile defaults to 30 tool calls, 3,600 seconds and 100,000 tokens. Task-level values can override these defaults.

- Tool-call count is consumed atomically before each tool starts.
- Cumulative runtime is checked at controlled model/tool/callback boundaries and is retained across retries. Pauses are excluded from accumulated runtime.
- Before a main Agent model request, the runtime reserves a heuristic input-token estimate plus a 4,096-token output cap and 256-token framing overhead. The model is configured with the same 4,096 output cap. Provider-reported usage is used for settlement when available.
- A convergence reserve protects 30% of the token budget, 25% of tool calls and 20% of runtime (subject to minimum/maximum reserve rules). When an exploration limit is reached, new searches and source reads are blocked so the remaining quota can be used for synthesis, persistence, validation and repair.
- Budget extension requests are recorded for operator approval. Approved extensions add quota; a task failed specifically by a hard budget gate can be requeued to reuse its checkpoint and workspace.

Token estimates are heuristic, not provider-exact accounting. Dollar cost is not currently exposed in the console.

## API

- Task and role: `POST /tasks`, `GET /tasks`, `GET /tasks/{task_id}`, `GET /agents`
- Events: `GET /tasks/{task_id}/events`, `GET /tasks/{task_id}/events/stream`
- Task controls: `POST /tasks/{task_id}/pause`, `resume`, `retry`, `cancel`; `DELETE /tasks/{task_id}`
- Artifacts: `GET /tasks/{task_id}/artifacts`, `GET /tasks/{task_id}/artifacts/{path}`
- Research state: `GET /tasks/{task_id}/research`, `GET /tasks/{task_id}/evidence`, `GET /tasks/{task_id}/memory`
- Budget: `GET /tasks/{task_id}/budget`, `POST /tasks/{task_id}/budget/approve`
- Human intervention: `POST /tasks/{task_id}/human-intervention/resolve`
- Operations: `GET /health`, `GET /system/overview`

SSE accepts `Last-Event-ID` or `after_id`. Events are persisted in SQLite before streaming and can be replayed by monotonically increasing event ID.

## Storage

- `data/tasks.sqlite`: task status, worker ownership, budgets, event journal, research control state, quality assessments, tool-call records, recovery decisions and human interventions.
- `data/checkpoints.sqlite`: LangGraph checkpoints keyed by task ID.
- `workspaces/{task_id}/`: plan, memory, source snapshots, evidence registry, intermediate artifacts and final report.

Back up both SQLite databases and the workspace directory together. Task data is retained until explicit deletion. The first additive schema migration archives legacy active tasks that predate runtime generation 2; those legacy active tasks are not automatically resumed by the current Worker.

## Tests

Run the offline suite:

~~~powershell
python -m pytest -v
node --check app/static/app.js
~~~

GitHub Actions runs the Python test suite on Python 3.12 and checks the frontend JavaScript with Node 22. Tests use local SQLite and test doubles; they do not prove a live end-to-end research run against a real model, Tavily and GitHub services. A live smoke test requires the corresponding credentials.

## Current boundaries and known limitations

- One machine and one active Worker. The global Worker lease deliberately prevents multiple active Workers; multi-host scheduling and concurrent role execution are not implemented.
- Only the research role is registered by default. The registry provides role-specific builders, output artifacts, validators, postprocessors, finalizers and budget defaults, but the Executor still has research-specific lifecycle hooks.
- Cancellation is cooperative. A blocking model or network request may finish before the next control boundary.
- The Controller computes evidence coverage and recommends actions; the Agent still chooses and performs most research actions. The runtime directly enforces budget gates and human parking, not every recommended action.
- Evidence snapshots are stored with SHA-256 metadata. Current integrity checks verify snapshot presence and the embedded hash/URL markers; they do not recompute the hash from the extracted source body or make the workspace tamper-proof.
- The LLM reviewer uses the same configured model as the Agent, but a separate call and context. It reviews a bounded report/evidence excerpt, not every source in full.
- CI is offline. Real provider reliability, end-to-end research quality and crash recovery under live external calls still require operational smoke tests.

## Research source integrations

GitHub tools are registered only for the research role. The default GitHub MCP transport is the official hosted read-only endpoint at <https://api.githubcopilot.com/mcp/readonly>. The adapter uses a read-only tool allow-list, requires a prior directory index, pins file reads to the indexed commit SHA and stores task-scoped snapshots. A fine-grained PAT should have only the required read permissions.

The arXiv adapter uses the official Atom API, a process-wide three-second request interval and a five-minute search cache. Paper PDF retrieval reuses the public-PDF extraction path, including SSRF and download-size checks.

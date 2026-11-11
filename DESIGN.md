# Design Notes — Research Digital Employee MVP

## 1. Digital employee vs. ordinary LLM application

An ordinary LLM application primarily maps one request to one answer or runs a fixed workflow. A digital employee is goal-oriented and long-running: it decomposes a goal, chooses tools, accumulates evidence, changes its plan when evidence exposes gaps, persists its working state, and produces a durable artifact. The system therefore treats task lifecycle, agent state, tool execution, artifacts, and observability as first-class concerns rather than treating the conversation transcript as the application database.

## 2. Architecture and responsibility boundaries

```text
Client
  |
  v
FastAPI ---------------------- SQLite task/event DB
  |                                  ^
  | creates task                     | claim / heartbeat / status / events
  v                                  |
Single Worker -----------------------+
  |
  v
ResearchExecutor
  |
  +---- Deep Agents (planning, file/context harness, tool selection)
  |       |
  |       +---- web_search (Tavily)
  |       +---- fetch_url (HTTP + HTML extraction)
  |       +---- save/read/list_artifact (task workspace)
  |
  +---- LangGraph SqliteSaver (checkpoints.sqlite; thread_id = task_id)
  |
  +---- workspaces/<task_id>/ (durable notes, sources, report.md)
```

### Digital employee / role-specific layer

`app/agents/research/` owns the Research Agent's role prompt, profile, and research conventions. A future Coding Agent or Data Analysis Agent should add a profile, skills, and an allowlisted tool set rather than reimplementing the Worker, task database, or checkpoint lifecycle.

### Shared platform layer

- `api/`: task submission, status, event, control, and artifact endpoints.
- `runtime/`: one Worker, execution boundary, retries, heartbeat, and recovery.
- `infrastructure/database.py`: task state, event journal, and singleton worker lease.
- `infrastructure/artifact_store.py`: task-scoped durable artifact storage with atomic writes and path validation.
- `tools/`: reusable search, fetch, and artifact capabilities.
- LangGraph Checkpointer: durable graph state, separate from task lifecycle rows.

This split makes a role replaceable without rewriting platform concerns.

## 3. Execution and recovery model

1. `POST /tasks` writes a `queued` task row and a `task_created` event, then returns a UUID.
2. The Worker acquires a SQLite singleton lease and atomically claims the oldest due task (`queued` or `retry_wait`). Claiming increments `attempts` and sets `status=running`.
3. The executor opens `SqliteSaver` and uses `thread_id=task_id`. If the graph has pending nodes, it calls `agent.invoke(None, config=...)` to continue from the checkpoint. If no checkpoint exists, it submits the original goal. If the graph has completed but the Worker died before updating the task row, it materializes `report.md` from the last assistant message.
4. Model/tool callbacks and tool services append structured events to SQLite. Search responses and fetched pages are saved under the task workspace. Agent-generated plans/notes/report are persisted with `save_artifact`.
5. A heartbeat thread renews both the worker lease and task heartbeat while a long synchronous model call is running.
6. On Worker restart, a task still marked `running` whose heartbeat is older than the lease threshold is moved to `queued` with `resume_mode=1`. The same LangGraph `thread_id` allows the executor to inspect and continue its checkpoint.
7. Transient or unexpected failures are recorded and scheduled with bounded exponential backoff. After `max_attempts`, the task becomes `failed`.

Task rows and LangGraph checkpoints solve different problems. A task row answers “is this job queued/running/completed and who owns it?” A checkpoint answers “what state and graph node should this Agent continue from?” Neither replaces the other.

## 4. Main technical challenges

### 4.1 Long-context control

Research pages and search results can grow much faster than the useful working context. Deep Agents provides a filesystem/context harness; the Research Agent is instructed to save plans, source notes, and evidence files, then carry concise summaries in its active context. The task workspace stores the durable evidence independently of the model context.

### 4.2 Durable execution and at-least-once replay

Checkpointing does not create exactly-once external side effects. A Worker can die after a tool succeeds but before the next checkpoint is committed. This MVP uses read-only web tools and atomic, overwrite-safe artifact writes, making replay low-risk. Any future tool that sends messages, modifies tickets, deploys code, or changes business data must add an idempotency key and a durable operation journal.

### 4.3 Observability and evidence quality

The event journal records task transitions, model call boundaries, tool inputs/outputs (truncated), errors, checkpoint recovery, and artifact writes. The Research Agent must save source artifacts and include direct URLs in the report. A future evaluator can use these traces to identify weak planning, repetitive search, unsupported claims, and expensive tool/model choices.

## 5. Reliability controls

- Model timeout and bounded model retry configured on `ChatOpenAI`.
- Worker-level bounded retries with exponential backoff.
- Single-worker SQLite lease prevents accidental concurrent Worker processes.
- Heartbeat allows recovery of stale `running` tasks.
- Graph recursion limit bounds the number of internal graph transitions.
- Per-attempt wall-clock budget is checked at model/tool boundaries.
- Pause/cancel are cooperative and checked at model/tool boundaries; an in-flight HTTP/model request may finish first.
- Search/fetch have network timeouts; fetch limits response bytes and validates public HTTP(S) destinations/redirects.
- Artifact paths are relative to a task workspace, traversal is rejected, and writes use temporary files plus atomic replace.

## 6. Most important three problems

1. **Context and state management:** preserve useful evidence without carrying every page in the model context; use file-backed notes plus LangGraph checkpoints.
2. **Long-running reliability:** separate API from Worker, persist task lifecycle, heartbeat the Worker, recover stale tasks, and bound retries/runtime.
3. **Observability and trustworthiness:** record the decision/tool lifecycle, preserve source URLs and fetched material, and distinguish sourced facts from recommendations.

## 7. What to improve after six months of task history

1. **Trajectory evaluation and failure analytics:** build task benchmarks, replay traces, classify failure modes, measure source coverage/citation validity, and run regression evaluations before changing prompts/models.
2. **Adaptive planning and model/tool routing:** learn which planning patterns, search providers, and model tiers work for each task class; use cost/quality-aware routing and reusable Skills.
3. **Multi-role production runtime:** move task/event state to PostgreSQL, queue work through Redis or a durable queue, run multiple isolated Workers, add per-user authorization, sandboxing, rate limits, quotas, and centralized tracing.

## 8. Current scope and trade-offs

This is a local, single-worker MVP, not a multi-tenant production service. SQLite and a local workspace keep setup small and make the recovery path easy to inspect. Search uses Tavily and webpage text extraction uses HTTP + BeautifulSoup rather than a headless browser. The agent has no shell execution capability. The system intentionally avoids multi-agent delegation so that the core single-agent execution and recovery behavior can be validated first.

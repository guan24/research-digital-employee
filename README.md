# Research Digital Employee

A local, long-running autonomous research employee built with **Deep Agents, LangGraph, FastAPI and SQLite**. The browser console is the supported task-submission workflow; a separate Worker executes tasks independently of the browser session.

## Features

- Agent-generated overall research plan with evidence-driven strategic revisions. Per-step progress (pending, in progress, completed, skipped, failed) is displayed independently from strategic version history. History records the trigger, new information, analysis of the previous approach, decision and concrete changes—not routine progress.
- The final plan and all material revisions are archived to `notes/agent_execution_plan.json` when a task reaches a terminal state.
- Tavily search, public webpage fetching and task-scoped Markdown/JSON artifacts.
- Durable task lifecycle and event journal in SQLite; LangGraph SQLite checkpoints keyed by task ID.
- Live execution timeline over Server-Sent Events (SSE), with persisted event IDs and reconnect/replay. New events update the DOM locally instead of rebuilding the timeline, preserving expanded panels and detail scroll positions.
- Observable model/tool lifecycle: model identifier, purpose, input summary, generated output/decision summary, token usage, duration, tool parameters, result summaries, errors and checkpoint recovery.
- Credential redaction before event persistence: API keys, bearer tokens, passwords, authorization values and credential-like fields are masked. Research content is retained.
- Task deletion: queued/paused/retry-wait/terminal tasks are fenced from Worker claims and deleted immediately. Running tasks receive cooperative cancellation; the API waits up to 60 seconds for Worker confirmation before removing task events, LangGraph checkpoint rows and workspace. On timeout, task data is preserved.
- Chinese task goals are stored as Unicode UTF-8 and covered by HTTP round-trip tests.

## Requirements

- Python 3.12 recommended.
- OpenAI API key and Tavily API key.
- Windows, macOS or Linux. Docker is optional.

## Windows local setup

Open two PowerShell terminals in the repository.

Terminal 1 — create environment and install dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY` and `TAVILY_API_KEY`.

Terminal 1 — start API:

```powershell
uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000
```

Terminal 2 — start the single Worker:

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.runtime.worker
```

Open the Research Console at <http://127.0.0.1:8000/>. Create research tasks in the browser form; Chinese input is sent as UTF-8 JSON. Do not use terminal code-page conversion to submit task goals.

## Console workflow

1. Create a task from the browser. Set goal, step budget, runtime budget and retry count.
2. Select the task to inspect its status and chronological execution trace.
3. The timeline streams persisted events over SSE. Reconnect uses the last event ID; historical events remain available through `GET /tasks/{task_id}/events`.
4. The trace groups model/tool start and completion/failure events into one card. The default view shows concise decisions, status, duration and token usage. Raw model prompts/responses stay collapsed until “View full request / response” is opened; tool inputs/outputs are separately expandable. SSE updates only the affected card, preserving expanded panels and their scroll positions. Only observable generated model output is shown, never hidden chain-of-thought. Credentials are redacted before persistence.
5. Inspect the Agent's overall plan, per-step progress, and strategic revision history. The execution-instance panel shows Worker ownership, heartbeat, checkpoint/retry state, model/tool counts, attempts and recent diagnostics. Final plan and progress are archived to `notes/agent_execution_plan.json`.
6. Browse task-scoped intermediate files. Markdown supports rendered/source modes; JSON is displayed as plain source text without parsing, reformatting or JSON rendering, inside a fixed-width preview frame.
7. Retry failed tasks, pause/resume or cancel using the task controls.
8. Delete a task permanently. For a running task, the UI shows the stopping/cleanup state while the API waits up to 60 seconds. If the Worker does not confirm stopping, deletion is aborted and task data remains.

## API

- `POST /tasks`, `GET /tasks`, `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`, `GET /tasks/{task_id}/events/stream`
- `POST /tasks/{task_id}/pause`, `POST /tasks/{task_id}/resume`
- `POST /tasks/{task_id}/retry`, `POST /tasks/{task_id}/cancel`
- `DELETE /tasks/{task_id}`
- `GET /tasks/{task_id}/artifacts`, `GET /tasks/{task_id}/artifacts/{path}`
- `GET /system/overview`

SSE clients may send `Last-Event-ID` or `after_id`. Each event has a monotonically increasing `event_id`.

## Storage

- `data/tasks.sqlite`: tasks and durable event journal. Event rows cascade with task deletion.
- `data/checkpoints.sqlite`: LangGraph checkpoint tables; deletion removes rows whose `thread_id` matches the task ID.
- `workspaces/<task_id>/`: search results, fetched sources, notes and final report.

Task execution data is retained until the user deletes the task. Back up both SQLite databases and `workspaces/` together.

## Tests

```powershell
python -m pytest -v
```

Tests do not require live OpenAI or Tavily credentials. They cover task persistence, UTF-8 round trips, event replay, credential redaction, deletion fencing/cascade and Worker lifecycle. A live research smoke test requires valid API keys.

## Plan history and artifact preview

The Agent plan panel describes the overall research strategy. Only the initial plan and material evidence-driven replans become versions; routine step progress remains in the execution trace. A replan records its trigger, new information, analysis of the previous approach, decision and concrete changes. At terminal task states, the final plan and all material revisions are archived in `notes/agent_execution_plan.json`. The archive stores concise decision rationale, not hidden chain-of-thought.

Markdown supports rendered and raw-source preview modes. JSON is shown as original plain text only—no JSON parsing, pretty-printing, tree rendering or syntax highlighting. Both use the same fixed-width preview frame with internal scrolling for long lines.

## MVP boundaries

This is a single-machine, single-Worker MVP. SQLite is not intended for multi-host high-concurrency workloads. Cancellation is cooperative: an in-flight model/network request may finish before the next control boundary. Hidden model chain-of-thought is not captured or displayed; the console records generated outputs and observable decisions.


## Reliability and quality controls

- `max_runtime_seconds` is cumulative across Worker retries. SQLite heartbeats settle elapsed runtime; pause/retry does not reset the consumed budget.
- `max_steps` is the strict task-wide tool-call budget, enforced atomically in SQLite before a tool starts. LangGraph recursion_limit is a separate graph-loop guard.
- A deterministic ReportValidator checks minimum report length, Markdown title, a source section and at least one URL. Failed reports are fed back to the Agent for at most two revisions; a still-invalid report fails with validation events preserved.
- Public PDF text extraction is supported through `fetch_pdf`; scanned PDFs that need OCR are reported as non-extractable.
- Worker startup waits for an active lease to be released or expire before taking over.
- Agent builders are registered by `agent_type`. The MVP registers `research`; additional roles can provide their own builder, prompt and tools without changing the runtime executor.

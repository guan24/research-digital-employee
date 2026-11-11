# Design — Research Digital Employee

## Runtime model

The browser is the operator console. FastAPI creates and controls durable tasks. A separate single Worker claims tasks and runs a task-scoped Deep Agent. LangGraph SQLite checkpoints preserve graph state; the task database preserves lifecycle and an append-only observable event journal.

## Responsibilities

- Role-specific: `app/agents/research/` owns the research role prompt and research conventions.
- Shared runtime: FastAPI, task database, Worker lease/heartbeat, retry, cancellation, checkpoint lifecycle, SSE, event redaction and artifact storage.
- Tools: web search, public-page fetch and task-scoped artifact operations.

## Event journal and SSE

Every event is committed to SQLite before it is streamed. `event_id` is the durable cursor. SSE accepts `Last-Event-ID` and `after_id`, queries events with a greater ID, and emits keep-alive comments when idle. Browser reconnection therefore replays persisted events without losing the event journal. The UI deduplicates by event ID.

Event categories include task lifecycle, generated plan/plan updates, model-call start/completion/failure, tool-call start/completion/failure, research search/fetch, checkpoint resume, artifact save, retries and execution exceptions. Callback instrumentation records model identity/purpose, bounded input summary, observable generated output/decision summary, token usage and duration. Tool callbacks record tool name, parameters, output and duration. Hidden chain-of-thought is not captured.

## Credential redaction

All event payloads pass through recursive key-based and common token-pattern redaction before JSON serialization and persistence. API key/token/password/authorization/credential-like keys are replaced with `[REDACTED]`; bearer tokens and common provider key prefixes in text are masked. Research text is retained. Redaction is defense-in-depth, not a substitute for secret-free prompts and tool outputs.

## Safe task deletion

Deletion is a two-phase operation:

1. In a `BEGIN IMMEDIATE` transaction, change the task to `deleting`. This state is excluded from Worker claim queries. For a running task, set `cancel_requested=1` while retaining its worker ID; for non-running tasks, clear the worker owner immediately.
2. For a running task, wait up to 60 seconds for the Worker to handle cooperative cancellation. The Worker exits the executor (thereby closing the LangGraph SQLite checkpointer) and clears its worker ID while leaving status `deleting`.
3. Only after worker ownership is cleared, remove all checkpoint rows with matching `thread_id`, remove the task workspace, and delete the task row. The events table uses foreign-key cascade.
4. If the Worker does not stop within 60 seconds, restore a non-deleting status and preserve the task row, events, checkpoint and workspace. A cleanup error leaves the task row available for diagnosis/retry.

This is designed for the single-worker MVP. It does not provide cross-process filesystem locking against external processes writing directly into a task workspace.

## Unicode

Task goals are JSON strings encoded as UTF-8 by browsers and stored in SQLite TEXT. API round-trip tests verify Chinese, English and punctuation survive POST, persistence and GET unchanged. The console sends `application/json; charset=utf-8`.

## Persistence

- `tasks.sqlite`: task state and event journal.
- `checkpoints.sqlite`: LangGraph checkpoint state.
- `workspaces/<task_id>/`: durable research artifacts.

Task data is retained indefinitely until explicit deletion. Back up all three storage locations together.

## Current limits

Single machine, one active Worker, SQLite, Tavily search and HTTP/BeautifulSoup extraction. Cancellation is cooperative and cannot interrupt a blocking provider call mid-request. The UI reports observable model output and decisions, not hidden chain-of-thought.

## Strategic plan versioning and final archive

The plan is the Agent's overall research strategy, not a step-progress tracker.

- The first update_research_plan call records version 1.
- A later call creates a version only when the normalized plan structure materially changes. Per-step status fields are stripped and never create plan versions.
- A material replan must record five audit fields: trigger, new_information, problem_analysis, decision, and changes. The complete revised plan and a structural diff (added/removed/modified/reordered workstreams) are stored with the event.
- Routine tool calls, reads, searches, and step completion remain in the execution event journal and do not mutate the plan history.
- Legacy step-status-only plan events are collapsed when building the archive and rendering the history.
- On terminal outcomes (completed, failed, cancelled), the Worker freezes the current plan and all material revisions in notes/agent_execution_plan.json, then emits research_plan_finalized.
- The rationale is a concise evidence-based decision record, not hidden chain-of-thought. Model outputs and tool inputs/results remain in the separate execution trace.

## Artifact preview layout

Markdown has rendered and raw-source modes. JSON is intentionally shown as original source text only: no JSON parsing, pretty-printing, tree rendering, or syntax highlighting. The preview panel and grid item use min-width: 0, fixed width: 100% constraints, and internal horizontal scrolling for long source lines/tables so switching between Markdown and JSON does not resize the outer preview frame.


## Step progress and task-instance diagnostics

Strategic plan versions and step execution progress are separate event streams. \`research_plan_updated\` stores strategy; \`research_plan_step_status\` stores per-step transitions (\`pending\`, \`in_progress\`, \`completed\`, \`skipped\`, \`failed\`). Unchanged step IDs retain progress after a replan; added or materially modified steps reset to pending. On terminal task failure, still-in-progress steps are marked failed with the task error. The final archive stores both the strategy-only plan and a final plan snapshot with progress.

Task details expose Worker ID, heartbeat, next retry time, checkpoint-resume mode and pause/cancel flags. The console derives model/tool counts, attempt history, recovery events and recent diagnostics from the event journal. The task-detail loader independently loads task metadata, events and artifacts so one failed endpoint does not blank the other panels; a load-generation token prevents stale requests from overwriting the current selection.

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


## Runtime budgets, report validation and role registry

Task runtime is cumulative across retries. SQLite stores `runtime_seconds_used` and `runtime_last_accounted_at`; worker heartbeats and stale-worker recovery settle elapsed wall time. Claiming a retry does not reset the accumulated value. `max_steps` is the strict task-wide tool-call budget enforced by an atomic SQL update before each tool starts. LangGraph recursion_limit remains a separate graph-loop guard.

The report validator is a deterministic structural/evidence gate, not a factuality oracle. It checks minimum body length, a Markdown title, a source section and at least one URL. Failed validation is recorded and fed back to the Agent for at most two bounded revisions; exhaustion fails the task and preserves the report and validation events for review.

Roles register a builder plus optional artifact path, validator, bounded postprocessor, finalizer and default budgets. Missing optional hooks have safe defaults. The executor is role-agnostic and never imports research modules. The research registration preserves report.md, the original evidence-coverage validator, two targeted revisions and plan archival.


## Long-running execution enhancements

The runtime separates role registration, shared task tools, evidence registration, durable memory and deterministic report validation. Deep Agents summarization compacts long conversations using absolute token thresholds derived from the model profile or MODEL_MAX_INPUT_TOKENS; known OpenAI-compatible models such as qwen3.8-flash have explicit context-window fallbacks. This avoids requiring LangChain profile metadata for fractional thresholds while the SQLite checkpointer preserves graph execution state. Research memory and source snapshots are stored in each task workspace.

Task budgets are enforced, not merely observed. Tool-call count is atomically consumed before each tool. Cumulative runtime is checked at model-call start, tool-call start and callback control boundaries; exceeding the limit raises a terminal runtime-budget signal, records the root cause and does not schedule a retry. Model calls reserve a conservative UTF-8-byte input estimate plus a 2,048-token output cap and 128-token fixed overhead before provider invocation; ChatOpenAI is configured with the same output cap. Successful calls settle provider usage, failed calls release reservations, and repeated callback settlement is idempotent. Insufficient reservation rejects the model call before it reaches the provider and fails the task with TOKEN_BUDGET_EXHAUSTED. At 80% the runtime warns; at 95% exploration tools are denied while artifact/evidence/memory tools remain available. Approved extensions add quota and preserve checkpoint/workspace for recovery. The estimate is deliberately heuristic; dollar-cost UI is not implemented. API endpoints expose budget, evidence and memory state.

Evidence IDs map to immutable content snapshots and source URLs. Findings link claims to known source IDs. The final report gate validates structure, source URLs, registered IDs and paragraph-level citation coverage; this is a deterministic provenance check, not a semantic truth proof.


## Error classification

Worker routes retryable provider/network failures through backoff, fails terminal authentication/permission/invalid-request/delivery-validation errors immediately, and handles control signals without retry. Unknown exceptions default to retryable but use the explicit UNKNOWN_EXCEPTION class. SQLite preserves latest error, first root cause and append-only per-attempt history.

## Enforced task budgets

Tool calls are consumed atomically before tool execution and cannot exceed max_steps. Model requests reserve a conservative UTF-8-byte input estimate plus a 2,048-token output cap and 128-token fixed overhead before invocation; ChatOpenAI uses the same output cap. Completion settles provider-reported usage, failures release reservations, and repeated settlement is idempotent. Stale worker generations cannot settle reservations. Runtime is cumulative across attempts and pauses are excluded. At 80% the system warns; at 95% exploration tools are blocked. If the next model-call reservation cannot fit, the provider is not called and the task fails with TOKEN_BUDGET_EXHAUSTED; runtime exhaustion also fails terminally without retry. Approved quota extensions add limits and resume the existing checkpoint. Token estimates are heuristic; dollar-cost UI is not implemented.


The research role's default step/runtime/token limits have one source of truth in `app/agents/research/profile.toml`; the role registration derives the effective defaults from those fields. Explicit task-level values override them.


## Research data sources: GitHub MCP and arXiv

GitHub and arXiv adapters are registered only by the research role's ResearchToolService; the shared runtime has no imports of either provider adapter or the MCP client. GitHubMCPClient uses GitHub's official hosted remote MCP endpoint in read-only mode by default, lazily initializes and reuses a Streamable HTTP session per Worker, serializes requests, enforces a timeout, and performs a bounded reinitialization after transport failure. Requests use the /readonly endpoint plus X-MCP-Readonly and X-MCP-Tools headers. The application client independently restricts calls to its seven-tool allow-list because the remote server may advertise additional read-only tools from its default toolsets. Local stdio remains an opt-in transport using --read-only and --tools. A generic infrastructure shutdown registry lets Worker close local provider processes without importing provider modules in app/runtime.

The GitHub adapter offers repository overview, filtered directory listing, batch file reading (maximum five paths per Agent tool step) and release history. It requires a prior directory index for each parent path, pins the batch to the indexed commit SHA, and rejects uncached, unsafe, secret, binary or over-limit paths before any file-read MCP request. It writes commit-addressed task snapshots under sources/github/, and registers immutable github_file / github_repo evidence using the shared Sxxxx ledger. It normalizes not-found, permission, rate-limit, timeout, upstream and policy errors into structured tool results, task error-history entries and operation events. Tool-level source errors do not automatically fail the task.

The arXiv adapter uses only urllib, urllib.parse and xml.etree.ElementTree. It supports field-qualified and Boolean Atom API queries, clamps results to 20, enforces a process-wide three-second request interval and caches equivalent searches for five minutes. Search results and metadata are registered as arxiv evidence. arxiv_paper delegates PDF retrieval to the existing ResearchToolService.fetch_pdf pipeline with the arXiv abstract URL as the evidence URL, retaining SSRF checks, download-size limits and PDF extraction behavior. No third-party dependency is added.

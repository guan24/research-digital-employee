# Design — Research Digital Employee

This document describes the implementation currently present in the repository, not a target architecture. The MVP optimizes for durable, inspectable research execution on one machine.

## 1. Runtime model

The browser is an operator console. FastAPI creates and controls durable tasks. A separate Worker claims tasks and executes a task-scoped Deep Agent. The Agent runs an open-ended tool-use loop: it proposes and revises a research plan, selects tools based on the information gathered, records evidence and writes artifacts. The runtime does not prescribe a fixed search/read/write sequence.

The MVP intentionally uses one active Worker and SQLite. A task is not a single HTTP request: it has a durable task ID, lifecycle state, graph checkpoint, event history, resource limits and task-scoped workspace.

~~~mermaid
flowchart TD
  UI[Browser console] --> API[FastAPI]
  API --> DB[(Task SQLite)]
  DB --> W[Single Worker]
  W --> EX[Research Executor]
  EX --> AG[Deep Agent + LangGraph]
  AG --> T[Research tools]
  AG --> A[Task artifacts and memory]
  T --> E[Evidence registry and source snapshots]
  EX --> CP[(LangGraph SQLite checkpoint)]
  EX --> DB
  DB --> SSE[SSE event stream]
  SSE --> UI
~~~

## 2. Responsibility boundaries

| Component | Current responsibility |
|---|---|
| FastAPI | Task creation and controls, role discovery, events, artifacts, research state, budget and human-intervention APIs |
| Agent registry | Role builder, optional profile loader, output path, validator, postprocessor, repair limit, finalizer and budget defaults |
| Research Agent | Research prompt, dynamic plan, tool selection, evidence collection, memory and report writing |
| TaskToolService | Shared task-scoped artifact read/write, durable memory, budget inspection and extension requests |
| ResearchToolService | Research-specific search/read tools, evidence/Finding operations, plan and research-status tools, GitHub and arXiv adapters |
| Research Controller | Deterministic requirements normalization, Finding-to-source coverage, weighted progress and bounded action recommendations |
| Research Evaluator | Rule-based quality metrics plus an isolated LLM review; persists scores, hard blocks and defects |
| Recovery Manager | Tool-call lifecycle, unknown-call verification, checkpoint inspection and stale-task reconciliation |
| Worker / Executor | Claiming, callbacks, checkpoints, model/tool budgets, retries, delivery validation, human parking and role finalization |
| SQLite / workspace | Task state, events, budgets, quality/recovery records, graph checkpoints and durable files |

The registry is an extension seam, not proof of a complete multi-role scheduler. Only `research` is registered by default. The Executor still attaches research-specific plan-progress and lifecycle components when the selected role is `research`; it is therefore not fully role-agnostic yet.

## 3. Task execution lifecycle

1. FastAPI validates the requested role and creates a generation-2 task using role budget defaults unless the request overrides them.
2. The Worker obtains the global SQLite lease, starts its heartbeat loop and claims one queued/retry-wait task. The task receives a new worker generation.
3. The Executor loads the role registration, creates a task-scoped ArtifactStore, opens the LangGraph SQLite checkpointer and builds the Agent.
4. The Agent invokes tools through callbacks. Callbacks check task controls and runtime budget, atomically consume a tool-call step, record model/tool events, reserve model tokens and journal tool-call lifecycle.
5. For the research role, tool activity is mapped to coarse phases (scope, discovery, reading, analysis, report, validation). PlanProgressTracker infers progress from tool/phase transitions unless the Agent has supplied explicit progress. Inferred completion still requires a non-empty readable artifact; otherwise the step becomes `awaiting_verification`.
6. The research Controller assesses coverage at observed phase boundaries and when the Agent requests research status. It returns allowed/recommended actions. A `REQUEST_HUMAN` decision parks the task at a phase boundary; the Agent performs most other recommended actions.
7. The Executor ensures the final role artifact exists, reads it back, runs the role validator and, for research, the quality lifecycle. Failed delivery can be repaired within the registration's revision limit. Unresolved quality/reviewer conditions may park for a human decision.
8. The Worker marks the task terminal and invokes the role finalizer. The research finalizer archives the plan and its material revisions.

A checkpoint uses the task ID as its LangGraph thread ID. On resume, the Executor reuses the existing checkpoint. Human decisions are injected as messages with stable IDs so the same decision is not injected twice after an interruption.

## 4. Persistence and state ownership

| Store | Owns |
|---|---|
| `tasks.sqlite` | Task status, worker ID/generation, lease, budgets, retry metadata, event journal, research requirements/coverage, quality assessments, tool calls, recovery decisions and human interventions |
| `checkpoints.sqlite` | LangGraph graph state and pending nodes |
| `workspaces/{task_id}/` | Research plan, plan archive, memory, source snapshots, evidence registry, intermediate artifacts and final deliverable |

Important tables include `tasks`, `events`, `token_reservations`, `worker_lease`, `schema_migrations`, `research_control_state`, `quality_assessments`, `tool_calls`, `recovery_decisions` and `human_interventions`.

The current additive migration adds runtime-generation and lifecycle fields. On first application of migration version 1, legacy generation-1 tasks still in active states (queued, running, retry_wait, paused or waiting_for_human) are marked `archived` and retain their former state in `archived_from_status`. New tasks are generation 2; the current Worker only claims generation-2 tasks. This migration does not automatically resume legacy active tasks.

## 5. Evidence-driven research control

Research requirements are stored separately from the plan. Each dimension has an ID, title, questions, expected evidence, priority and minimum source count. If the Agent does not define requirements, the Controller derives them from plan steps; if there is no usable plan, it creates a fallback research-scope dimension.

A Finding links a claim to one or more registered source IDs and one primary dimension. Coverage counts only source IDs linked through Findings and whose snapshots pass the current presence/marker checks. Dimension status is covered, partial or uncovered. Weighted coverage uses priority weights: critical 3, high 2, normal 1.

The progress vector contains coverage ratio, valid source count, Finding count, critical-gap count and latest quality score. A cycle counts as stagnated after two consecutive eligible assessment cycles without material improvement while critical gaps remain.

The Controller returns `FILL_EVIDENCE_GAP`, `REPLAN`, `CONTINUE_RESEARCH`, `ENTER_CONVERGENCE` or `REQUEST_HUMAN` as recommendations/allowed actions. It does not search, rewrite the plan or independently execute all actions. The Agent receives the decision through the research-status tool and the system prompt. The runtime directly parks a task when a phase-boundary callback observes `REQUEST_HUMAN`.

## 6. Delivery validation and quality evaluation

ReportValidator performs deterministic structure/provenance checks: a Markdown H1, a separate Sources/References section, minimum body length, direct URLs, known registered source IDs, corresponding source URLs in the Sources section and paragraph-level citation coverage. When an evidence registry is supplied, at least 40% of eligible body paragraphs (paragraphs of at least 80 characters) must contain a valid registered source citation. It also rejects unknown Finding IDs when the report cites them. This is a provenance/format gate, not proof that a source semantically supports a claim.

ResearchEvaluator has five dimensions with default weights: goal coverage 20%, evidence sufficiency 25%, source reliability 20%, conclusion consistency 20% and report completeness 15%. Rule scores are combined with LLM scores at 60%/40%. The default weighted total threshold is 80 and the per-dimension threshold is 60. Empty reports, no registered sources and invalid source snapshots are non-waivable hard blocks. The independent review is a separate call with no Agent conversation history, but it uses the same configured model and endpoint. It receives the report (bounded to 16,000 characters), up to eight source excerpts (each taken from the end of the snapshot, up to 1,000 characters) and up to 30 Findings.

The current research registration sets `max_revisions=3`, so the Executor can run up to three delivery repair rounds after the initial validation. The TOML profile also contains `quality_max_revisions=2`, which is loaded into evaluator profile data but does not currently control the Executor's repair loop; the registration value is the effective delivery limit. This duplicate setting should be consolidated in a future code change.

## 7. Long-running execution, budgets and recovery

The Worker uses a single global SQLite lease and task heartbeats. Retryable failures use bounded exponential backoff; terminal errors fail immediately; pause, cancel, lease loss and human waiting are handled as control signals rather than ordinary retries.

The hard tool-call budget is consumed atomically before each tool starts. Cumulative runtime is checked at model/tool/callback boundaries and is retained across retries. The main Agent model is configured for a 4,096-token output cap. Before a model call, the runtime reserves a heuristic input estimate plus that output cap and 256 tokens of framing overhead; provider-reported usage is used for settlement when available.

The budget policy protects 30% of tokens, 25% of tool calls and 20% of runtime for convergence (subject to minimum/maximum reserve rules). When an exploration limit is reached, the task enters convergence and exploration tools such as search and source reads are blocked; artifact, memory, evidence and selected progress tools remain available. Approved extensions add limits and preserve the task workspace/checkpoint.

RecoveryManager records tool calls with an input hash, idempotency key, worker generation, status and result/error metadata. On stale-task reconciliation:
- Read-only web, PDF, GitHub and arXiv calls are eligible for retry because their external response cannot be proven from local state.
- Artifact writes are confirmed only by exact read-back comparison with the original input.
- Finding, plan, step-progress and requirement writes are checked against their corresponding persisted record/event.
- Unknown tool types require manual review.
- Checkpoint inspection checks the SQLite database/table shape and whether the task thread has checkpoint rows. A detected corruption or ambiguous tool call parks the task for human handling; otherwise the Worker can requeue the task to resume the same checkpoint.

This is conservative at the application boundary, not an exactly-once guarantee for external services. A retried read request may be sent more than once.

## 8. Observability and operator intervention

Events are persisted in SQLite before SSE delivery. Event IDs are durable cursors; clients can reconnect using `Last-Event-ID` or `after_id`. Model/tool callbacks record lifecycle, bounded inputs/outputs, duration, token usage and errors. Credentials are redacted before event persistence. Hidden chain-of-thought is not collected.

The operator APIs expose task events, budget, evidence, memory, research control state, quality assessments, recovery decisions and pending human interventions. Human decisions are validated against the choices attached to the pending intervention. Supported actions include continue research, change direction, add requirements, continue repair, approve degraded delivery and terminate, subject to the specific intervention's allowed choices. Degraded delivery requires explicit confirmation and a report read-back verification.

## 9. Role boundary and current limitations

Shared task capabilities live in TaskToolService. Research-specific capabilities live in ResearchToolService and its GitHub/arXiv adapters. A role registration can supply its own builder, profile loader, artifact path, validator, postprocessor, repair count, finalizer and budget defaults.

The current system is still a single-machine, single-Worker MVP:
- Only the research role is registered by default; multi-role parallel execution is not implemented.
- The Executor still contains research-specific lifecycle wiring.
- Controller recommendations other than human parking and budget gates are primarily Agent-directed, not all hard-enforced by the runtime.
- Source snapshot validation checks file presence and embedded hash/URL markers; it does not recompute the content hash from the extracted source body, and the task workspace is not a tamper-proof immutable store.
- The LLM reviewer shares the Agent's model configuration and reviews bounded excerpts; the rule-based contradiction check is lexical/heuristic.
- SQLite and the global Worker lease are not a multi-host scheduler. Cancellation is cooperative.
- Automated tests are offline and use fakes for external/model behavior. A real end-to-end research task and live crash/restart test remain necessary acceptance evidence.


## Offline runtime verification

The offline integration suite drives a real LangChain agent graph with structured fake model tool calls through ResearchToolService, the public executor/worker, SQLite checkpoints and task workspaces. It verifies evidence-driven plan changes, preserved unchanged-step progress, reset changed steps, controller coverage growth, registered citations and a failed review followed by targeted repair. Invalid reviewer structure fails delivery; repeated low scores exhaust the repair allowance and park for human review.

`tests/test_role_extensibility.py` registers a test-only analysis role, never a production default. It exercises FastAPI creation, common tools, JSON artifact validation, its own finalizer and budget defaults, checkpoint recovery after pause, cancellation and bounded transient retry. Research-specific quality assessment is not invoked.

Recovery checks cover structured tool failures (including ToolMessage output), ordinary exceptions, late callbacks after generation takeover, corrupt checkpoints, exact Artifact/Finding read-back and incomplete plan publication. Existing conflicting artifacts, inconsistent plan files/progress and changed completed-step artifacts are preserved for human review. Explicit tool policies live in `TOOL_RECOVERY_POLICIES`; unrecognized tools remain manual. `get_research_status`, `set_active_plan_step` and budget-extension requests are conservatively manual during uncertain recovery because they mutate control/audit state. Memory writes require exact persisted read-back. Recovery confirmations emit a completion event.

All automated model responses, reviewer responses and external HTTP data are fake. The test fixture prevents external socket connections and supplies deterministic DNS; Windows asyncio loopback wakeup sockets remain allowed. No real model API or paid token usage participates. CI runs the full pytest suite on Python 3.12 plus the JavaScript syntax check.

Boundary: this establishes runtime orchestration under simulated model decisions, not real-model research judgment. External-provider behavior, multi-host concurrent filesystem writes and exactly-once execution of external services remain outside this verification. Callback ownership checks reject a worker already superseded before its callback starts; they do not provide a distributed transaction spanning SQLite, graph checkpoints and workspace files. The production Deep Agents builder and real model quality still need a separate manual smoke test.

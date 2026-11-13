from __future__ import annotations

import hashlib
from datetime import datetime, timezone

try:
    from langchain_core.tools import tool
except ImportError:
    def tool(name=None):
        def decorate(func):
            func.name = name or func.__name__
            return func
        return decorate

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.runtime.control import TaskCancelled, TaskPaused, TaskDeadlineExceeded, TaskWaitingForHuman, WorkerLeaseLost


class TaskToolService:
    """Capabilities shared by every role, scoped to one durable task workspace."""

    def __init__(self, settings, db: Database, task_id: str, artifacts: ArtifactStore,
                 started_monotonic: float, worker_id: str | None = None,
                 worker_generation: int | None = None):
        self.settings = settings
        self.db = db
        self.task_id = task_id
        self.artifacts = artifacts
        self.started_monotonic = started_monotonic
        self.worker_id = worker_id
        self.worker_generation = worker_generation

    def check_control(self) -> None:
        task = self.db.get_task(self.task_id)
        if task is None:
            raise TaskCancelled("task record no longer exists")
        if task.get("status") == "waiting_for_human":
            raise TaskWaitingForHuman("task is waiting for an operator decision")
        if self.worker_id:
            if task.get("worker_id") != self.worker_id:
                raise WorkerLeaseLost("task ownership changed")
            current_generation = int(task.get("worker_generation") or 0)
            if self.worker_generation is not None and current_generation != int(self.worker_generation):
                raise WorkerLeaseLost("task execution generation changed")
            if not self.db.owns_worker_lease(self.worker_id):
                raise WorkerLeaseLost("worker singleton lease was lost")
        flags = self.db.get_control_flags(self.task_id)
        if not flags:
            raise TaskCancelled("task record no longer exists")
        if flags["cancel_requested"] or flags["status"] in {"cancelled", "deleting"}:
            raise TaskCancelled("task cancellation requested")
        if flags["pause_requested"] or flags["status"] == "paused":
            raise TaskPaused("task pause requested")
        budget = self.db.check_runtime_budget(
            self.task_id, self.worker_id, self.worker_generation
        )
        if budget.get("exhausted"):
            raise TaskDeadlineExceeded(
                f"cumulative runtime budget exhausted: used={budget['used']:.2f}s "
                f"limit={budget['limit']}s"
            )
        self.db.emit_budget_alerts(self.task_id)

    def budget_tool_gate(self, tool_name: str) -> str | None:
        status = self.db.get_budget_status(self.task_id)
        if not status.get("enabled"):
            return None
        restricted = (
            status.get("phase") == "convergence"
            or status.get("state") in {"restricted", "soft_limit", "hard_limit"}
        )
        if restricted:
            allowed = {
                "save_artifact", "read_artifact", "list_artifacts",
                "save_research_memory", "read_research_memory",
                "get_budget_status", "get_evidence_registry", "record_finding",
                "update_plan_step_status", "request_budget_extension",
            }
            if tool_name not in allowed:
                payload = {
                    "success": False,
                    "error_type": "budget_intercept",
                    "reason": "Protected convergence reserve active. Stop exploration and use saved evidence to write and validate the deliverable.",
                    "dimension": status.get("trigger_dimension"),
                    "phase": status.get("phase"),
                    "remaining": status.get("dimensions", {}),
                    "tool": tool_name,
                }
                self.db.add_event(self.task_id, "budget_tool_rejected", payload)
                return __import__("json").dumps(payload, ensure_ascii=False)
        return None

    def save_artifact(self, path: str, content: str) -> str:
        self.check_control()
        normalized = str(content if content is not None else "")
        if self.artifacts.exists(path) and self.artifacts.read_text(path) == normalized:
            return f"Artifact already read-back verified: {path}"
        saved = self.artifacts.write_text(path, normalized)
        persisted = self.artifacts.read_text(path)
        if persisted != normalized:
            raise OSError(f"artifact read-back verification failed: {path}")
        digest = hashlib.sha256(persisted.encode("utf-8")).hexdigest()
        self.db.add_event(self.task_id, "artifact_saved", {
            "path": saved,
            "bytes": len(persisted.encode("utf-8")),
            "sha256": digest,
            "read_back_verified": True,
        })
        return f"Saved and read-back verified artifact: {saved} (sha256={digest[:12]})"

    def read_artifact(self, path: str, start_char: int = 0, max_chars: int = 8_000) -> str:
        self.check_control()
        content = self.artifacts.read_text(path)
        start = max(0, int(start_char or 0))
        limit = max(1_000, min(int(max_chars or 8_000), 12_000))
        end = min(len(content), start + limit)
        excerpt = content[start:end]
        if start == 0 and end == len(content):
            return excerpt
        return (
            f"[Artifact excerpt: characters {start}-{end} of {len(content)}. "
            f"Read the next chunk with start_char={end}.]\n\n{excerpt}"
        )

    def list_artifacts(self) -> str:
        self.check_control()
        return __import__("json").dumps(self.artifacts.list_files(), ensure_ascii=False)

    def save_research_memory(self, content: str) -> str:
        self.check_control()
        normalized = str(content or "").strip()
        if not normalized:
            raise ValueError("research memory cannot be empty")
        if len(normalized) > 20_000:
            raise ValueError("research memory exceeds 20000 characters")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        version_path = f"notes/memory_versions/{stamp}_{digest}.md"
        self.artifacts.write_text(version_path, normalized + "\n")
        self.artifacts.write_text("notes/research_memory.md", normalized + "\n")
        self.db.add_event(self.task_id, "research_memory_saved", {
            "path": "notes/research_memory.md", "version_path": version_path,
            "characters": len(normalized),
        })
        return f"Memory saved: {version_path}; latest summary: notes/research_memory.md"

    def read_research_memory(self) -> str:
        self.check_control()
        path = "notes/research_memory.md"
        if not self.artifacts.exists(path):
            return "No durable research memory has been saved yet."
        return self.artifacts.read_text(path)[:20_000]

    def get_budget_status(self) -> str:
        self.check_control()
        import json
        return json.dumps(self.db.get_budget_status(self.task_id), ensure_ascii=False, indent=2)

    def request_budget_extension(self, reason: str, additional_steps: int = 0,
                                 additional_runtime_seconds: int = 0,
                                 additional_tokens: int = 0) -> str:
        self.check_control()
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("a reason is required")
        request = {
            "reason": reason[:1200],
            "additional_steps": max(0, min(int(additional_steps), 100)),
            "additional_runtime_seconds": max(0, min(int(additional_runtime_seconds), 86_400)),
            "additional_tokens": max(0, min(int(additional_tokens), 5_000_000)),
        }
        if not any(request[key] for key in ("additional_steps", "additional_runtime_seconds", "additional_tokens")):
            raise ValueError("request at least one additional resource dimension")
        self.db.add_event(self.task_id, "budget_extension_requested", request)
        return "Budget extension request recorded for operator approval. Soft budgets do not interrupt execution."

    def as_common_tools(self):
        @tool("save_artifact")
        def save_artifact_tool(path: str, content: str) -> str:
            """Save an intermediate task artifact. Paths must be relative and use supported extensions."""
            return self.save_artifact(path, content)

        @tool("read_artifact")
        def read_artifact_tool(path: str, start_char: int = 0, max_chars: int = 8_000) -> str:
            """Read a bounded chunk of a saved artifact; use start_char to continue large files."""
            return self.read_artifact(path, start_char, max_chars)

        @tool("list_artifacts")
        def list_artifacts_tool() -> str:
            """List all files in the task workspace."""
            return self.list_artifacts()

        @tool("save_research_memory")
        def save_research_memory_tool(content: str) -> str:
            """Persist concise verified progress, key decisions, open questions and next steps."""
            return self.save_research_memory(content)

        @tool("read_research_memory")
        def read_research_memory_tool() -> str:
            """Recover durable research memory after context summarization or worker restart."""
            return self.read_research_memory()

        @tool("get_budget_status")
        def get_budget_status_tool() -> str:
            """Inspect task-wide soft budgets, actual usage and convergence guidance."""
            return self.get_budget_status()

        @tool("request_budget_extension")
        def request_budget_extension_tool(reason: str, additional_steps: int = 0,
                                          additional_runtime_seconds: int = 0,
                                          additional_tokens: int = 0) -> str:
            """Request operator approval for additional task resources; does not interrupt the task."""
            return self.request_budget_extension(
                reason, additional_steps, additional_runtime_seconds, additional_tokens
            )

        return [
            save_artifact_tool, read_artifact_tool, list_artifacts_tool,
            save_research_memory_tool, read_research_memory_tool,
            get_budget_status_tool, request_budget_extension_tool,
        ]

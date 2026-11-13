from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.infrastructure.evidence_registry import EvidenceRegistry


READ_ONLY_TOOLS = {
    "web_search", "fetch_url", "fetch_pdf", "github_repo", "github_tree",
    "github_read", "github_releases", "arxiv_search", "arxiv_paper",
}
WRITE_TOOLS = {"save_artifact", "record_finding", "update_research_plan", "update_plan_step_status"}


class RecoveryManager:
    """Conservative recovery coordinator for the single-worker runtime.

    External read calls cannot be made exactly-once without provider idempotency
    support. This manager records their lifecycle and classifies uncertain calls;
    it only auto-confirms side effects when persisted artifacts/records prove them.
    """

    def __init__(self, db: Database, workspace_dir: Path, checkpoint_path: Path):
        self.db = db
        self.workspace_dir = Path(workspace_dir)
        self.checkpoint_path = Path(checkpoint_path)

    @staticmethod
    def _json_input(value: str) -> tuple[dict[str, Any], str]:
        try:
            parsed = json.loads(str(value or ""))
            if isinstance(parsed, dict):
                return parsed, json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        except (TypeError, json.JSONDecodeError):
            pass
        return {}, str(value or "")

    def begin_tool_call(self, task_id: str, call_id: str, tool_name: str,
                        input_value: str, worker_generation: int = 0,
                        metadata: dict[str, Any] | None = None) -> str:
        arguments, canonical = self._json_input(input_value)
        input_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        metadata = metadata or {}
        scope = ":".join(str(metadata.get(key) or "") for key in ("langgraph_node", "langgraph_step"))
        # A graph node + step + normalized arguments is stable across a resumed
        # checkpoint. When graph metadata is absent, run_id prevents false dedupe.
        stable_scope = scope if scope.strip(":") else str(call_id)
        key_material = "|".join((task_id, stable_scope, tool_name, input_hash))
        idempotency_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        record = self.db.begin_tool_call(
            task_id, str(call_id), idempotency_key, str(tool_name or "unknown"),
            json.dumps(arguments, ensure_ascii=False, default=str) if arguments else canonical,
            input_hash, worker_generation,
        )
        return str(record.get("call_id") or call_id)

    def finish_tool_call(self, task_id: str, call_id: str, output: Any) -> None:
        self.db.finish_tool_call(task_id, call_id, str(output))

    def fail_tool_call(self, task_id: str, call_id: str, error: Any) -> None:
        self.db.fail_tool_call(task_id, call_id, str(error), type(error).__name__)

    def inspect_checkpoint(self, task_id: str) -> dict[str, Any]:
        if not self.checkpoint_path.exists():
            return {"exists": False, "thread_found": False, "corrupt": False}
        try:
            with sqlite3.connect(self.checkpoint_path, timeout=5) as conn:
                tables = [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                if "checkpoints" not in tables:
                    return {"exists": True, "thread_found": False, "corrupt": False, "tables": tables}
                columns = {row[1] for row in conn.execute('PRAGMA table_info("checkpoints")')}
                if "thread_id" not in columns:
                    return {"exists": True, "thread_found": False, "corrupt": True,
                            "reason": "checkpoint table has no thread_id column"}
                count = conn.execute(
                    'SELECT COUNT(*) FROM "checkpoints" WHERE thread_id=?', (task_id,)
                ).fetchone()[0]
                return {"exists": True, "thread_found": bool(count), "checkpoint_count": int(count), "corrupt": False}
        except sqlite3.DatabaseError as exc:
            return {"exists": True, "thread_found": False, "corrupt": True,
                    "reason": f"{type(exc).__name__}: {exc}"}

    def _verify_unknown_call(self, task_id: str, record: dict[str, Any],
                             artifacts: ArtifactStore, registry: EvidenceRegistry) -> dict[str, Any]:
        tool_name = str(record.get("tool_name") or "")
        arguments, _ = self._json_input(str(record.get("input_json") or ""))
        call_id = str(record.get("call_id") or "")
        if tool_name in READ_ONLY_TOOLS:
            return {"action": "retry_allowed", "confidence": "high",
                    "reason": "只读外部请求无法确认响应；允许按预算重新请求，原调用记录保留。"}
        if tool_name == "save_artifact":
            path = str(arguments.get("path") or "")
            content = str(arguments.get("content") or "")
            if path and artifacts.exists(path):
                try:
                    persisted = artifacts.read_text(path)
                    if persisted == content:
                        self.db.finish_tool_call(task_id, call_id, "recovered by exact artifact read-back")
                        return {"action": "mark_succeeded", "confidence": "high",
                                "reason": "目标 Artifact 与原始写入内容完全一致。",
                                "path": path,
                                "sha256": hashlib.sha256(persisted.encode("utf-8")).hexdigest(),
                                "bytes": len(persisted.encode("utf-8"))}
                except (OSError, ValueError):
                    pass
            return {"action": "retry_allowed", "confidence": "medium",
                    "reason": "未发现与原始参数完全一致的 Artifact，可在 Checkpoint 恢复后重试。", "path": path}
        if tool_name == "record_finding":
            snapshot = registry.summary()
            for finding in snapshot.get("findings", []):
                if (finding.get("claim") == arguments.get("claim")
                        and finding.get("source_ids") == arguments.get("source_ids")
                        and finding.get("dimension_id") == arguments.get("dimension_id")):
                    self.db.finish_tool_call(task_id, call_id, "recovered by finding registry lookup")
                    return {"action": "mark_succeeded", "confidence": "high",
                            "reason": "Evidence Registry 中已存在完全匹配的 Finding。",
                            "finding_id": finding.get("finding_id")}
            return {"action": "retry_allowed", "confidence": "medium",
                    "reason": "未找到匹配 Finding；恢复时允许重新登记。"}
        if tool_name == "update_research_plan":
            events = self.db.get_events(task_id, limit=5000)
            latest = next((event for event in reversed(events)
                           if event.get("event_type") == "research_plan_updated"), None)
            if latest:
                self.db.finish_tool_call(task_id, call_id, "recovered by persisted plan event")
                return {"action": "mark_succeeded", "confidence": "high",
                        "reason": "数据库已有持久化的研究计划版本。",
                        "plan_version": latest.get("payload", {}).get("version")}
            return {"action": "retry_allowed", "confidence": "medium",
                    "reason": "未发现计划更新事件，可从原 Checkpoint 重试。"}
        if tool_name == "update_plan_step_status":
            events = self.db.get_events(task_id, limit=5000)
            step_id = str(arguments.get("step_id") or "")
            status = str(arguments.get("status") or "")
            exists = any(
                event.get("event_type") == "research_plan_step_status"
                and (event.get("payload") or {}).get("step_id") == step_id
                and (event.get("payload") or {}).get("status") == status
                for event in events
            )
            if exists:
                self.db.finish_tool_call(task_id, call_id, "recovered by plan progress event")
                return {"action": "mark_succeeded", "confidence": "high",
                        "reason": "数据库已有匹配的步骤状态事件。"}
            return {"action": "retry_allowed", "confidence": "medium",
                    "reason": "未发现匹配的步骤状态事件。"}
        return {"action": "manual_review", "confidence": "low",
                "reason": "工具类型不在自动核验白名单中，禁止盲目重放。"}

    def reconcile_stale_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = str(task["task_id"])
        artifacts = ArtifactStore(self.workspace_dir, task_id)
        registry = EvidenceRegistry(artifacts)
        decisions = []
        ambiguous = []
        for record in self.db.get_open_tool_calls(task_id):
            self.db.mark_tool_call_unknown(task_id, record["call_id"], "worker heartbeat expired")
            result = self._verify_unknown_call(task_id, record, artifacts, registry)
            decisions.append({"call_id": record["call_id"], "tool": record["tool_name"], **result})
            self.db.record_recovery_decision(
                task_id, "tool_call_reconciliation", result["action"], result["confidence"],
                {"call_id": record["call_id"], "tool": record["tool_name"], "reason": result["reason"]},
            )
            if result["action"] == "manual_review":
                ambiguous.append(result)
        checkpoint = self.inspect_checkpoint(task_id)
        self.db.record_recovery_decision(
            task_id, "checkpoint_inspection",
            "manual_review" if checkpoint.get("corrupt") else "resume_or_start",
            "low" if checkpoint.get("corrupt") else "high", checkpoint,
        )
        if checkpoint.get("corrupt"):
            ambiguous.append({"reason": "LangGraph Checkpoint 数据库或表结构损坏。", "checkpoint": checkpoint})
        outcome = {"task_id": task_id, "checkpoint": checkpoint, "tool_calls": decisions,
                   "requires_human": bool(ambiguous), "conflicts": ambiguous}
        if ambiguous:
            self.db.set_waiting_for_human(
                task_id, "recovery_conflict",
                {"summary": "自动恢复无法证明状态唯一正确。", "conflicts": ambiguous,
                 "checkpoint": checkpoint, "tool_calls": decisions},
            )
        else:
            self.db.add_event(task_id, "recovery_reconciliation_completed", outcome)
        return outcome

    def reconcile_stale_tasks(self, lease_seconds: int) -> dict[str, int]:
        recovered = 0
        waiting = 0
        for task in self.db.get_stale_running_tasks(lease_seconds):
            result = self.reconcile_stale_task(task)
            if result["requires_human"]:
                waiting += 1
            else:
                recovered += 1
        return {"checked": recovered + waiting, "safe_to_resume": recovered, "waiting_for_human": waiting}

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database


READ_ONLY_TOOLS = {
    "web_search", "fetch_url", "fetch_pdf", "github_repo", "github_tree",
    "github_read", "github_releases", "arxiv_search", "arxiv_paper",
    "read_artifact", "list_artifacts", "read_research_memory", "get_budget_status",
    "get_human_decisions", "get_evidence_registry",
}
WRITE_TOOLS = {
    "save_artifact", "record_finding", "update_research_plan",
    "update_plan_step_status", "define_research_requirements",
    "save_research_memory",
}

MANUAL_TOOLS = {"set_active_plan_step", "get_research_status", "request_budget_extension"}
TOOL_RECOVERY_POLICIES = {
    **dict.fromkeys(READ_ONLY_TOOLS, "retry_allowed"),
    **dict.fromkeys(WRITE_TOOLS, "verify_persistence"),
    **dict.fromkeys(MANUAL_TOOLS, "manual_review"),
}


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
                        metadata: dict[str, Any] | None = None, worker_id=None, event_payload=None) -> str:
        arguments, canonical = self._json_input(input_value)
        input_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        metadata = metadata or {}
        idempotency_key = self.call_key(task_id, tool_name, input_value, metadata, call_id)
        record = self.db.begin_tool_call(
            task_id, str(call_id), idempotency_key, str(tool_name or "unknown"),
            json.dumps(arguments, ensure_ascii=False, default=str) if arguments else canonical,
            input_hash, worker_generation, worker_id, event_payload,
        )
        return str(record.get("call_id") or call_id)

    @classmethod
    def call_key(cls, task_id, tool_name, input_value, metadata, call_id):
        _, canonical = cls._json_input(input_value)
        input_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        scope = ":".join(str(metadata.get(key) or "") for key in ("langgraph_node", "langgraph_step"))
        stable_scope = scope if scope.strip(":") else str(call_id)
        return hashlib.sha256("|".join((task_id, stable_scope, tool_name, input_hash)).encode("utf-8")).hexdigest()

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
                             artifacts: ArtifactStore, registry: Any = None, expected_task=None) -> dict[str, Any]:
        expected_task = expected_task or self.db.get_task(task_id)
        def confirm(value):
            return self.db.finish_tool_call(task_id, record["call_id"], value,
                expected_task.get("worker_id"), expected_task["worker_generation"], {"recovered": True})
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
                        confirm("recovered by exact artifact read-back")
                        return {"action": "mark_succeeded", "confidence": "high",
                                "reason": "目标 Artifact 与原始写入内容完全一致。",
                                "path": path,
                                "sha256": hashlib.sha256(persisted.encode("utf-8")).hexdigest(),
                                "bytes": len(persisted.encode("utf-8"))}
                except (OSError, ValueError):
                    pass
                return {"action": "manual_review", "confidence": "low",
                        "reason": "Artifact exists with conflicting or unreadable content; preserve it.", "path": path}
            return {"action": "retry_allowed", "confidence": "medium",
                    "reason": "Artifact is absent; retry from checkpoint.", "path": path}
        if tool_name == "save_research_memory":
            path = "notes/research_memory.md"
            content = str(arguments.get("content") or "").strip() + "\n"
            if artifacts.exists(path) and artifacts.read_text(path) == content:
                confirm("recovered by memory read-back")
                return {"action": "mark_succeeded", "confidence": "high", "reason": "Exact memory read-back."}
            return {"action": "manual_review", "confidence": "low", "reason": "Memory write cannot be verified."}
        if tool_name == "record_finding":
            if registry is None:
                from app.infrastructure.evidence_registry import EvidenceRegistry
                registry = EvidenceRegistry(artifacts)
            snapshot = registry.summary()
            for finding in snapshot.get("findings", []):
                if (finding.get("claim") == arguments.get("claim")
                        and finding.get("source_ids") == arguments.get("source_ids")
                        and finding.get("dimension_id") == arguments.get("dimension_id")):
                    confirm("recovered by finding registry lookup")
                    return {"action": "mark_succeeded", "confidence": "high",
                            "reason": "Evidence Registry 中已存在完全匹配的 Finding。",
                            "finding_id": finding.get("finding_id")}
            return {"action": "retry_allowed", "confidence": "medium",
                    "reason": "未找到匹配 Finding；恢复时允许重新登记。"}
        if tool_name == "update_research_plan":
            events = self.db.get_events(task_id, limit=5000)
            latest = next((event for event in reversed(events)
                           if event.get("event_type") == "research_plan_updated"), None)
            def normalize_plan(value: dict[str, Any]) -> dict[str, Any]:
                return {
                    "title": str(value.get("title") or "研究执行计划").strip(),
                    "objective": str(value.get("objective") or "").strip(),
                    "steps": [{
                        "id": str(step.get("id") or "").strip(),
                        "title": str(step.get("title") or "").strip(),
                        "objective": str(step.get("objective") or "").strip(),
                        "expected_output": str(step.get("expected_output") or "").strip(),
                        "expected_output_path": str(step.get("expected_output_path") or "").strip(),
                    } for step in value.get("steps") or [] if isinstance(step, dict)],
                }
            candidate = normalize_plan(arguments)
            persisted = normalize_plan((latest or {}).get("payload", {}).get("plan") or {})
            if latest and candidate == persisted:
                plan_path = "notes/research_plan.json"
                if not artifacts.exists(plan_path):
                    return {"action": "manual_review", "confidence": "low", "reason": "Plan file is missing."}
                try:
                    saved_plan = json.loads(artifacts.read_text(plan_path))
                except (ValueError, OSError):
                    saved_plan = {}
                if (normalize_plan(saved_plan) != persisted
                        or saved_plan.get("version") != latest["payload"].get("version")):
                    return {"action": "manual_review", "confidence": "low", "reason": "Plan file and event disagree."}
                progress = [e["payload"] for e in events if e["event_type"] == "research_plan_step_status"]
                if any(not any(p.get("step_id") == step["id"] and p.get("plan_version") == latest["payload"].get("version")
                               for p in progress) for step in persisted["steps"]):
                    return {"action": "manual_review", "confidence": "low", "reason": "Plan progress synchronization is incomplete."}
                confirm("recovered by exact plan comparison")
                return {"action": "mark_succeeded", "confidence": "high",
                        "reason": "数据库中存在与本次工具输入完全一致的研究计划。",
                        "plan_version": latest.get("payload", {}).get("version")}
            return {"action": "retry_allowed", "confidence": "medium",
                    "reason": "最新持久化计划与本次工具输入不完全一致，不能将历史计划误判为本次写入成功。"}
        if tool_name == "update_plan_step_status":
            events = self.db.get_events(task_id, limit=5000)
            step_id = str(arguments.get("step_id") or "")
            status = str(arguments.get("status") or "")
            note = str(arguments.get("note") or "")[:1500]
            latest_plan = next((event for event in reversed(events)
                                if event.get("event_type") == "research_plan_updated"), None)
            latest_plan_payload = (latest_plan or {}).get("payload") or {}
            version = int(latest_plan_payload.get("version") or 1)
            current_plan = latest_plan_payload.get("plan") or {}
            current_step = next(
                (item for item in current_plan.get("steps", [])
                 if str(item.get("id") or "") == step_id),
                {},
            )
            expected_output_path = str(
                arguments.get("output_path") or current_step.get("expected_output_path") or ""
            ).strip()
            latest_status = next((event for event in reversed(events)
                                  if event.get("event_type") == "research_plan_step_status"
                                  and (event.get("payload") or {}).get("step_id") == step_id), None)
            exists = any(
                event.get("event_type") == "research_plan_step_status"
                and (event.get("payload") or {}).get("step_id") == step_id
                and (event.get("payload") or {}).get("status") == status
                and (event.get("payload") or {}).get("note", "") == note
                and int((event.get("payload") or {}).get("plan_version") or 1) == version
                and (
                    status != "completed"
                    or (
                        bool(expected_output_path)
                        and (event.get("payload") or {}).get("verified_output", {}).get("path") == expected_output_path
                    )
                )
                for event in ([latest_status] if latest_status else [])
            )
            if exists and status == "completed":
                verified = latest_status["payload"].get("verified_output") or {}
                try:
                    content = artifacts.read_text(expected_output_path)
                    exists = bool(content.strip()) and hashlib.sha256(content.encode("utf-8")).hexdigest() == verified.get("sha256")
                except (OSError, ValueError):
                    exists = False
                if not exists:
                    return {"action": "manual_review", "confidence": "low", "reason": "Completed step artifact no longer matches its verified hash."}
            if exists:
                confirm("recovered by exact plan progress comparison")
                return {"action": "mark_succeeded", "confidence": "high",
                        "reason": "数据库已有与本次参数、计划版本和产物校验完全匹配的步骤状态事件。"}
            return {"action": "retry_allowed", "confidence": "medium",
                    "reason": "未发现完全匹配的步骤状态事件，允许从原 Checkpoint 重试。"}
        if tool_name == "define_research_requirements":
            from app.runtime.research_controller import ResearchController
            candidate = ResearchController._normalize_requirements({
                "dimensions": arguments.get("dimensions") or [],
                "quality_policy": arguments.get("quality_policy") or {},
            })
            current = (self.db.get_research_control_state(task_id) or {}).get("requirements") or {}
            current_normalized = (
                ResearchController._normalize_requirements(current)
                if current.get("dimensions") else {}
            )
            if candidate == current_normalized:
                confirm("recovered by exact requirements comparison")
                return {"action": "mark_succeeded", "confidence": "high",
                        "reason": "Research Controller 中已持久化完全匹配的研究要求。"}
            return {"action": "retry_allowed", "confidence": "medium",
                    "reason": "当前研究要求与本次工具输入不一致，可按原参数重试。"}
        return {"action": "manual_review", "confidence": "low",
                "reason": "工具类型不在自动核验白名单中，禁止盲目重放。"}

    def reconcile_stale_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = str(task["task_id"])
        artifacts = ArtifactStore(self.workspace_dir, task_id)
        decisions = []
        ambiguous = []
        for record in self.db.get_open_tool_calls(task_id):
            self.db.mark_tool_call_unknown(task_id, record["call_id"], "worker heartbeat expired", task.get("worker_id"), task["worker_generation"])
            result = self._verify_unknown_call(task_id, record, artifacts, expected_task=task)
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
                 "checkpoint": checkpoint, "tool_calls": decisions,
                 "choices": ["terminate"]},
                worker_id=task.get("worker_id"), generation=task["worker_generation"],
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

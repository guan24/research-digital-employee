from __future__ import annotations

from pathlib import Path
import hashlib
from typing import Any
import asyncio
import json
import shutil
import sqlite3
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.infrastructure.artifact_store import ArtifactError, ArtifactStore
from app.schemas.task import BudgetExtensionApproval, EventOut, HumanInterventionDecision, TaskCreate, TaskOut, TaskRetry, TaskRetryOut

router = APIRouter()


def _db(request: Request):
    return request.app.state.db


def _settings(request: Request):
    return request.app.state.settings


@router.get("/health")
def health(request: Request):
    db = _db(request)
    return {"status": "ok", "database": str(db.path)}


@router.get("/system/overview")
def system_overview(request: Request):
    """Small operational snapshot for the local research console."""
    return _db(request).get_runtime_overview()


@router.post("/tasks", response_model=TaskOut, status_code=202)
def create_task(body: TaskCreate, request: Request):
    settings = _settings(request)
    from app.agents.registry import get_agent_builder
    try:
        get_agent_builder(body.agent_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from app.agents.registry import get_agent_registration
    defaults = get_agent_registration(body.agent_type).budget_defaults
    task = _db(request).create_task(
        goal=body.goal,
        agent_type=body.agent_type,
        max_attempts=body.max_attempts,
        max_steps=body.max_steps if body.max_steps is not None else defaults.max_steps,
        max_runtime_seconds=body.max_runtime_seconds if body.max_runtime_seconds is not None else defaults.max_runtime_seconds,
        budget_enabled=body.budget_enabled,
        max_total_tokens=body.max_total_tokens if body.max_total_tokens is not None else defaults.max_total_tokens,
    )
    ArtifactStore(settings.workspace_dir, task["task_id"])
    return task


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(request: Request, limit: int = 50):
    return _db(request).list_tasks(max(1, min(limit, 200)))


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, request: Request):
    task = _db(request).get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/agents")
def list_agents():
    from app.agents.registry import list_agent_profiles
    return {"agents": list_agent_profiles()}


@router.get("/tasks/{task_id}/budget")
def get_task_budget(task_id: str, request: Request):
    db = _db(request)
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return db.get_budget_status(task_id)


@router.post("/tasks/{task_id}/budget/approve")
def approve_task_budget_extension(task_id: str, body: BudgetExtensionApproval, request: Request):
    db = _db(request)
    current = db.get_task(task_id)
    result = db.approve_budget_extension(
        task_id, reason=body.reason, additional_steps=body.additional_steps,
        additional_runtime_seconds=body.additional_runtime_seconds,
        additional_tokens=body.additional_tokens,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="task not found")
    if current and current.get("status") == "failed" and current.get("error_code") in {
        "TOOL_STEP_BUDGET_EXHAUSTED", "RUNTIME_BUDGET_EXHAUSTED", "TOKEN_BUDGET_EXHAUSTED"
    }:
        resumed = db.requeue_failed_task(task_id, reason="Budget extension approved: " + body.reason)
        if resumed and resumed.get("requeued"):
            db.add_event(task_id, "budget_checkpoint_resume_requested", {
                "reason": body.reason, "checkpoint_reuse_requested": True,
                "workspace_preserved": True, "additions": result["additions"],
            })
            result["resumed_from_checkpoint"] = True
    return result


@router.get("/tasks/{task_id}/evidence")
def get_task_evidence(task_id: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    from app.infrastructure.evidence_registry import EvidenceRegistry
    store = ArtifactStore(_settings(request).workspace_dir, task_id)
    return EvidenceRegistry(store).summary()


@router.get("/tasks/{task_id}/evidence/sources/{source_id}/snapshot", response_class=PlainTextResponse)
def get_task_evidence_snapshot(task_id: str, source_id: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    from app.infrastructure.evidence_registry import EvidenceRegistry
    store = ArtifactStore(_settings(request).workspace_dir, task_id)
    try:
        content = EvidenceRegistry(store).read_snapshot(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="evidence source not found")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="evidence snapshot file not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return PlainTextResponse(content, media_type="text/markdown")


@router.get("/tasks/{task_id}/plan")
def get_task_plan_snapshot(task_id: str, request: Request):
    db = _db(request)
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    from app.runtime.plan_progress import PlanProgressTracker
    store = ArtifactStore(_settings(request).workspace_dir, task_id)
    return PlanProgressTracker(db, task_id, store).snapshot()


@router.get("/tasks/{task_id}/memory")
def get_task_memory(task_id: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    store = ArtifactStore(_settings(request).workspace_dir, task_id)
    path = "notes/research_memory.md"
    return {"path": path, "content": store.read_text(path) if store.exists(path) else ""}


@router.get("/tasks/{task_id}/research")
def get_task_research_state(task_id: str, request: Request):
    db = _db(request)
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    pending = db.get_pending_human_intervention(task_id)
    if pending:
        pending["allowed_actions"] = _allowed_human_actions(pending)
    return {
        "task_id": task_id,
        "control": db.get_research_control_state(task_id),
        "quality_assessments": db.list_quality_assessments(task_id, limit=10),
        "recovery_decisions": db.list_recovery_decisions(task_id, limit=50),
        "pending_intervention": pending,
        "human_decisions": db.list_human_decisions(task_id, limit=10),
    }


def _allowed_human_actions(intervention: dict[str, Any]) -> list[str]:
    request = intervention.get("request") or {}
    choices = request.get("choices") if isinstance(request, dict) else None
    if isinstance(choices, list) and choices:
        return [str(item) for item in choices]
    defaults = {
        "recovery_conflict": ["terminate"],
        "research_control_requires_human": [
            "continue_research", "change_research_direction",
            "add_research_requirements", "terminate",
        ],
        "independent_review_unavailable": [
            "continue_research", "continue_repair",
            "approve_degraded_delivery", "terminate",
        ],
        "quality_not_satisfied": [
            "continue_repair", "approve_degraded_delivery", "terminate",
        ],
    }
    return defaults.get(str(intervention.get("category") or ""), ["terminate"])


@router.post("/tasks/{task_id}/human-intervention/resolve")
def resolve_task_human_intervention(
    task_id: str, body: HumanInterventionDecision, request: Request,
):
    db = _db(request)
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    pending = db.get_pending_human_intervention(task_id)
    if not pending or pending.get("intervention_id") != body.intervention_id:
        raise HTTPException(status_code=409, detail="human intervention is no longer pending")
    if body.action not in _allowed_human_actions(pending):
        raise HTTPException(status_code=409, detail="the selected action is not allowed for this intervention")

    payload: dict[str, Any] = {"reason": body.reason.strip()}
    if body.action == "change_research_direction":
        direction = body.direction.strip()
        if len(direction) < 5:
            raise HTTPException(status_code=422, detail="direction must contain at least 5 characters")
        payload["direction"] = direction

    if body.action == "add_research_requirements":
        if not body.requirements:
            raise HTTPException(status_code=422, detail="requirements are required for add_research_requirements")
        from app.runtime.research_controller import ResearchController
        current = db.get_research_control_state(task_id) or {}
        current_requirements = current.get("requirements") or {}
        try:
            incoming = ResearchController._normalize_requirements(body.requirements)
            current_dimensions = list(current_requirements.get("dimensions") or [])
            existing_ids = {str(item.get("id") or "") for item in current_dimensions}
            overlap = existing_ids.intersection({item["id"] for item in incoming["dimensions"]})
            if overlap:
                raise ValueError("new dimension ids already exist: " + ", ".join(sorted(overlap)))
            old_policy = current_requirements.get("quality_policy") or {}
            new_policy = incoming.get("quality_policy") or {}
            merged_policy = {
                key: max(float(old_policy.get(key, 0)), float(new_policy.get(key, 0)))
                for key in {"min_total_score", "min_dimension_score"}
                if key in old_policy or key in new_policy
            }
            merged = ResearchController._normalize_requirements({
                "dimensions": current_dimensions + incoming["dimensions"],
                "quality_policy": merged_policy,
            })
            ResearchController(db, task_id).define_requirements(merged, source="human")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"invalid research requirements: {exc}") from exc
        payload["requirements"] = incoming

    if body.action == "approve_degraded_delivery":
        if not body.confirm:
            raise HTTPException(status_code=422, detail="explicit confirmation is required for degraded delivery")
        if len(body.reason.strip()) < 3:
            raise HTTPException(status_code=422, detail="a reason is required for degraded delivery")
        artifact_path = str((pending.get("request") or {}).get("artifact_path") or "").strip()
        if not artifact_path:
            raise HTTPException(status_code=409, detail="the intervention has no verified report path")
        store = ArtifactStore(_settings(request).workspace_dir, task_id)
        try:
            report = store.read_text(artifact_path)
        except (FileNotFoundError, OSError, ArtifactError) as exc:
            raise HTTPException(status_code=409, detail=f"degraded report cannot be read: {exc}") from exc
        if not report.strip():
            raise HTTPException(status_code=409, detail="degraded report is empty")
        approval_note = (
            "\n\n---\n\n## 人工批准的降级交付说明\n\n"
            f"人工复核决定：{body.reason.strip()}\n\n"
            "本报告未达到全部自动质量门槛，经人工明确批准后交付。\n"
        )
        try:
            store.write_text(artifact_path, report.rstrip() + approval_note)
            read_back = store.read_text(artifact_path)
        except (OSError, ValueError, ArtifactError) as exc:
            raise HTTPException(status_code=500, detail=f"failed to persist degraded-delivery approval: {exc}") from exc
        if not read_back.strip() or approval_note.strip() not in read_back:
            raise HTTPException(status_code=500, detail="degraded-delivery approval failed read-back verification")
        encoded = read_back.encode("utf-8")
        absolute_path = (Path(_settings(request).workspace_dir) / task_id / artifact_path).resolve()
        app_home = Path(_settings(request).app_home).resolve()
        result_path = str(absolute_path.relative_to(app_home)) if absolute_path.is_relative_to(app_home) else str(absolute_path)
        payload["confirm"] = True
        payload["delivery_verification"] = {
            "path": artifact_path,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
            "read_back_verified": True,
            "result_path": result_path,
        }

    try:
        resolved = db.resolve_human_intervention(
            task_id, body.intervention_id, body.action, payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(status_code=409, detail="human intervention was already resolved or task state changed")
    if body.action == "approve_degraded_delivery":
        db.add_event(task_id, "report_delivered", {
            **payload["delivery_verification"],
            "manual_approval": True,
            "reason": body.reason.strip(),
        })
    return {
        "task_id": task_id,
        "status": resolved.get("status"),
        "action": body.action,
        "message": (
            "人工批准的报告已完成读回校验并交付。"
            if body.action == "approve_degraded_delivery"
            else "人工决策已持久化，任务已按决策进入后续状态。"
        ),
    }


@router.get("/tasks/{task_id}/events", response_model=list[EventOut])
def get_events(task_id: str, request: Request, limit: int = 500, after_id: int = 0):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    db = _db(request)
    bounded_limit = max(1, min(limit, 2000))
    if after_id > 0:
        return db.get_events_after(task_id, after_id, bounded_limit)
    return db.get_events(task_id, bounded_limit)


def _format_sse_event(event: dict) -> str:
    event_id = int(event["event_id"])
    data = json.dumps(event, ensure_ascii=False)
    return f"id: {event_id}\nevent: task_event\ndata: {data}\n\n"




@router.post(
    "/tasks/{task_id}/retry",
    response_model=TaskRetryOut,
    status_code=202,
)
def retry_task(task_id: str, body: TaskRetry, request: Request):
    reason = body.reason.strip() if body.reason and body.reason.strip() else None
    result = _db(request).requeue_failed_task(task_id, reason=reason)
    if result is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not result["requeued"]:
        current_status = result["current_status"]
        raise HTTPException(
            status_code=409,
            detail=f"only failed tasks can be retried; current status is '{current_status}'",
        )

    return {
        "task_id": task_id,
        "status": "queued",
        "checkpoint_reuse_requested": True,
        "workspace_preserved": True,
        "previous_attempts": result["previous_attempts"],
        "message": (
            "Task requeued. The Worker will reuse the existing LangGraph checkpoint "
            "when one is available."
        ),
    }


@router.get("/tasks/{task_id}/events/stream")
async def stream_events(task_id: str, request: Request, after_id: int = 0):
    db = _db(request)
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")

    async def generate():
        try:
            cursor = max(after_id, int(request.headers.get("last-event-id", "0") or 0))
        except ValueError:
            cursor = max(after_id, 0)
        while not await request.is_disconnected():
            if not db.get_task(task_id):
                yield "event: task_deleted\ndata: {}\n\n"
                return
            batch = db.get_events_after(task_id, cursor, 100)
            for event in batch:
                cursor = event["event_id"]
                yield _format_sse_event(event)
            if not batch:
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, request: Request):
    db = _db(request)
    settings = _settings(request)
    task = db.begin_delete(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    deadline = time.monotonic() + (60 if task.get("worker_id") else 0)
    while task.get("worker_id") is not None and time.monotonic() < deadline:
        time.sleep(0.25)
        task = db.get_task(task_id)
        if task is None:
            return {"task_id": task_id, "deleted": True}
    if task.get("worker_id") is not None:
        db.abort_delete(task_id)
        raise HTTPException(status_code=409, detail="Worker did not confirm stop within 60 seconds. Task data was preserved.")

    checkpoint_path = settings.checkpoint_path
    if checkpoint_path.exists():
        with sqlite3.connect(checkpoint_path, timeout=30) as conn:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for (table_name,) in tables:
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')}
                if "thread_id" in columns:
                    conn.execute(f'DELETE FROM "{table_name}" WHERE thread_id=?', (task_id,))
            conn.commit()

    workspace = (settings.workspace_dir / task_id).resolve()
    if settings.workspace_dir.resolve() not in workspace.parents:
        db.abort_delete(task_id)
        raise HTTPException(status_code=400, detail="invalid task workspace path")
    try:
        if workspace.exists():
            shutil.rmtree(workspace)
        db.delete_task_record(task_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Task cleanup failed; task retained: {exc}")
    return {"task_id": task_id, "deleted": True}


@router.post("/tasks/{task_id}/pause")
def pause_task(task_id: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    if not _db(request).request_pause(task_id):
        raise HTTPException(status_code=409, detail="task cannot be paused in its current state")
    return {"task_id": task_id, "status": "pause_requested"}


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    if not _db(request).resume_task(task_id):
        raise HTTPException(status_code=409, detail="only paused tasks can be resumed")
    return {"task_id": task_id, "status": "queued"}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    if not _db(request).request_cancel(task_id):
        raise HTTPException(status_code=409, detail="task cannot be cancelled in its current state")
    return {"task_id": task_id, "status": "cancel_requested"}


@router.get("/tasks/{task_id}/artifacts")
def list_artifacts(task_id: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    store = ArtifactStore(_settings(request).workspace_dir, task_id)
    return {
        "task_id": task_id,
        "artifacts": [
            {**item, "url": f"/tasks/{task_id}/artifacts/{item['path']}"}
            for item in store.list_files()
        ],
    }


@router.get("/tasks/{task_id}/artifacts/{artifact_path:path}", response_class=PlainTextResponse)
def read_artifact(task_id: str, artifact_path: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    store = ArtifactStore(_settings(request).workspace_dir, task_id)
    try:
        content = store.read_text(artifact_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="artifact not found")
    except ArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    media_type = "text/markdown" if artifact_path.lower().endswith(".md") else "text/plain"
    return PlainTextResponse(content, media_type=media_type)

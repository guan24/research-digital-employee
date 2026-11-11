from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import json
import shutil
import sqlite3
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from app.infrastructure.artifact_store import ArtifactError, ArtifactStore
from app.schemas.task import EventOut, TaskCreate, TaskOut, TaskRetry, TaskRetryOut

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
    task = _db(request).create_task(
        goal=body.goal,
        agent_type=body.agent_type,
        max_attempts=body.max_attempts,
        max_steps=body.max_steps,
        max_runtime_seconds=body.max_runtime_seconds,
        budget_enabled=body.budget_enabled,
        max_total_tokens=body.max_total_tokens,
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


@router.get("/tasks/{task_id}/evidence")
def get_task_evidence(task_id: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    from app.infrastructure.evidence_registry import EvidenceRegistry
    store = ArtifactStore(_settings(request).workspace_dir, task_id)
    return EvidenceRegistry(store).summary()


@router.get("/tasks/{task_id}/memory")
def get_task_memory(task_id: str, request: Request):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    store = ArtifactStore(_settings(request).workspace_dir, task_id)
    path = "notes/research_memory.md"
    return {"path": path, "content": store.read_text(path) if store.exists(path) else ""}


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

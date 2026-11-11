from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

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
    task = _db(request).create_task(
        goal=body.goal,
        max_attempts=body.max_attempts,
        max_steps=body.max_steps,
        max_runtime_seconds=body.max_runtime_seconds,
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


@router.get("/tasks/{task_id}/events", response_model=list[EventOut])
def get_events(task_id: str, request: Request, limit: int = 500):
    if not _db(request).get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return _db(request).get_events(task_id, max(1, min(limit, 2000)))


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

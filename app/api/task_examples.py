"""Bundled historical snapshots, served without creating runnable database tasks."""
import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.schemas.task import TaskOut

EXAMPLE_ID = "example-rainbow-colors"
EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "rainbow-research"
router = APIRouter()


def _read(name):
    return json.loads((EXAMPLE_DIR / name).read_text(encoding="utf-8"))


@router.get("/task-examples", response_model=list[TaskOut])
def list_task_examples():
    return [_read("task.json")]


@router.api_route(f"/tasks/{EXAMPLE_ID}", methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
@router.api_route(f"/tasks/{EXAMPLE_ID}/{{resource:path}}", methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def reject_example_mutation(resource: str = ""):
    return JSONResponse(status_code=405, content={"detail": "这是只读任务执行样例，不能重试、继续、修改或删除。"},
                        headers={"Allow": "GET"})


def _artifact(path):
    files = _read("artifacts.json")["artifacts"]
    item = next((f for f in files if f["path"] == path), None)
    root = (EXAMPLE_DIR / "artifacts").resolve()
    target = (root / path).resolve()
    if item is None or not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="example artifact not found")
    data = target.read_bytes()
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise HTTPException(status_code=409, detail="example artifact integrity check failed")
    return PlainTextResponse(data.decode("utf-8"), media_type="text/markdown" if path.endswith(".md") else "text/plain")


@router.get(f"/tasks/{EXAMPLE_ID}")
@router.get(f"/tasks/{EXAMPLE_ID}/{{resource:path}}")
def read_task_example(resource: str = "", limit: int = 500, after_id: int = 0):
    snapshots = {"": "task.json", "budget": "budget.json", "evidence": "evidence.json", "plan": "plan.json",
                 "memory": "memory.json", "research": "research.json", "artifacts": "artifacts.json"}
    if resource in snapshots:
        return _read(snapshots[resource])
    if resource in {"events", "events/stream"}:
        limit = max(1, min(limit, 2000))
        events = _read("events.json")
        events = [e for e in events if e["event_id"] > after_id][:limit] if after_id else events[-limit:]
        if resource == "events/stream":
            return Response("".join(f'id: {e["event_id"]}\nevent: task_event\ndata: {json.dumps(e, ensure_ascii=False)}\n\n' for e in events),
                            media_type="text/event-stream")
        return events
    if resource.startswith("artifacts/"):
        return _artifact(resource[len("artifacts/"):])
    if resource.startswith("evidence/sources/") and resource.endswith("/snapshot"):
        source_id = resource[len("evidence/sources/"):-len("/snapshot")].upper()
        source = next((s for s in _read("evidence.json")["sources"] if s["source_id"] == source_id), None)
        if source:
            return _artifact(source["snapshot_path"])
    raise HTTPException(status_code=404, detail="example resource not found")

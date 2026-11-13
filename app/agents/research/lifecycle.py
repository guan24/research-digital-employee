from __future__ import annotations

from datetime import datetime, timezone

from app.tools.research_tools import ResearchToolService


def finalize_research_task(db, task_id: str, artifacts, status: str, task: dict):
    finalized_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if status == "failed":
        ResearchToolService.mark_open_plan_steps_failed(
            db, task_id, task.get("last_error") or "任务执行失败，步骤未能完成",
            artifacts=artifacts,
        )
    archive_path = ResearchToolService.persist_plan_archive(
        db, task_id, artifacts, status="finalized", outcome=status,
        finalized_at=finalized_at,
    )
    if archive_path:
        import json
        archive = json.loads(artifacts.read_text(archive_path))
        db.add_event(task_id, "research_plan_finalized", {
            "version": archive.get("current_version", 1),
            "outcome": status,
            "history_count": len(archive.get("history", [])),
            "artifact_path": archive_path,
            "finalized_at": finalized_at,
        })
    return archive_path

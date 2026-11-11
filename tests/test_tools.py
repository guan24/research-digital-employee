from __future__ import annotations

import json

from app.infrastructure.artifact_store import ArtifactStore
from app.tools.research_tools import ResearchToolService


def test_research_tools_persist_search_evidence_and_artifacts(settings, db, monkeypatch):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, __import__("time").monotonic())
    service.save_artifact("notes/plan.md", "# Plan")
    assert service.read_artifact("notes/plan.md") == "# Plan"
    assert "notes/plan.md" in service.list_artifacts()


def test_web_search_requires_tavily_key(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, __import__("time").monotonic())
    try:
        service.web_search("example research")
    except RuntimeError as exc:
        assert "TAVILY_API_KEY" in str(exc)
    else:
        raise AssertionError("expected missing search key error")


def test_fetch_rejects_localhost(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, __import__("time").monotonic())
    result = json.loads(service.fetch_url("http://127.0.0.1:8000/internal"))
    assert result["success"] is False
    assert result["error_type"] == "invalid_url"
    assert "local" in result["error"].lower() or "non-public" in result["error"].lower()

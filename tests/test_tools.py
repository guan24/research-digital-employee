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

def test_read_artifact_supports_bounded_chunks_and_save_readback_verification(settings, db):
    task = db.create_task("Read a large artifact in bounded chunks")
    task = db.claim_next_task("worker-chunks")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, __import__("time").monotonic())
    content = "0123456789" * 1_000
    service.save_artifact("sources/large.md", content)

    first = service.read_artifact("sources/large.md")
    second = service.read_artifact("sources/large.md", start_char=8_000, max_chars=2_000)
    assert len(first.split("\n\n", 1)[-1]) == 8_000
    assert "characters 8000-10000 of 10000" in second
    assert second.endswith(content[8_000:])
    saved_event = [
        event for event in db.get_events(task["task_id"])
        if event["event_type"] == "artifact_saved"
    ][-1]
    assert saved_event["payload"]["read_back_verified"] is True
    assert len(saved_event["payload"]["sha256"]) == 64

def test_update_research_plan_tool_uses_structured_step_arguments(settings, db):
    task = db.create_task("Validate the structured research plan tool schema")
    task = db.claim_next_task("worker-plan-schema")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, __import__("time").monotonic())

    tools = {item.name: item for item in service.as_langchain_tools()}
    plan_tool = tools["update_research_plan"]
    schema = plan_tool.args_schema.model_json_schema()

    assert "plan_json" not in schema["properties"]
    assert {"title", "objective", "steps"}.issubset(schema["properties"])
    assert {"title", "objective", "steps"}.issubset(set(schema["required"]))
    result = plan_tool.invoke({
        "title": "Structured plan",
        "objective": "Verify the tool schema and persist the plan",
        "steps": [{
            "id": "scope",
            "title": "Confirm scope",
            "objective": "Define the research boundary",
            "expected_output": "A saved scope definition",
        }] + [{"id": f"step-{i}", "title": "Verify", "objective": "Check output", "expected_output": "Verified output"} for i in range(2)],
    })
    assert "Saved initial research plan" in result
    assert artifacts.exists("notes/research_plan.json")

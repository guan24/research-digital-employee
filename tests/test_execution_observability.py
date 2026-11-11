from __future__ import annotations

import json
from types import SimpleNamespace

from app.infrastructure.artifact_store import ArtifactStore
from app.runtime.executor import TaskEventCallback
from app.tools.research_tools import ResearchToolService


def make_service(settings, db):
    task = db.create_task("Research a sufficiently detailed topic")
    db.claim_next_task("worker-test")
    store = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], store, __import__("time").monotonic())
    return task, service


def test_structured_plan_is_versioned_and_preserves_step_status(settings, db):
    task, service = make_service(settings, db)
    first = {
        "title": "研究计划",
        "objective": "比较研究对象",
        "steps": [
            {"id": "scope", "title": "明确范围", "objective": "确定范围", "expected_output": "范围说明"},
            {"id": "compare", "title": "开展比较", "objective": "比较架构", "expected_output": "比较表"},
        ],
    }
    service.update_research_plan(json.dumps(first, ensure_ascii=False))
    db.add_event(task["task_id"], "research_plan_step_status", {
        "step_id": "scope", "status": "completed", "source": "agent"
    })
    second = {
        "title": "调整后的研究计划",
        "objective": "补充证据",
        "steps": [
            {"id": "scope", "title": "明确范围", "objective": "确定范围", "expected_output": "范围说明"},
            {"id": "compare", "title": "开展比较", "objective": "比较架构", "expected_output": "比较表"},
            {"id": "verify", "title": "交叉核验", "objective": "核验来源", "expected_output": "核验记录"},
        ],
    }
    service.update_research_plan(json.dumps(second, ensure_ascii=False), "新增来源核验步骤")
    plan_events = [event for event in db.get_events(task["task_id"]) if event["event_type"] == "research_plan_updated"]
    assert len(plan_events) == 2
    assert plan_events[0]["payload"]["version"] == 1
    assert plan_events[1]["payload"]["version"] == 2
    assert plan_events[1]["payload"]["change_reason"] == "新增来源核验步骤"
    assert plan_events[1]["payload"]["plan"]["steps"][0]["status"] == "completed"
    assert service.artifacts.exists("notes/research_plan.json")


def test_structured_plan_rejects_invalid_status(settings, db):
    _, service = make_service(settings, db)
    invalid = {"steps": [{"id": "one", "title": "step", "status": "unknown"}]}
    try:
        service.update_research_plan(json.dumps(invalid))
    except ValueError as exc:
        assert "invalid status" in str(exc)
    else:
        raise AssertionError("invalid plan status must be rejected")


def test_model_callback_persists_tool_only_chat_response(settings, db):
    task, _ = make_service(settings, db)
    callback = TaskEventCallback(db, task["task_id"], __import__("time").monotonic(), 3600)
    response_message = SimpleNamespace(
        content="",
        tool_calls=[{"name": "web_search", "args": {"query": "agent architecture"}, "id": "call-1"}],
        invalid_tool_calls=[],
        additional_kwargs={},
        usage_metadata={"input_tokens": 17, "output_tokens": 9, "total_tokens": 26},
        response_metadata={},
    )
    response = SimpleNamespace(
        generations=[[SimpleNamespace(message=response_message, text="")]],
        llm_output={},
    )
    callback.on_llm_end(response, run_id="run-model-1")
    event = [item for item in db.get_events(task["task_id"]) if item["event_type"] == "model_call_completed"][-1]
    assert event["payload"]["raw_response"]["tool_calls"][0]["name"] == "web_search"
    assert "web_search" in event["payload"]["decision_summary"]
    assert event["payload"]["usage"]["total_tokens"] == 26


def test_sse_formatter_emits_real_event_stream_line_breaks():
    from app.api.routes import _format_sse_event

    frame = _format_sse_event({
        "event_id": 42,
        "event_type": "research_plan_updated",
        "payload": {"title": "计划"},
    })
    assert frame.startswith("id: 42\nevent: task_event\ndata: ")
    assert frame.endswith("\n\n")
    assert "\\\\n" not in frame


def test_artifact_preview_has_markdown_json_render_and_source_modes():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function renderMarkdown(source)" in source
    assert "JSON.stringify(JSON.parse(source), null, 2)" in source
    assert "previewMode === 'rendered'" in source
    assert "previewMode === 'source'" in source
    assert "async function refreshArtifacts(taskId)" in source



def test_events_endpoint_supports_incremental_after_id_polling(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api.main import app

    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={"goal": "Research a sufficiently detailed topic"})
        task_id = created.json()["task_id"]
        db = client.app.state.db
        db.add_event(task_id, "one", {"n": 1})
        first = db.get_events(task_id)[-1]["event_id"]
        db.add_event(task_id, "two", {"n": 2})
        response = client.get(f"/tasks/{task_id}/events?after_id={first}")
        assert response.status_code == 200
        assert [item["event_type"] for item in response.json()] == ["two"]



def test_plan_text_fallback_extracts_markdown_steps(settings, db):
    task, service = make_service(settings, db)
    service.update_research_plan("# 研究计划\n\n1. 明确研究范围\n2. 搜索官方资料\n3. 对比并撰写报告")
    event = [item for item in db.get_events(task["task_id"]) if item["event_type"] == "research_plan_updated"][-1]
    plan = event["payload"]["plan"]
    assert plan["title"] == "执行计划（文本提取）"
    assert [step["title"] for step in plan["steps"]] == ["明确研究范围", "搜索官方资料", "对比并撰写报告"]

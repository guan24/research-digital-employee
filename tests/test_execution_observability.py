from __future__ import annotations

import json
from types import SimpleNamespace

from app.infrastructure.artifact_store import ArtifactStore
from app.runtime.executor import ResearchExecutor, TaskEventCallback
from app.tools.research_tools import ResearchToolService


def make_service(settings, db):
    task = db.create_task("Research a sufficiently detailed topic")
    db.claim_next_task("worker-test")
    store = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], store, __import__("time").monotonic())
    return task, service


def test_plan_history_only_versions_material_replans(settings, db):
    task, service = make_service(settings, db)
    initial = {"title": "研究计划", "objective": "比较研究对象", "steps": [
        {"id": "scope", "title": "明确范围", "objective": "确定范围", "expected_output": "范围说明"},
        {"id": "compare", "title": "开展比较", "objective": "比较架构", "expected_output": "比较表"},
    ]}
    service.update_research_plan(json.dumps(initial, ensure_ascii=False))
    db.add_event(task["task_id"], "research_plan_step_status", {
        "step_id": "scope", "status": "completed", "source": "system"
    })
    status_only = json.loads(json.dumps(initial, ensure_ascii=False))
    status_only["steps"][0]["status"] = "completed"
    result = service.update_research_plan(json.dumps(status_only, ensure_ascii=False))
    assert "No material plan change" in result
    revised = {"title": "补充证据核验后的研究计划", "objective": "比较并验证", "steps": [
        {"id": "scope", "title": "明确范围"},
        {"id": "compare", "title": "开展架构比较"},
        {"id": "verify", "title": "交叉核验关键结论"},
    ]}
    service.update_research_plan(
        json.dumps(revised, ensure_ascii=False),
        trigger="初步检索发现官方资料披露程度差异较大",
        new_information="部分项目只能从仓库和发布说明确认工具机制",
        problem_analysis="原计划无法充分区分已验证能力与推断",
        decision="增加独立证据核验工作流",
        changes="新增官方资料交叉核验步骤并标注证据等级",
    )
    plan_events = [e for e in db.get_events(task["task_id"]) if e["event_type"] == "research_plan_updated"]
    assert len(plan_events) == 2
    payload = plan_events[1]["payload"]
    assert payload["version"] == 2
    assert payload["change_type"] == "replan"
    assert payload["trigger"].startswith("初步检索")
    assert payload["new_information"].startswith("部分项目")
    assert payload["problem_analysis"].startswith("原计划")
    assert payload["decision"].startswith("增加独立")
    assert payload["changes"].startswith("新增官方资料")
    assert payload["plan_diff"]["added_steps"] == [{"id": "verify", "title": "交叉核验关键结论"}]
    assert all("status" not in step for step in payload["plan"]["steps"])
    assert service.artifacts.exists("notes/agent_execution_plan.json")


def test_material_replan_requires_audit_rationale(settings, db):
    _, service = make_service(settings, db)
    initial = {"title": "初始计划", "objective": "研究", "steps": [{"id": "one", "title": "收集资料"}]}
    service.update_research_plan(json.dumps(initial, ensure_ascii=False))
    revised = {"title": "调整计划", "objective": "研究并核验", "steps": [
        {"id": "one", "title": "收集资料"}, {"id": "two", "title": "核验结论"}
    ]}
    try:
        service.update_research_plan(json.dumps(revised, ensure_ascii=False), trigger="发现新问题")
    except ValueError as exc:
        assert "new_information" in str(exc)
        assert "problem_analysis" in str(exc)
        assert "decision" in str(exc)
        assert "changes" in str(exc)
    else:
        raise AssertionError("material replan without an audit trail must be rejected")


def test_final_plan_archive_preserves_all_material_versions(settings, db):
    task, service = make_service(settings, db)
    initial = {"title": "初始计划", "objective": "初始目标", "steps": [
        {"id": "scope", "title": "明确范围"}, {"id": "research", "title": "收集证据"}
    ]}
    service.update_research_plan(json.dumps(initial, ensure_ascii=False))
    revised = {"title": "修订计划", "objective": "补充核验", "steps": [
        {"id": "scope", "title": "明确范围"},
        {"id": "research", "title": "收集证据"},
        {"id": "verify", "title": "交叉核验"},
    ]}
    service.update_research_plan(
        json.dumps(revised, ensure_ascii=False),
        trigger="新证据显示需要交叉核验",
        new_information="部分来源存在版本差异",
        problem_analysis="单一来源无法支持版本相关结论",
        decision="增加第二来源核验",
        changes="新增交叉核验步骤并将版本信息纳入比较维度",
    )
    executor = ResearchExecutor(settings, db)
    archive_path = executor.finalize_plan(task["task_id"], "completed")
    assert archive_path == "notes/agent_execution_plan.json"
    archive = json.loads(service.artifacts.read_text(archive_path))
    assert archive["status"] == "finalized"
    assert archive["outcome"] == "completed"
    assert archive["current_version"] == 2
    assert archive["final_plan"]["title"] == "修订计划"
    assert [item["version"] for item in archive["history"]] == [1, 2]
    assert archive["history"][1]["problem_analysis"] == "单一来源无法支持版本相关结论"
    finalized = [e for e in db.get_events(task["task_id"]) if e["event_type"] == "research_plan_finalized"]
    assert len(finalized) == 1
    assert finalized[0]["payload"]["history_count"] == 2


def test_tool_callbacks_do_not_mutate_plan_step_status(settings, db):
    task, _ = make_service(settings, db)
    callback = TaskEventCallback(db, task["task_id"], __import__("time").monotonic(), 3600)
    callback.on_tool_start({"name": "web_search"}, '{"query":"new evidence"}', run_id="tool-1")
    callback.on_tool_end('{"results":[]}', run_id="tool-1")
    event_types = [event["event_type"] for event in db.get_events(task["task_id"])]
    assert "tool_call_started" in event_types
    assert "tool_call_completed" in event_types
    assert "research_plan_step_status" not in event_types
    assert "research_plan_activity" not in event_types


def test_plan_step_progress_is_separate_from_strategic_versions(settings, db):
    task, service = make_service(settings, db)
    plan = {"title": "研究计划", "objective": "完成研究", "steps": [
        {"id": "scope", "title": "明确范围"}, {"id": "research", "title": "收集证据"}
    ]}
    service.update_research_plan(json.dumps(plan, ensure_ascii=False))
    service.update_plan_step_status("scope", "in_progress", "正在梳理研究边界")
    service.update_plan_step_status("scope", "completed", "范围和评价标准已确定")
    service.update_plan_step_status("research", "in_progress", "正在检索官方资料")
    assert len([e for e in db.get_events(task["task_id"]) if e["event_type"] == "research_plan_updated"]) == 1
    live = json.loads(service.artifacts.read_text("notes/research_plan.json"))
    assert live["steps"][0]["status"] == "completed"
    assert live["steps"][1]["status"] == "in_progress"


def test_plan_progress_replan_preserves_unchanged_and_resets_modified_steps(settings, db):
    task, service = make_service(settings, db)
    initial = {"title": "初始计划", "objective": "研究", "steps": [
        {"id": "scope", "title": "明确范围"}, {"id": "research", "title": "收集证据"}
    ]}
    service.update_research_plan(json.dumps(initial, ensure_ascii=False))
    service.update_plan_step_status("scope", "completed", "范围确认完成")
    service.update_plan_step_status("research", "in_progress", "正在收集资料")
    revised = {"title": "调整计划", "objective": "增加交叉核验", "steps": [
        {"id": "scope", "title": "明确范围"},
        {"id": "research", "title": "重新收集并整理证据"},
        {"id": "verify", "title": "交叉核验关键结论"},
    ]}
    service.update_research_plan(
        json.dumps(revised, ensure_ascii=False),
        trigger="发现部分资料版本不一致",
        new_information="同一功能在不同版本文档中的描述存在差异",
        problem_analysis="原证据收集步骤没有明确版本核验要求",
        decision="扩展证据收集范围并新增交叉核验",
        changes="修改 research 步骤并增加 verify 步骤",
    )
    progress = service._latest_step_progress(db.get_events(task["task_id"]))
    assert progress["scope"]["status"] == "completed"
    assert progress["research"]["status"] == "pending"
    assert progress["verify"]["status"] == "pending"


def test_final_plan_archive_contains_step_progress(settings, db):
    task, service = make_service(settings, db)
    service.update_research_plan(json.dumps({"title":"计划","objective":"研究","steps":[
        {"id":"one","title":"收集资料"},{"id":"two","title":"撰写报告"}]}, ensure_ascii=False))
    service.update_plan_step_status("one", "completed", "资料已收集")
    service.update_plan_step_status("two", "in_progress", "正在撰写")
    archive_path = ResearchExecutor(settings, db).finalize_plan(task["task_id"], "completed")
    archive = json.loads(service.artifacts.read_text(archive_path))
    assert archive["schema_version"] == 2
    assert archive["progress"]["one"]["status"] == "completed"
    assert archive["progress"]["two"]["status"] == "in_progress"
    assert archive["final_plan_with_progress"]["steps"][0]["status"] == "completed"


def test_failed_finalization_closes_in_progress_steps(settings, db):
    task, service = make_service(settings, db)
    service.update_research_plan(json.dumps({"title":"计划","objective":"研究","steps":[
        {"id":"one","title":"收集资料"}]}, ensure_ascii=False))
    service.update_plan_step_status("one", "in_progress", "正在检索")
    with db.transaction() as conn:
        conn.execute("UPDATE tasks SET status='failed', last_error=? WHERE task_id=?",
                     ("API quota exhausted", task["task_id"]))
    archive_path = ResearchExecutor(settings, db).finalize_plan(task["task_id"], "failed")
    archive = json.loads(service.artifacts.read_text(archive_path))
    assert archive["progress"]["one"]["status"] == "failed"
    assert "API quota exhausted" in archive["progress"]["one"]["note"]


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
    assert "preview-table-wrap" in source
    assert "function highlightJson(source)" not in source
    assert "JSON.stringify(JSON.parse(source), null, 2)" not in source
    assert "JSON · 原始文本" in source
    assert "$('previewRendered').hidden = true" in source
    assert "function planHistoryEntry(version)" in source
    assert "let status = traceStatus(item)" in source
    assert "const status = traceStatus(item)" not in source
    assert "Promise.allSettled" in source
    assert "function renderInstanceDiagnostics()" in source
    assert "plan-progress-counts" in source
    assert "problemAnalysis" in source
    assert "newInformation" in source
    assert "research_plan_finalized" in source
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert "#preview{display:block;width:100%;min-width:0;max-width:100%" in css
    assert "#preview>pre.source-code{display:block;width:100%;min-width:0;max-width:100%" in css
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

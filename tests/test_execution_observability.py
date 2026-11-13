from __future__ import annotations

import json
import pytest
from pathlib import Path
from types import SimpleNamespace

from app.infrastructure.artifact_store import ArtifactStore
from app.runtime.executor import ResearchExecutor, TaskEventCallback, extract_final_text
from app.tools.research_tools import ResearchToolService


def make_service(settings, db):
    task = db.create_task("Research a sufficiently detailed topic")
    db.claim_next_task("worker-test")
    store = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], store, __import__("time").monotonic())
    return task, service


def test_instance_diagnostics_defaults_collapsed_and_preserves_user_toggle():
    source = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
    start = source.index("function renderInstanceDiagnostics()")
    end = source.index("\nfunction renderArtifacts()", start)
    renderer = source[start:end]
    assert "const wasOpen = root.querySelector('details.instance-diagnostics')?.open ?? false;" in renderer
    assert "wasOpen?" in renderer and "instance-diagnostics" in renderer
    assert '<details class="instance-diagnostics" open>' not in renderer


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


def test_tool_callbacks_automatically_advance_plan_steps_from_real_work(settings, db):
    from app.runtime.plan_progress import PlanProgressTracker

    task, service = make_service(settings, db)
    service.update_research_plan(json.dumps({
        "title": "研究计划",
        "objective": "完成有证据支持的研究报告",
        "steps": [
            {"id": "discover", "title": "检索官方资料", "expected_output": "官方资料清单"},
            {"id": "read", "title": "阅读关键来源材料", "expected_output": "来源证据摘要"},
            {"id": "analyze", "title": "分析比较研究发现", "expected_output": "比较分析结果"},
            {"id": "report", "title": "撰写最终研究报告", "expected_output": "report.md"},
            {"id": "validate", "title": "核验报告结构与引用", "expected_output": "通过交付校验"},
        ],
    }, ensure_ascii=False))
    tracker = PlanProgressTracker(db, task["task_id"], service.artifacts)
    callback = TaskEventCallback(
        db, task["task_id"], __import__("time").monotonic(), 3600,
        plan_progress_service=tracker,
    )

    callback.on_tool_start({"name": "web_search"}, '{"query":"official documentation"}', run_id="tool-search")
    callback.on_tool_end('{"results":[{"title":"Official docs"}]}', run_id="tool-search")
    callback.on_tool_start({"name": "fetch_url"}, '{"url":"https://example.org/docs"}', run_id="tool-read")
    callback.on_tool_end('{"success":true,"text":"source text"}', run_id="tool-read")
    callback.on_tool_start({"name": "record_finding"}, '{"claim":"verified finding","source_ids":["S0001"]}', run_id="tool-analysis")
    callback.on_tool_end('{"finding_id":"F0001"}', run_id="tool-analysis")
    callback.on_tool_start({"name": "save_artifact"}, '{"path":"report.md","content":"# Report"}', run_id="tool-report")
    callback.on_tool_end('Saved report.md', run_id="tool-report")
    service.artifacts.write_text("report.md", "# Verified report\\n\\nFinal deliverable.")
    callback.sync_plan_progress_for_phase("validation", "系统开始交付校验")
    callback.complete_active_plan_step("报告已通过校验并交付", output_path="report.md")

    events = db.get_events(task["task_id"])
    progress_events = [event for event in events if event["event_type"] == "research_plan_step_status"]
    latest = service._latest_step_progress(events)
    assert [latest[key]["status"] for key in ("discover", "read", "analyze", "report", "validate")] == [
        "awaiting_verification", "awaiting_verification", "awaiting_verification",
        "completed", "completed",
    ]
    assert latest["report"]["verified_output"]["path"] == "report.md"
    assert latest["validate"]["verified_output"]["path"] == "report.md"
    assert all(latest[key]["source"] == "runtime" for key in latest)
    assert [event["event_type"] for event in events].count("tool_call_started") == 4
    assert [event["event_type"] for event in events].count("tool_call_completed") == 4
    assert len(progress_events) >= 9
    assert "research_plan_activity" not in [event["event_type"] for event in events]


def test_report_phase_marks_analysis_as_awaiting_verification_without_an_artifact(settings, db):
    from app.runtime.plan_progress import PlanProgressTracker

    task, service = make_service(settings, db)
    service.update_research_plan(json.dumps({
        "title": "研究计划",
        "objective": "完成研究报告",
        "steps": [
            {"id": "discover", "title": "检索资料"},
            {"id": "read", "title": "阅读关键来源"},
            {"id": "analyze", "title": "分析并比较研究发现"},
            {"id": "report", "title": "撰写研究报告", "expected_output": "report.md"},
            {"id": "validate", "title": "核验报告引用"},
        ],
    }, ensure_ascii=False))
    tracker = PlanProgressTracker(db, task["task_id"], service.artifacts)
    callback = TaskEventCallback(
        db, task["task_id"], __import__("time").monotonic(), 3600,
        plan_progress_service=tracker,
    )
    callback.on_tool_start({"name": "web_search"}, '{"query":"research"}', run_id="search")
    callback.on_tool_end('{"results":[]}', run_id="search")
    callback.on_tool_start({"name": "fetch_url"}, '{"url":"https://example.org"}', run_id="read")
    callback.on_tool_end('{"success":true}', run_id="read")
    callback.on_tool_start({"name": "save_artifact"}, '{"path":"report.md","content":"# Report"}', run_id="report")

    latest = service._latest_step_progress(db.get_events(task["task_id"]))
    assert latest["discover"]["status"] == "awaiting_verification"
    assert latest["read"]["status"] == "awaiting_verification"
    assert latest["analyze"]["status"] == "awaiting_verification"
    assert latest["report"]["status"] == "in_progress"
    assert latest["validate"]["status"] == "pending"


def test_progress_tracker_does_not_override_explicit_agent_status(settings, db):
    from app.runtime.plan_progress import PlanProgressTracker

    task, service = make_service(settings, db)
    service.update_research_plan(json.dumps({
        "title": "计划", "objective": "研究",
        "steps": [{"id": "research", "title": "检索并阅读资料"}],
    }, ensure_ascii=False))
    service.update_plan_step_status("research", "in_progress", "Agent 明确报告正在执行")
    tracker = PlanProgressTracker(db, task["task_id"], service.artifacts)
    callback = TaskEventCallback(
        db, task["task_id"], __import__("time").monotonic(), 3600,
        plan_progress_service=tracker,
    )
    callback.on_tool_start({"name": "web_search"}, '{"query":"test"}', run_id="tool-explicit")
    callback.on_tool_end('{"results":[]}', run_id="tool-explicit")

    latest = service._latest_step_progress(db.get_events(task["task_id"]))
    assert latest["research"]["status"] == "in_progress"
    assert latest["research"]["source"] == "agent"
def test_plan_step_progress_is_separate_from_strategic_versions(settings, db):
    task, service = make_service(settings, db)
    plan = {"title": "研究计划", "objective": "完成研究", "steps": [
        {"id": "scope", "title": "明确范围"}, {"id": "research", "title": "收集证据"}
    ]}
    service.update_research_plan(json.dumps(plan, ensure_ascii=False))
    service.update_plan_step_status("scope", "in_progress", "正在梳理研究边界")
    with pytest.raises(ValueError, match="requires an existing"):
        service.update_plan_step_status("scope", "completed", "范围和评价标准已确定")
    service.artifacts.write_text("notes/scope.md", "# Scope\\n\\n范围和评价标准已确定。")
    service.update_plan_step_status(
        "scope", "completed", "范围和评价标准已确定", output_path="notes/scope.md",
    )
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
    service.artifacts.write_text("notes/scope.md", "# Scope\\n\\n范围确认完成。")
    service.update_plan_step_status("scope", "completed", "范围确认完成", output_path="notes/scope.md")
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
    service.artifacts.write_text("notes/source-list.md", "# Sources\\n\\n资料已收集。")
    service.update_plan_step_status(
        "one", "completed", "资料已收集", output_path="notes/source-list.md",
    )
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

def test_model_callback_surfaces_malformed_tool_arguments_and_anomaly_event(settings, db):
    task, _ = make_service(settings, db)
    callback = TaskEventCallback(db, task["task_id"], __import__("time").monotonic(), 3600)
    message = SimpleNamespace(
        content="",
        tool_calls=[],
        invalid_tool_calls=[{
            "name": "update_research_plan",
            "args": "{\"steps\":[",
            "error": "arguments were malformed or truncated",
        }],
        additional_kwargs={"refusal": None},
        response_metadata={"finish_reason": "length"},
        usage_metadata={"input_tokens": 100, "output_tokens": 2048, "total_tokens": 2148},
    )
    callback.on_llm_end(
        SimpleNamespace(generations=[[SimpleNamespace(message=message, text="")]], llm_output={}),
        run_id="invalid-tool-call",
    )
    events = db.get_events(task["task_id"])
    completed = [event for event in events if event["event_type"] == "model_call_completed"][-1]
    anomaly = [event for event in events if event["event_type"] == "model_response_anomaly"][-1]

    assert "无法解析的工具调用" in completed["payload"]["decision_summary"]
    assert "malformed or truncated" in completed["payload"]["decision_summary"]
    assert anomaly["payload"]["kind"] == "invalid_tool_calls"


def test_model_callback_distinguishes_a_true_empty_response_from_tool_only_turn(settings, db):
    task, _ = make_service(settings, db)
    callback = TaskEventCallback(db, task["task_id"], __import__("time").monotonic(), 3600)
    message = SimpleNamespace(
        content="",
        tool_calls=[],
        invalid_tool_calls=[],
        additional_kwargs={"refusal": None},
        response_metadata={},
        usage_metadata={"input_tokens": 11, "output_tokens": 0, "total_tokens": 11},
    )
    callback.on_llm_end(
        SimpleNamespace(generations=[[SimpleNamespace(message=message, text="")]], llm_output={}),
        run_id="empty-response",
    )
    events = db.get_events(task["task_id"])
    completed = [event for event in events if event["event_type"] == "model_call_completed"][-1]
    anomaly = [event for event in events if event["event_type"] == "model_response_anomaly"][-1]

    assert "模型返回空响应" in completed["payload"]["decision_summary"]
    assert completed["payload"]["response_kind"] == "empty"
    assert anomaly["payload"]["kind"] == "empty"


def test_final_text_extraction_does_not_fall_back_to_an_earlier_progress_message():
    result = {"messages": [
        {"role": "assistant", "content": "I will start the research now."},
        {"role": "tool", "content": "Evidence was saved."},
        {"role": "assistant", "content": ""},
    ]}
    assert extract_final_text(result) == ""

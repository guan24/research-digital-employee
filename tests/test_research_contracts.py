import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from pydantic import Field, ValidationError

from app.agents.research.agent import build_research_agent
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.errors import ClassifiedTaskError
from app.runtime.executor import TaskEventCallback, _serialize_model_response, _model_response_kind, _model_response_summary
from app.runtime.research_safety import model_output_guard
from app.tools.research_schemas import ResearchRequirementsInput, ResearchPlan
from app.tools.research_tools import ResearchToolService


def requirements():
    return {"dimensions": [{"id": f"d{i}", "title": "Storage", "questions": ["What persists?"],
                            "expected_evidence": ["Official documentation"], "priority": "normal", "min_sources": 1}
                           for i in range(3)]}


def plan(n=3):
    return {"title": "Storage", "objective": "Compare persistence", "steps": [
        {"id": f"s{i}", "title": "Compare", "objective": "Verify ownership", "expected_output": "Evidence"}
        for i in range(n)]}


def service(settings, db):
    task = db.create_task("Validate research contracts offline", max_steps=20, max_total_tokens=100000)
    assert db.acquire_worker_lease("contract-worker", 3600)
    task = db.claim_next_task("contract-worker")
    store = ArtifactStore(settings.workspace_dir, task["task_id"])
    return task, ResearchToolService(settings, db, task["task_id"], store, time.monotonic())


@pytest.mark.parametrize("change", [
    lambda v: v["dimensions"][0].update(expected_evidence="text"),
    lambda v: v["dimensions"][0].update(questions="text"),
    lambda v: v["dimensions"][0].pop("title"),
    lambda v: v["dimensions"][0].update(priority="urgent"),
    lambda v: v.update(dimensions=v["dimensions"][:2]),
    lambda v: v.update(dimensions=v["dimensions"] * 5),
    lambda v: v.update(quality_policy={"unknown": 10}),
    lambda v: v.update(quality_policy={"min_total_score": 101}),
    lambda v: v["dimensions"][0].update(id=" "),
    lambda v: v["dimensions"][0].update(id="x" * 65),
    lambda v: v["dimensions"][0].update(min_sources=11),
])
def test_requirements_reject_invalid_nested_values(change):
    value = requirements()
    change(value)
    with pytest.raises(ValidationError):
        ResearchRequirementsInput.model_validate(value)


def test_public_nested_schema_and_valid_input(settings, db):
    _, svc = service(settings, db)
    tools = {t.name: t for t in svc.as_langchain_tools()}
    schema = tools["define_research_requirements"].args_schema.model_json_schema()
    dimension = schema["$defs"]["ResearchDimension"]
    assert dimension["properties"]["expected_evidence"]["items"]["type"] == "string"
    assert dimension["properties"]["priority"]["enum"] == ["critical", "high", "normal"]
    assert set(dimension["required"]) == {"id", "title", "questions", "expected_evidence", "priority", "min_sources"}
    assert schema["properties"]["dimensions"]["minItems"] == 3
    assert schema["properties"]["dimensions"]["maxItems"] == 12
    assert schema["$defs"]["ResearchQualityPolicy"]["additionalProperties"] is False
    assert ResearchRequirementsInput.model_validate(requirements()).dimensions[0].expected_evidence == ["Official documentation"]
    assert json.loads(tools["define_research_requirements"].invoke(requirements()))["version"] == 1


@pytest.mark.parametrize("n", [3, 8])
def test_complete_plan_is_persisted(settings, db, n):
    task, svc = service(settings, db)
    svc.update_research_plan(json.dumps(plan(n)))
    saved = json.loads(svc.artifacts.read_text("notes/research_plan.json"))
    assert len(saved["steps"]) == n and saved["version"] == 1
    assert len([e for e in db.get_events(task["task_id"]) if e["event_type"] == "research_plan_updated"]) == 1


@pytest.mark.parametrize("change", [
    lambda v: v.update(steps=v["steps"][:1]), lambda v: v.update(steps=v["steps"][:2]),
    lambda v: v.update(steps=plan(9)["steps"]),
    *[lambda v, key=k: v["steps"][0].pop(key) for k in ["id", "title", "objective", "expected_output"]],
    lambda v: v["steps"][1].update(id="s0"),
])
def test_invalid_plan_never_changes_old_plan_or_version(settings, db, change):
    task, svc = service(settings, db)
    svc.update_research_plan(json.dumps(plan()))
    old = svc.artifacts.read_text("notes/research_plan.json")
    events = copy.deepcopy(db.get_events(task["task_id"]))
    value = plan()
    change(value)
    with pytest.raises(ValidationError):
        svc.update_research_plan(json.dumps(value))
    assert svc.artifacts.read_text("notes/research_plan.json") == old
    assert db.get_events(task["task_id"]) == events


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-contract-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens(self, text):
        return max(1, len(text) // 4)

    def _generate(self, messages, **kwargs):
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=self.responses[len(self.seen) - 1])])


def call(name, args, index, finish="tool_calls"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": str(index), "type": "tool_call"}],
                     response_metadata={"finish_reason": finish})


@pytest.mark.parametrize("name,args", [
    ("set_active_plan_step", {"step_id": "write-report"}),
    ("update_plan_step_status", {"step_id": "write-report", "status": "in_progress"}),
    ("update_plan_step_status", {"step_id": "s0", "status": "invented-status"}),
    ("update_plan_step_status", {"step_id": "s0", "status": "completed"}),
    ("set_active_plan_step", {"step_id": ["wrong type"]}),
    ("set_active_plan_step", {}),
])
def test_step_argument_failure_returns_current_plan_without_changing_state(settings, db, name, args):
    task, svc = service(settings, db)
    svc.update_research_plan(json.dumps(plan()))
    old_plan = svc.artifacts.read_text("notes/research_plan.json")
    old_progress = [e for e in db.get_events(task["task_id"]) if e["event_type"] in {
        "research_plan_updated", "research_plan_step_status", "research_plan_step_focus_changed"}]
    tools = {t.name: t for t in svc.as_langchain_tools()}
    from app.runtime.plan_progress import PlanProgressTracker
    callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60,
        plan_progress_service=PlanProgressTracker(db, task["task_id"], svc.artifacts))
    result = json.loads(tools[name].invoke(args, config={"callbacks": [callback]}))
    assert result["success"] is False and result["recoverable"] is True
    assert result["error_type"] == "plan_step_validation" and result["tool"] == name
    assert result["errors"] and result["suggestion"]
    assert result["current_plan"]["version"] == 1
    assert [s["id"] for s in result["current_plan"]["steps"]] == ["s0", "s1", "s2"]
    assert svc.artifacts.read_text("notes/research_plan.json") == old_plan
    assert [e for e in db.get_events(task["task_id"]) if e["event_type"] in {
        "research_plan_updated", "research_plan_step_status", "research_plan_step_focus_changed"}] == old_progress


@pytest.mark.parametrize("decision", ["correct_id", "revise_plan", "stop"])
def test_production_model_decides_how_to_handle_step_error(settings, db, decision):
    task, svc = service(settings, db)
    svc.update_research_plan(json.dumps(plan()))
    bad = call("set_active_plan_step", {"step_id": "write-report"}, 1)
    if decision == "revise_plan":
        revised = plan()
        revised["steps"][2]["id"] = "write-report"
        revised.update(trigger="New report workstream", new_information="Operator requested a distinct report step",
                       problem_analysis="Existing IDs do not describe the intended workstream", decision="Revise the complete plan",
                       changes="Replace s2 with write-report")
        responses = [bad, call("update_research_plan", revised, 2),
                     call("set_active_plan_step", {"step_id": "write-report"}, 3), AIMessage(content="Done")]
    elif decision == "correct_id":
        responses = [bad, call("set_active_plan_step", {"step_id": "s2"}, 2), AIMessage(content="Done")]
    else:
        responses = [bad, AIMessage(content="Stop here and explain the limitation")]
    model = ScriptedModel(responses=responses)
    agent = build_research_agent(settings, db, task, None, time.monotonic(), model=model)
    callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60)
    agent.invoke({"messages": [HumanMessage(content="Work on the report step")]}, {"callbacks": [callback]})
    feedback = next(m for m in model.seen[1] if isinstance(m, ToolMessage) and m.name == "set_active_plan_step")
    assert json.loads(feedback.content)["current_plan"]["steps"][2]["id"] == "s2"
    events = db.get_events(task["task_id"])
    assert len([e for e in events if e["event_type"] == "tool_call_failed"]) == 1
    focus = [e for e in events if e["event_type"] == "research_plan_step_focus_changed"]
    if decision == "stop":
        assert focus == [] and len(model.seen) == 2
    else:
        assert focus[-1]["payload"]["step_id"] == ("s2" if decision == "correct_id" else "write-report")
    plans = [e for e in events if e["event_type"] == "research_plan_updated"]
    assert len(plans) == (2 if decision == "revise_plan" else 1)


def test_repeated_step_errors_still_obey_tool_budget(settings, db):
    from app.runtime.control import TaskStepLimitExceeded
    task = db.create_task("Report workstream", max_steps=2, max_total_tokens=100000)
    assert db.acquire_worker_lease("step-budget-worker", 3600)
    task = db.claim_next_task("step-budget-worker")
    store = ArtifactStore(settings.workspace_dir, task["task_id"])
    svc = ResearchToolService(settings, db, task["task_id"], store, time.monotonic())
    svc.update_research_plan(json.dumps(plan()))
    model = ScriptedModel(responses=[call("set_active_plan_step", {"step_id": "write-report"}, i) for i in range(5)])
    agent = build_research_agent(settings, db, task, None, time.monotonic(), model=model)
    callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60)
    with pytest.raises(TaskStepLimitExceeded):
        agent.invoke({"messages": [HumanMessage(content="Work on the report")]}, {"callbacks": [callback]})
    assert db.get_task(task["task_id"])["execution_steps"] == 2
    assert len([e for e in db.get_events(task["task_id"]) if e["event_type"] == "research_plan_step_validation_failed"]) == 2


@pytest.mark.parametrize("signal_name", ["TaskPaused", "TaskCancelled", "TaskDeadlineExceeded", "WorkerLeaseLost"])
def test_step_feedback_never_swallows_task_control_signals(settings, db, monkeypatch, signal_name):
    from app.runtime import control
    task, svc = service(settings, db)
    signal = getattr(control, signal_name)
    def stopped():
        raise signal("Execution must stop")
    monkeypatch.setattr(svc, "check_control", stopped)
    tools = {t.name: t for t in svc.as_langchain_tools()}
    with pytest.raises(signal):
        tools["set_active_plan_step"].invoke({"step_id": "write-report"})
    assert not any(e["event_type"] == "research_plan_step_validation_failed" for e in db.get_events(task["task_id"]))


@pytest.mark.parametrize("correct", [True, False])
def test_production_graph_returns_schema_errors_and_bounds_correction(settings, db, correct):
    task, _ = service(settings, db)
    bad = requirements()
    bad["dimensions"][0]["expected_evidence"] = "wrong type"
    model = ScriptedModel(responses=[call("define_research_requirements", bad, 1),
                                    call("define_research_requirements", requirements() if correct else bad, 2),
                                    AIMessage(content="Done")])
    agent = build_research_agent(settings, db, task, None, time.monotonic(), model=model)
    callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60)
    config = {"callbacks": [callback], "configurable": {"thread_id": task["task_id"]}}
    if correct:
        agent.invoke({"messages": [{"role": "user", "content": "Define the research requirements"}]}, config)
        assert db.get_research_control_state(task["task_id"])["requirements_version"] == 1
        assert len([e for e in db.get_events(task["task_id"]) if e["event_type"] == "research_requirements_updated"]) == 1
    else:
        with pytest.raises(ClassifiedTaskError, match="correction exhausted"):
            agent.invoke({"messages": [{"role": "user", "content": "Define research requirements"}]}, config)
        assert not db.get_research_control_state(task["task_id"])
        assert len(model.seen) == 2
    feedback = [m for m in model.seen[1] if isinstance(m, ToolMessage)][-1]
    error = json.loads(feedback.content)
    assert error["success"] is False and error["attempt"] == 1 and error["retryable"] is True
    assert error["errors"][0]["path"] == "dimensions.0.expected_evidence"
    assert error["errors"][0]["expected"] and error["errors"][0]["suggestion"]


class HumanInstructionModel(BaseChatModel):
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-human-instruction-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens(self, text):
        return max(1, len(text) // 4)

    def _generate(self, messages, **kwargs):
        self.seen.append(messages)
        by_name = {}
        for message in messages:
            if isinstance(message, ToolMessage):
                by_name.setdefault(message.name, []).append(message)
        human_text = " ".join(
            str(message.content) for message in messages if isinstance(message, HumanMessage)
        )
        if "read_research_memory" not in by_name:
            response = call("read_research_memory", {}, 100)
        elif "get_budget_status" not in by_name:
            response = call("get_budget_status", {}, 101)
        elif "get_human_decisions" not in by_name:
            response = call("get_human_decisions", {}, 102)
        elif "get_research_status" not in by_name:
            response = call("get_research_status", {}, 103)
        elif "define_research_requirements" not in by_name:
            assert "请补充安全边界" in human_text
            status = json.loads(by_name["get_research_status"][-1].content)
            existing = status["requirements"]["dimensions"]
            response = call("define_research_requirements", {
                "dimensions": existing + [{
                    "id": "security-boundary",
                    "title": "安全边界与权限隔离",
                    "questions": ["系统如何隔离权限并保护敏感数据？"],
                    "expected_evidence": ["官方安全文档与权限设计资料"],
                    "priority": "critical",
                    "min_sources": 2,
                }]
            }, 104)
        elif "update_research_plan" not in by_name:
            revised = plan()
            revised["title"] = "Storage and security"
            revised["objective"] = "Compare persistence and the security boundary"
            revised["steps"].append({
                "id": "security",
                "title": "Security boundary",
                "objective": "Verify permission isolation and sensitive-data handling",
                "expected_output": "Evidence-backed security boundary analysis",
            })
            revised.update({
                "change_reason": "Apply operator's additional security research requirement",
                "trigger": "Operator supplied a natural-language requirement",
                "new_information": "The operator requested security-boundary and permission-isolation analysis.",
                "problem_analysis": "The existing plan did not cover the requested security boundary.",
                "decision": "Add a dedicated security workstream while preserving completed workstreams.",
                "changes": "Keep the existing three steps and append a security analysis step.",
            })
            response = call("update_research_plan", revised, 105)
        else:
            response = AIMessage(content="已根据人工指令更新研究需求矩阵和执行计划。")
        return ChatResult(generations=[ChatGeneration(message=response)])


def test_agent_interprets_human_natural_language_and_updates_requirements_and_plan(settings, db):
    task, service_instance = service(settings, db)
    tools = {tool.name: tool for tool in service_instance.as_langchain_tools()}
    tools["define_research_requirements"].invoke(requirements())
    tools["update_research_plan"].invoke(plan())

    instruction = "请补充安全边界和权限隔离机制的研究，并优先核验官方文档。"
    db.add_event(task["task_id"], "human_intervention_resolved", {
        "intervention_id": "human-natural-language-test",
        "action": "add_research_requirements",
        "payload": {"instruction": instruction, "reason": ""},
        "request": {"message": "请补充研究要求。"},
    })

    model = HumanInstructionModel()
    agent = build_research_agent(
        settings, db, task, None, time.monotonic(), model=model
    )
    agent.invoke({
        "messages": [HumanMessage(
            id="human-intervention-human-natural-language-test",
            content="用户自然语言指令：" + instruction,
        )]
    }, config={"configurable": {"thread_id": task["task_id"]}})

    state = db.get_research_control_state(task["task_id"])
    dimensions = state["requirements"]["dimensions"]
    assert [item["id"] for item in dimensions[:3]] == ["d0", "d1", "d2"]
    assert dimensions[-1]["id"] == "security-boundary"
    assert state["requirements"]["source"] == "agent"
    saved_plan = json.loads(service_instance.artifacts.read_text("notes/research_plan.json"))
    assert saved_plan["version"] == 2
    assert saved_plan["steps"][-1]["id"] == "security"
    assert any(
        event["event_type"] == "research_requirements_updated"
        and event["payload"]["source"] == "agent"
        for event in db.get_events(task["task_id"])
    )
    assert any(
        event["event_type"] == "research_plan_updated"
        for event in db.get_events(task["task_id"])
    )
    assert len(model.seen) >= 7


@pytest.mark.parametrize("message,kind", [
    (AIMessage(content="complete", response_metadata={"finish_reason": "stop"}), "text"),
    (call("update_research_plan", plan(), 1), "tool_calls"),
    (AIMessage(content="partial", response_metadata={"finish_reason": "length"}), "truncated"),
    (call("update_research_plan", plan(2), 1, "length"), "truncated"),
    (AIMessage(content="", invalid_tool_calls=[{"name": "update_research_plan", "args": '{"steps":[', "id": "1", "error": "invalid JSON"}], response_metadata={"finish_reason": "length"}), "truncated"),
])
def test_callback_identifies_truncation_before_content_and_calls(settings, db, message, kind):
    task, _ = service(settings, db)
    response = LLMResult(generations=[[ChatGeneration(message=message)]])
    raw = _serialize_model_response(response)
    assert _model_response_kind(raw) == kind
    callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60)
    callback.on_llm_end(response, run_id="response")
    completed = [e for e in db.get_events(task["task_id"]) if e["event_type"] == "model_call_completed"][-1]
    assert completed["payload"]["raw_response"] == raw
    if kind == "truncated":
        assert "truncated" in _model_response_summary(raw)
        assert any(e["event_type"] == "model_response_anomaly" for e in db.get_events(task["task_id"]))


@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("variant", ["partial_tool", "complete_tool", "body", "invalid_tool"])
def test_truncated_tool_calls_cannot_persist_before_bounded_retry(settings, db, repeat, variant):
    task, svc = service(settings, db)
    first = call("update_research_plan", plan(2), 1, "length")
    if variant == "complete_tool":
        first = call("update_research_plan", plan(), 1, "length")
    elif variant == "body":
        first = AIMessage(content="Partial plan", response_metadata={"finish_reason": "length"})
    elif variant == "invalid_tool":
        first = AIMessage(content="", invalid_tool_calls=[{"name": "update_research_plan", "args": '{"steps":[', "id": "1", "error": "truncated JSON"}], response_metadata={"finish_reason": "length"})
    second = call("update_research_plan", plan(2) if repeat else plan(), 2, "length" if repeat else "tool_calls")
    model = ScriptedModel(responses=[first, second, AIMessage(content="Done")])
    agent = build_research_agent(settings, db, task, None, time.monotonic(), model=model)
    if repeat:
        with pytest.raises(ClassifiedTaskError, match="remained truncated"):
            agent.invoke({"messages": [{"role": "user", "content": "Plan the research"}]})
        assert not svc.artifacts.exists("notes/research_plan.json")
        assert len(model.seen) == 2
    else:
        agent.invoke({"messages": [{"role": "user", "content": "Plan the research"}]})
        assert len(json.loads(svc.artifacts.read_text("notes/research_plan.json"))["steps"]) == 3
    events = db.get_events(task["task_id"])
    assert len([e for e in events if e["event_type"] == "research_plan_updated"]) == (0 if repeat else 1)
    assert any(e["event_type"] == "model_output_truncated" for e in events)


def test_truncation_correction_reserves_budget_before_second_model_call(settings, db, monkeypatch):
    from app.runtime.errors import BudgetExhausted
    task = db.create_task("Plan with a strict token budget", max_total_tokens=100)
    assert db.acquire_worker_lease("budget-worker", 3600)
    task = db.claim_next_task("budget-worker")
    first = call("update_research_plan", plan(2), 1, "length")
    first.usage_metadata = {"input_tokens": 20, "output_tokens": 30, "total_tokens": 50}
    model = ScriptedModel(responses=[first, call("update_research_plan", plan(), 2)])
    monkeypatch.setattr("app.runtime.executor.estimate_model_call_reservation", lambda *a, **kw: 80)
    callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60)
    agent = build_research_agent(settings, db, task, None, time.monotonic(), model=model)
    with pytest.raises(BudgetExhausted):
        agent.invoke({"messages": [{"role": "user", "content": "Plan"}]}, {"callbacks": [callback]})
    assert len(model.seen) == 1
    store = ArtifactStore(settings.workspace_dir, task["task_id"])
    assert not store.exists("notes/research_plan.json")


def test_async_truncation_uses_same_bounded_guard(settings, db):
    task, _ = service(settings, db)
    guard = model_output_guard(db, task["task_id"])
    requests = []
    async def handler(request):
        requests.append(request)
        return ModelResponse(result=[AIMessage(content="partial", response_metadata={"finish_reason": "length"})])
    request = SimpleNamespace(messages=[], override=lambda **kw: SimpleNamespace(**kw))
    with pytest.raises(ClassifiedTaskError):
        asyncio.run(guard.awrap_model_call(request, handler))
    assert len(requests) == 2



def test_parallel_finding_writes_are_not_lost(settings, db, monkeypatch):
    _, svc = service(settings, db)
    registry = EvidenceRegistry(svc.artifacts)
    source = registry.register_source(
        url="https://example.com/parallel-evidence",
        title="Parallel evidence",
        content="One verified source supports multiple separately recorded dimensions.",
    )

    original_save = EvidenceRegistry._save

    def delayed_save(self, payload):
        # Widen the read/modify/write race window so an unlocked implementation
        # deterministically loses updates when independent tool calls overlap.
        time.sleep(0.02)
        return original_save(self, payload)

    monkeypatch.setattr(EvidenceRegistry, "_save", delayed_save)

    def record(index):
        return EvidenceRegistry(svc.artifacts).record_finding(
            claim=f"Finding {index}",
            source_ids=[source["source_id"]],
            dimension_id=f"dimension-{index}",
            rationale="Concurrent write regression test",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        written = list(pool.map(record, range(8)))

    final = EvidenceRegistry(svc.artifacts).summary()
    assert final["finding_count"] == 8
    assert len({item["finding_id"] for item in final["findings"]}) == 8
    assert {item["dimension_id"] for item in final["findings"]} == {
        f"dimension-{index}" for index in range(8)
    }
    assert len(written) == 8

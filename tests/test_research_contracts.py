import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
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
def test_truncated_tool_calls_cannot_persist_before_bounded_retry(settings, db, repeat):
    task, svc = service(settings, db)
    first = call("update_research_plan", plan(2), 1, "length")
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

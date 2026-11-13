import json
from dataclasses import replace

import pytest
import httpx
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.executor import ResearchExecutor
from app.runtime.worker import Worker
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

GOAL = "Compare Coding Agent context management architectures"
PLAN = {"title": "Context architecture", "objective": GOAL, "steps": [
    {"id": "search", "title": "Search architecture", "objective": "Compare architecture", "expected_output": "Evidence summary", "expected_output_path": "notes/search.md"},
    {"id": "context", "title": "Compare checkpoint and memory", "objective": "Compare architecture", "expected_output": "Evidence summary", "expected_output_path": "notes/context.md"},
    {"id": "scope", "title": "Define scope", "objective": "Set boundaries", "expected_output": "Scope", "expected_output_path": ""},
]}


class ConditionalResearchModel(BaseChatModel):
    trace: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-conditional-research"

    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens(self, text):
        return max(1, len(text) // 4)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        observed = [m for m in messages if isinstance(m, ToolMessage)]
        by_name = {}
        for message in observed:
            by_name.setdefault(message.name, []).append(message)
        pages = [json.loads(m.content) for m in by_name.get("fetch_url", [])]
        evidence = "\n".join(p.get("text", "") for p in pages)
        contradiction = "CONTRADICTS_HYPOTHESIS:" in evidence
        plans = by_name.get("update_research_plan", [])
        humans = [m for m in messages if isinstance(m, HumanMessage)]
        repair = any("质量门禁未通过" in str(m.content) or "修复" in str(m.content) for m in humans)
        source = pages[0]["source_id"] if pages else ""
        def choose(name, **args):
            self.trace.append({"hypothesis": "Checkpoint may preserve both execution and durable memory; verify this assumption",
                               "evidence": evidence[:400], "contradiction": contradiction,
                               "replan": name == "update_research_plan" and bool(plans),
                               "reason": "Observed primary evidence contradicts assumption" if contradiction else "No observed contradiction",
                               "action": name})
            return call(name, **args)
        if not plans:
            response = choose("update_research_plan", **PLAN)
        elif "define_research_requirements" not in by_name:
            response = choose("define_research_requirements", dimensions=[{"id": dim, "title": title, "questions": ["What does checkpoint persist?"], "expected_evidence": ["Primary source"], "priority": "critical" if dim == "context" else "normal", "min_sources": 1} for dim, title in [("context", "Context"), ("scope", "Scope"), ("lifecycle", "Lifecycle")]])
        elif "web_search" not in by_name:
            response = choose("web_search", query="agent context architecture")
        elif "update_plan_step_status" not in by_name:
            search = json.loads(by_name["web_search"][-1].content)
            path = "sources/" + search["saved_to"].replace("\\", "/").split("/sources/")[-1]
            response = choose("update_plan_step_status", step_id="search", status="completed", output_path=path)
        elif not pages:
            search = json.loads(by_name["web_search"][-1].content)
            response = choose("fetch_url", url=search["results"][0]["url"])
        elif "get_research_status" not in by_name:
            response = choose("get_research_status")
        elif contradiction and len(plans) == 1:
            revised = json.loads(json.dumps(PLAN))
            revised["steps"][1]["title"] = "Inspect separate long-term memory storage"
            response = choose("update_research_plan", **revised,
                trigger="Contradictory primary evidence", new_information=f"{source}: {pages[0]['text'][:300]}",
                problem_analysis="Checkpoint alone does not preserve long-term memory", decision="Research the independent store",
                changes="Replace context step with separate memory investigation", change_reason="Observed source contradicts hypothesis")
        elif contradiction and "set_active_plan_step" not in by_name:
            response = choose("set_active_plan_step", step_id="context")
        elif not by_name.get("record_finding"):
            coverage = json.loads(by_name["get_research_status"][-1].content)["coverage"]
            if contradiction and len(pages) < 2 and coverage["coverage_ratio"] < 1:
                response = choose("fetch_url", url=pages[0]["url"] + "?detail=memory")
            else:
                response = choose("record_finding", claim="Checkpoint execution and durable memory have separate ownership", source_ids=[source],
                    dimension_id="context", confidence="high", rationale=pages[0]["text"][:300])
                for dimension in ("scope", "lifecycle"):
                    response.tool_calls.append({"name": "record_finding", "id": dimension + "-finding", "type": "tool_call",
                                                "args": {"claim": "The source defines persistence ownership and lifecycle", "source_ids": [source],
                                                         "dimension_id": dimension, "confidence": "high", "rationale": pages[0]["text"][:300]}})
        elif len(by_name.get("get_research_status", [])) < 2:
            response = choose("get_research_status")
        elif json.loads(by_name["get_research_status"][-1].content)["coverage"]["coverage_ratio"] < 1:
            raise AssertionError("Model cannot deliver with an uncovered research dimension")
        else:
            report = "# Agent context architecture\n\n" + "\n\n".join(
                "## " + heading + "\n\n" + (f"Primary evidence separates graph execution from durable memory [{source}]. Context ownership and recovery should be compared independently. " * 6)
                for heading in ["Scope and methodology", "Context findings", "Comparison and analysis", "Limitations", "Conclusion"]
            ) + "\n\n## Sources\n- " + pages[0]["url"]
            if repair:
                # Revision count is derived from feedback messages, not invocation count.
                report += "\n\nRepair: clarified context ownership. " + str(len(humans)) + f" [{source}]"
                last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
                if not any(isinstance(m, ToolMessage) and m.name == "save_artifact" for m in messages[last_human:]):
                    response = choose("save_artifact", path="report.md", content=report)
                else:
                    response = AIMessage(content="Repaired report delivered.")
            else:
                response = AIMessage(content=report)
        assert any(isinstance(m, SystemMessage) for m in messages), "Production prompt must be retained"
        return ChatResult(generations=[ChatGeneration(message=response)])


@pytest.fixture
def run_research(settings, db, monkeypatch):
    from app.agents.research.agent import build_research_agent
    original_client = httpx.Client
    def run(marker, review_mode="repair", model=None):
        model = model or ConditionalResearchModel()
        def respond(request):
            if request.method == "POST":
                return httpx.Response(200, json={"results": [{"url": "https://93.184.216.34/context", "content": "Primary context source"}]})
            return httpx.Response(200, text="<html><body><p>" + marker * 20 + "</p></body></html>", headers={"content-type": "text/html"})
        monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(respond), trust_env=False, **kw))
        config = replace(settings, tavily_api_key="offline-placeholder", openai_api_key="")
        reviews = []
        def reviewer(payload):
            reviews.append(payload)
            if review_mode == "invalid":
                return {"invalid": True}
            return {"scores": {key: (0 if review_mode == "exhausted" else (40 if len(reviews) == 1 else 98)) for key in (
                "goal_coverage", "evidence_sufficiency", "source_reliability", "conclusion_consistency", "report_completeness")}}
        executor = ResearchExecutor(config, db,
            agent_builder=lambda *args: build_research_agent(*args, model=model), research_reviewer=reviewer)
        task = db.create_task(GOAL, max_steps=50, max_total_tokens=100000)
        assert Worker(config, db, executor).run_once()
        return {"task": db.get_task(task["task_id"]), "model": model, "events": db.get_events(task["task_id"], limit=5000),
                "store": ArtifactStore(settings.workspace_dir, task["task_id"]), "reviews": reviews,
                "db": db, "settings": settings}
    return run


CONTRADICTION = "CONTRADICTS_HYPOTHESIS: Graph Checkpoint preserves execution state, while long-term research memory is stored separately."
SUPPORT = "SUPPORTS_HYPOTHESIS: Graph Checkpoint stores graph execution state, and durable research memory is maintained separately."


def assert_complete(result, versions):
    assert result["task"]["status"] == "completed", result["task"].get("last_error")
    plans = [e["payload"] for e in result["events"] if e["event_type"] == "research_plan_updated"]
    assert [p["version"] for p in plans] == list(range(1, versions + 1))
    assert plans[0]["plan"]["steps"] == PLAN["steps"]
    assert "Repair:" in result["store"].read_text("report.md")
    assert json.loads(result["store"].read_text("notes/research_plan.json"))["version"] == versions
    evidence = EvidenceRegistry(result["store"]).summary()
    assert evidence["findings"][0]["source_ids"] == [evidence["sources"][0]["source_id"]]
    assert all(result["store"].exists(s["snapshot_path"]) for s in evidence["sources"])
    from runtime_assertions import assert_research_storage_consistency
    messages = assert_research_storage_consistency(result["db"], result["settings"], result["task"]["task_id"], result['model'])
    statuses = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == "get_research_status"]
    assert statuses[0]["coverage"]["coverage_ratio"] < statuses[-1]["coverage"]["coverage_ratio"]
    progress = [e['payload'] for e in result['events'] if e['event_type'] == 'research_plan_step_status']
    assert [p for p in progress if p['step_id'] == 'search'][-1]['status'] == 'completed'
    assert [a['passed'] for a in result['db'].list_quality_assessments(result['task']['task_id'])] == [False, True]
    assert not any(e['event_type'] == 'tool_call_failed' for e in result['events'])
    return plans


def test_contradictory_evidence_triggers_replan(run_research):
    result = run_research(CONTRADICTION)
    assert_complete(result, 2)
    assert any(t["contradiction"] and t["replan"] for t in result["model"].trace)


def test_supporting_evidence_does_not_trigger_replan(run_research):
    result = run_research(SUPPORT)
    assert_complete(result, 1)
    assert not any(t["replan"] for t in result["model"].trace)


def test_replan_decision_depends_on_observed_evidence(run_research):
    conflicting = run_research(CONTRADICTION)
    supporting = run_research(SUPPORT)
    assert_complete(conflicting, 2)
    assert_complete(supporting, 1)
    assert conflicting["task"]["goal"] == supporting["task"]["goal"]
    assert any(t["replan"] for t in conflicting["model"].trace)
    assert not any(t["replan"] for t in supporting["model"].trace)


def test_research_completes_without_unnecessary_plan_revision(run_research):
    result = run_research(SUPPORT)
    assert_complete(result, 1)
    assert any(e["event_type"] == "research_plan_step_status" for e in result["events"])


def test_research_replan_audit_is_complete(run_research):
    result = run_research(CONTRADICTION)
    plans = assert_complete(result, 2)
    assert all(plans[1].get(k) for k in ("trigger", "new_information", "problem_analysis", "decision", "changes"))
    assert "CONTRADICTS_HYPOTHESIS" in plans[1]["new_information"]
    progress = [e['payload'] for e in result['events'] if e['event_type'] == 'research_plan_step_status']
    assert any(p['step_id'] == 'context' and p['plan_version'] == 2 and p['status'] == 'pending' for p in progress)
    assert [p for p in progress if p['step_id'] == 'search'][-1]['plan_version'] == 1
    reads = [e['payload'] for e in result['events'] if e['event_type'] == 'tool_call_started' and e['payload']['tool'] == 'fetch_url']
    assert reads[-1]['plan_step_id'] == 'context' and reads[-1]['plan_version'] == 2


@pytest.mark.parametrize("review_mode", ["repair", "invalid", "exhausted"])
def test_model_driven_replan_and_quality_repair(run_research, review_mode):
    result = run_research(CONTRADICTION, review_mode)
    if review_mode == "repair":
        assert_complete(result, 2)
        assert len(result["reviews"]) == 2
    else:
        assert result["task"]["status"] == ("failed" if review_mode == "invalid" else "waiting_for_human")
        assert len(result["reviews"]) == (1 if review_mode == "invalid" else 4)
        assert not any(e["event_type"] == "report_delivered" for e in result["events"])


class ToolCallingModel(FakeMessagesListChatModel):
    def _generate(self, messages, **kwargs):
        response = self.responses[self.i]
        if response.tool_calls and response.tool_calls[0]["name"] == "update_research_plan" and response.tool_calls[0]["args"].get("new_information"):
            assert any(isinstance(m, ToolMessage) and "NOT stored in the graph" in m.content for m in messages)
        return super()._generate(messages, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return self


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}-{len(str(args))}"}])


@pytest.mark.parametrize("correct", [True, False])
def test_worker_survives_first_schema_error_and_bounds_correction(run_research, correct):
    class CorrectingModel(ConditionalResearchModel):
        errors_sent: int = 0

        def _generate(self, messages, **kwargs):
            failures = [m for m in messages if isinstance(m, ToolMessage) and m.name == "define_research_requirements"
                        and json.loads(m.content).get("success") is False]
            if failures:
                args = {"dimensions": [{"id": dim, "title": title, "questions": ["What persists?"],
                         "expected_evidence": ["Primary evidence"], "priority": "critical" if dim == "context" else "normal",
                         "min_sources": 1} for dim, title in [("context", "Context"), ("scope", "Scope"), ("lifecycle", "Lifecycle")]]}
                if not correct:
                    args["dimensions"][0]["expected_evidence"] = "wrong type"
                if not any(json.loads(m.content).get("saved") for m in messages if isinstance(m, ToolMessage) and m.name == "define_research_requirements"):
                    return ChatResult(generations=[ChatGeneration(message=call("define_research_requirements", **args))])
            result = super()._generate(messages, **kwargs)
            for tc in result.generations[0].message.tool_calls:
                if tc["name"] == "define_research_requirements" and not self.errors_sent:
                    tc["args"]["dimensions"][0]["expected_evidence"] = "wrong type"
                    self.errors_sent += 1
            return result

    result = run_research(SUPPORT, model=CorrectingModel())
    errors = [e for e in result["events"] if e["event_type"] == "research_tool_validation_failed"]
    assert len(errors) == (1 if correct else 2)
    if correct:
        assert result["task"]["status"] == "completed", result["task"]["last_error"]
        assert len([e for e in result["events"] if e["event_type"] == "tool_call_failed"]) == 1
        state = result["db"].get_research_control_state(result["task"]["task_id"])
        assert state["requirements_version"] == 1
    else:
        assert result["task"]["status"] == "failed"
        assert "TOOL_ARGUMENT_CORRECTION_EXHAUSTED" in result["task"]["last_error"]
        assert not any(e["event_type"] == "research_requirements_updated" for e in result["events"])


def test_worker_keeps_running_when_model_corrects_unknown_step_id(run_research):
    class CorrectingStepModel(ConditionalResearchModel):
        error_sent: bool = False
        feedback_seen: bool = False

        def _generate(self, messages, **kwargs):
            feedback = [m for m in messages if isinstance(m, ToolMessage) and m.name == "set_active_plan_step"
                        and str(m.content).startswith("{") and json.loads(m.content).get("error_type") == "plan_step_validation"]
            if feedback and not self.feedback_seen:
                self.feedback_seen = True
                current = json.loads(feedback[-1].content)["current_plan"]
                assert "context" in [s["id"] for s in current["steps"]]
                return ChatResult(generations=[ChatGeneration(message=call("set_active_plan_step", step_id="context"))])
            result = super()._generate(messages, **kwargs)
            for tc in result.generations[0].message.tool_calls:
                if tc["name"] == "set_active_plan_step" and not self.error_sent:
                    tc["args"]["step_id"] = "write-report"
                    self.error_sent = True
            return result

    model = CorrectingStepModel()
    result = run_research(CONTRADICTION, model=model)
    assert result["task"]["status"] == "completed", result["task"].get("last_error")
    assert model.feedback_seen and result["store"].exists("report.md")
    errors = [e for e in result["events"] if e["event_type"] == "tool_call_failed"]
    assert len(errors) == 1 and errors[0]["payload"]["tool"] == "set_active_plan_step"
    assert not any(e["event_type"] == "task_error_classified" for e in result["events"])
    assert result["db"].list_quality_assessments(result["task"]["task_id"])[-1]["passed"] is True

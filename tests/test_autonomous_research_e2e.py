import json
import time
from dataclasses import replace

import pytest
import httpx
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.executor import ResearchExecutor, sqlite_checkpointer
from app.runtime.worker import Worker
from app.tools.research_tools import ResearchToolService


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


@pytest.mark.parametrize("review_mode", ["repair", "invalid", "exhausted"])
def test_model_driven_replan_and_quality_repair(settings, db, monkeypatch, review_mode):
    settings = replace(settings, tavily_api_key="offline-placeholder")
    original_client = httpx.Client
    requested = []

    def respond(request):
        requested.append(str(request.url))
        if request.method == "POST":
            return httpx.Response(200, json={"results": [{"url": "https://example.org/architecture", "content": "Architecture source"}]})
        text = "Architecture uses graph checkpoints. Context is NOT stored in the graph; a separate durable store is required."
        return httpx.Response(200, text="<html><body><p>" + text * 20 + "</p></body></html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(respond), trust_env=False, **kw))
    # Public literal IP avoids DNS; the mocked transport supplies the page.
    source_url = "https://93.184.216.34/architecture"
    first = {"title": "Agent comparison", "objective": "Compare architecture and context", "steps": [
        {"id": "search", "title": "Search architecture", "expected_output_path": "sources/search_2d05a76fb9.json"},
        {"id": "context", "title": "Compare graph context", "expected_output_path": "notes/context.md"},
    ]}
    second = json.loads(json.dumps(first))
    second["steps"][1]["title"] = "Read separate durable context store"
    report = "# Agent architecture comparison\n\n" + "\n\n".join(
        "## " + heading + "\n\n" + ("Graph checkpoints preserve execution, while a separate durable store preserves context. Verified primary evidence supports this distinction [S0001]. " * 6)
        for heading in ["Research scope and methodology", "Architecture and context findings", "Comparison and analysis", "Limitations", "Conclusion"]
    ) + "\n\n## Sources\n- " + source_url
    sequence = [
        call("update_research_plan", **first),
        call("define_research_requirements", dimensions=[{"id": "context", "title": "Context", "questions": ["Where is context stored?"], "priority": "critical", "min_sources": 1}]),
        call("web_search", query="agent architecture"),
        call("update_plan_step_status", step_id="search", status="completed", output_path="sources/search_2d05a76fb9.json"),
        call("fetch_url", url=source_url),
        call("get_research_status"),
        call("update_research_plan", **second, trigger="Contradictory primary source", new_information="S0001: Context is NOT stored in the graph", problem_analysis="Original graph-only hypothesis is wrong", decision="Inspect separate durable store", changes="Replace graph context comparison", change_reason="New primary evidence"),
        call("set_active_plan_step", step_id="context"),
        call("fetch_url", url=source_url + "?context=durable"),
        call("record_finding", claim="Context requires a separate durable store", source_ids=["S0001"], dimension_id="context", confidence="high", rationale="Primary page explicitly contradicts graph-only context"),
        call("get_research_status"),
        AIMessage(content=report),
        call("save_artifact", path="report.md", content=report + "\n\nRepair: context and execution state have distinct ownership [S0001]."),
        AIMessage(content="Delivered repaired report."),
    ]
    sequence.extend([AIMessage(content=report + "\nRepair attempt [S0001].")] * 2)
    model = ToolCallingModel(responses=sequence)
    def builder(settings, database, task, saver, started):
        store = ArtifactStore(settings.workspace_dir, task["task_id"])
        service = ResearchToolService(settings, database, task["task_id"], store, started, task.get("worker_id"), task.get("worker_generation"))
        return create_agent(model, service.as_langchain_tools(), checkpointer=saver)

    reviews = []
    def reviewer(payload):
        reviews.append(payload)
        if review_mode == "invalid":
            return {"invalid": True}
        return {"scores": {key: (0 if review_mode == "exhausted" else (40 if len(reviews) == 1 else 98)) for key in (
            "goal_coverage", "evidence_sufficiency", "source_reliability", "conclusion_consistency", "report_completeness")}}

    task = db.create_task("Compare agent architecture and context management", max_steps=50, max_total_tokens=50000)
    executor = ResearchExecutor(settings, db, agent_builder=builder, research_reviewer=reviewer)
    assert Worker(settings, db, executor).run_once()
    task = db.get_task(task["task_id"])
    events = db.get_events(task["task_id"], limit=5000)
    if review_mode == "invalid":
        assert task["status"] == "failed"
        assert "scores object" in task["last_error"]
        assert len(reviews) == 1
        assert not any(e["event_type"] == "report_delivered" for e in events)
        return
    if review_mode == "exhausted":
        assert task["status"] == "waiting_for_human", task.get("last_error")
        category = db.get_pending_human_intervention(task["task_id"])["category"]
        assert category == ("independent_review_unavailable" if review_mode == "invalid" else "quality_not_satisfied")
        assert len(reviews) == (1 if review_mode == "invalid" else 4)
        assert not any(e["event_type"] == "report_delivered" for e in events)
        return
    assert task["status"] == "completed", task.get("last_error")
    plans = [e["payload"] for e in events if e["event_type"] == "research_plan_updated"]
    assert len(plans) == 2
    assert plans[0]["plan"] != plans[1]["plan"]
    assert "S0001" in plans[1]["new_information"]
    progress = [e["payload"] for e in events if e["event_type"] == "research_plan_step_status"]
    assert [p for p in progress if p["step_id"] == "search"][-1]["status"] == "completed"
    assert any(p["step_id"] == "context" and p["plan_version"] == 2 and p["status"] == "pending" for p in progress)
    reads = [e for e in events if e["event_type"] == "tool_call_started" and e["payload"]["tool"] == "fetch_url"]
    assert reads[-1]["payload"]["plan_step_id"] == "context"
    assert reads[-1]["payload"]["plan_version"] == 2
    store = ArtifactStore(settings.workspace_dir, task["task_id"])
    assert "Repair:" in store.read_text("report.md")
    assert json.loads(store.read_text("notes/research_plan.json"))["version"] == 2
    registry = EvidenceRegistry(store).summary()
    assert registry["findings"][0]["source_ids"] == ["S0001"]
    assert all(store.exists(s["snapshot_path"]) for s in registry["sources"])
    assessments = db.list_quality_assessments(task["task_id"])
    assert len(assessments) == 2
    assert [a["passed"] for a in assessments] == [False, True]
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        checkpoint = saver.get_tuple({"configurable": {"thread_id": task["task_id"]}})
        messages = checkpoint.checkpoint["channel_values"]["messages"]
        statuses = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == "get_research_status"]
        assert statuses[0]["coverage"]["coverage_ratio"] < statuses[-1]["coverage"]["coverage_ratio"]
    with db.connect() as conn:
        calls = conn.execute("SELECT call_id,status FROM tool_calls WHERE task_id=?", (task["task_id"],)).fetchall()
        assert calls and all(c["status"] == "succeeded" for c in calls)

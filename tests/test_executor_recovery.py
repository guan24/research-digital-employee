from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from app.infrastructure.artifact_store import ArtifactStore
from app.runtime.executor import ResearchExecutor


@contextmanager
def fake_checkpointer(_path):
    yield object()


class FakeAgent:
    def __init__(self, snapshot=None, result=None):
        self.snapshot = snapshot
        self.result = result or {"messages": [{"role": "assistant", "content": (
            "# Research report\n\n## 摘要\n\n" + ("Evidence-backed result and analysis. " * 12)
            + "\n\n## Sources\n- https://example.org/research-source"
        )}]}
        self.invocations = []
        self.configs = []

    def get_state(self, config):
        return self.snapshot

    def invoke(self, payload, config):
        self.invocations.append(payload)
        self.configs.append(config)
        return self.result


def _make_runner(settings, db, agent):
    def builder(_settings, _db, _task, _checkpointer, _started):
        return agent

    return ResearchExecutor(settings, db, agent_builder=builder, checkpointer_factory=fake_checkpointer)


def test_executor_starts_new_task_without_checkpoint(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    agent = FakeAgent(snapshot=None)
    runner = _make_runner(settings, db, agent)
    report_path = runner.run(task)
    assert agent.invocations[0]["messages"][0]["content"] == task["goal"]
    assert Path(report_path).read_text(encoding="utf-8").startswith("# Research report")


def test_executor_resumes_pending_graph_from_checkpoint(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    snapshot = SimpleNamespace(next=("agent",), values={"messages": []})
    agent = FakeAgent(snapshot=snapshot)
    runner = _make_runner(settings, db, agent)
    runner.run(task)
    assert agent.invocations == [None]
    assert any(event["event_type"] == "checkpoint_resume" for event in db.get_events(task["task_id"]))


def test_executor_recovers_completed_checkpoint_without_reinvoking(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    snapshot = SimpleNamespace(
        next=(),
        values={"messages": [{"role": "assistant", "content": (
                "# Already completed\n\n## 摘要\n\n" + ("Saved from checkpoint evidence. " * 12)
                + "\n\n## Sources\n- https://example.org/checkpoint-source"
            )}]},
    )
    agent = FakeAgent(snapshot=snapshot)
    runner = _make_runner(settings, db, agent)
    report_path = runner.run(task)
    assert agent.invocations == []
    assert "Already completed" in Path(report_path).read_text(encoding="utf-8")



def test_manual_retry_resumes_original_checkpoint_thread_id(settings, db):
    task = db.create_task("Research a topic with enough detail", max_attempts=1)
    claimed = db.claim_next_task("worker-a")
    assert db.schedule_retry(
        claimed, "OpenAI API quota exhausted", 0, "worker-a"
    ) == "failed"
    retry_result = db.requeue_failed_task(
        task["task_id"], reason="OpenAI API quota replenished"
    )
    assert retry_result is not None and retry_result["requeued"] is True

    resumed_task = db.claim_next_task("worker-b")
    snapshot = SimpleNamespace(next=("agent",), values={"messages": []})
    agent = FakeAgent(snapshot=snapshot)
    runner = _make_runner(settings, db, agent)
    runner.run(resumed_task)

    assert agent.invocations == [None]
    assert agent.configs[0]["configurable"]["thread_id"] == task["task_id"]
    assert any(
        event["event_type"] == "checkpoint_resume"
        for event in db.get_events(task["task_id"])
    )


def test_real_sqlite_checkpoint_resumes_after_interrupted_node(tmp_path):
    from typing import TypedDict
    from langgraph.graph import END, START, StateGraph
    from app.runtime.executor import sqlite_checkpointer

    class State(TypedDict):
        completed: int
        final: str

    calls = {"completed_node": 0, "interrupted_node": 0}
    def completed_node(state):
        calls["completed_node"] += 1
        return {"completed": state["completed"] + 1}
    def interrupted_node(state):
        calls["interrupted_node"] += 1
        if calls["interrupted_node"] == 1:
            raise RuntimeError("simulated provider interruption")
        return {"final": "resumed"}

    graph = StateGraph(State)
    graph.add_node("completed_node", completed_node)
    graph.add_node("interrupted_node", interrupted_node)
    graph.add_edge(START, "completed_node")
    graph.add_edge("completed_node", "interrupted_node")
    graph.add_edge("interrupted_node", END)
    config = {"configurable": {"thread_id": "integration-resume"}}
    with sqlite_checkpointer(tmp_path / "real-checkpoint.sqlite") as saver:
        app = graph.compile(checkpointer=saver)
        try:
            app.invoke({"completed": 0, "final": ""}, config=config)
        except RuntimeError as exc:
            assert "simulated provider interruption" in str(exc)
        else:
            raise AssertionError("first invocation must be interrupted")
        snapshot = app.get_state(config)
        assert snapshot.values["completed"] == 1
        assert snapshot.next == ("interrupted_node",)
        result = app.invoke(None, config=config)
        assert result["final"] == "resumed"
    assert calls == {"completed_node": 1, "interrupted_node": 2}


def test_minimal_second_role_owns_artifact_and_finalizer(settings, db):
    from app.agents.registry import register_agent
    from app.runtime.executor import ResearchExecutor

    class MinimalAgent(FakeAgent):
        def __init__(self):
            super().__init__(snapshot=None, result={"messages":[{"role":"assistant","content":"A concise operations brief."}]})

    calls = []
    register_agent(
        "operations_test_role",
        lambda *_args: MinimalAgent(),
        output_artifact="deliverables/operations.txt",
        finalizer=lambda _db, task_id, artifacts, status, task: calls.append((task_id, status, artifacts.exists("deliverables/operations.txt"))),
    )
    task = db.create_task("Research a topic with enough detail", agent_type="operations_test_role")
    task = db.claim_next_task("worker-a")
    runner = ResearchExecutor(settings, db, checkpointer_factory=fake_checkpointer)
    output = runner.run(task)
    assert output.endswith("deliverables/operations.txt")
    assert Path(output).read_text(encoding="utf-8") == "A concise operations brief.\n"
    runner.finalize_task(task["task_id"], "completed")
    assert calls == [(task["task_id"], "completed", True)]

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
        self.result = result or {"messages": [{"role": "assistant", "content": "# Research report\n\nEvidence-backed result."}]}
        self.invocations = []

    def get_state(self, config):
        return self.snapshot

    def invoke(self, payload, config):
        self.invocations.append(payload)
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
        values={"messages": [{"role": "assistant", "content": "# Already completed\n\nSaved from checkpoint."}]},
    )
    agent = FakeAgent(snapshot=snapshot)
    runner = _make_runner(settings, db, agent)
    report_path = runner.run(task)
    assert agent.invocations == []
    assert "Already completed" in Path(report_path).read_text(encoding="utf-8")

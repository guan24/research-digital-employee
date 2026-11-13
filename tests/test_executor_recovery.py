from __future__ import annotations

from contextlib import contextmanager
import time
import pytest
from pathlib import Path
from types import SimpleNamespace

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.executor import ResearchExecutor, TaskEventCallback
from app.runtime.control import TaskWaitingForHuman


@contextmanager
def fake_checkpointer(_path):
    yield object()


class FakeAgent:
    def __init__(self, snapshot=None, result=None):
        self.snapshot = snapshot
        self.result = result or {"messages": [{"role": "assistant", "content": (
            "# Research report\n\n## 摘要\n\n" + ("Evidence-backed result and analysis is supported by the registered primary source [S0001]. " * 8)
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


def _register_evidence(settings, task):
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    return EvidenceRegistry(artifacts).register_source(
        url="https://example.org/research-source",
        title="Registered primary source",
        content="Verified primary evidence supporting the research report. " * 20,
    )


def _make_runner(settings, db, agent):
    def builder(_settings, _db, _task, _checkpointer, _started):
        return agent

    return ResearchExecutor(
        settings, db, agent_builder=builder, checkpointer_factory=fake_checkpointer,
        quality_review_enabled=False,
    )


def test_executor_starts_new_task_without_checkpoint(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    _register_evidence(settings, task)
    agent = FakeAgent(snapshot=None)
    runner = _make_runner(settings, db, agent)
    report_path = runner.run(task)
    assert agent.invocations[0]["messages"][0]["content"] == task["goal"]
    assert Path(report_path).read_text(encoding="utf-8").startswith("# Research report")


def test_executor_resumes_pending_graph_from_checkpoint(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    _register_evidence(settings, task)
    snapshot = SimpleNamespace(next=("agent",), values={"messages": []})
    agent = FakeAgent(snapshot=snapshot)
    runner = _make_runner(settings, db, agent)
    runner.run(task)
    assert agent.invocations == [None]
    assert any(event["event_type"] == "checkpoint_resume" for event in db.get_events(task["task_id"]))


def test_executor_recovers_completed_checkpoint_without_reinvoking(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    _register_evidence(settings, task)
    snapshot = SimpleNamespace(
        next=(),
        values={"messages": [{"role": "assistant", "content": (
                "# Already completed\n\n## 摘要\n\n" + ("Saved from checkpoint evidence and verified by the registered primary source [S0001]. " * 8)
                + "\n\n## Sources\n- https://example.org/research-source"
            )}]},
    )
    agent = FakeAgent(snapshot=snapshot)
    runner = _make_runner(settings, db, agent)
    report_path = runner.run(task)
    assert agent.invocations == []
    assert "Already completed" in Path(report_path).read_text(encoding="utf-8")



def test_executor_runs_integrated_evidence_and_quality_delivery_loop(settings, db):
    from app.infrastructure.evidence_registry import EvidenceRegistry
    from app.runtime.research_controller import ResearchController

    task = db.create_task("研究 Agent 生命周期闭环")
    task = db.claim_next_task("worker-quality-loop")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    registry = EvidenceRegistry(artifacts)
    source = registry.register_source(
        url="https://example.org/architecture",
        title="Primary architecture source",
        content="The primary source documents the architecture, state persistence and recovery semantics. " * 12,
    )
    registry.record_finding(
        claim="The system persists graph state and resumes pending work after interruption.",
        source_ids=[source["source_id"]],
        rationale="Verified against the immutable source snapshot.",
        confidence="high",
        dimension_id="architecture",
    )
    ResearchController(db, task["task_id"], artifacts).define_requirements({
        "dimensions": [{
            "id": "architecture", "title": "系统架构", "questions": ["状态如何持久化和恢复？"],
            "expected_evidence": ["一手架构资料"], "priority": "critical", "min_sources": 1,
        }]
    })
    report = (
        "# 研究报告\n\n"
        "## 研究范围与方法\n\n"
        + ("本研究依据登记的一手架构资料分析状态持久化与任务恢复机制，并将关键结论与来源快照逐项核对 [S0001]。 " * 3)
        + "\n\n## 主要发现\n\n"
        + ("系统使用持久化执行状态恢复中断任务，来源快照支持这一实现判断，并记录了相应证据边界 [S0001]。 " * 3)
        + "\n\n## 局限性\n\n"
        + ("当前验证覆盖单 Worker 的离线恢复路径，尚未覆盖多主机并发执行和外部服务长期故障 [S0001]。 " * 3)
        + "\n\n## 结论\n\n"
        + ("该实现能够为单 Worker 长任务提供可追踪的状态恢复基础，仍需通过故障注入进一步验证边界 [S0001]。 " * 3)
        + "\n\n## Sources\n\n- https://example.org/architecture\n"
    )

    class LifecycleAgent:
        def get_state(self, config):
            return None
        def invoke(self, payload, config):
            callback = config["callbacks"][0]
            callback.on_tool_start({"name": "web_search"}, '{"query":"architecture"}', run_id="quality-loop-tool")
            callback.on_tool_end('{"results":[]}', run_id="quality-loop-tool")
            return {"messages": [{"role": "assistant", "content": report}]}

    reviewer = lambda _: {"scores": {key: 95 for key in (
        "goal_coverage", "evidence_sufficiency", "source_reliability",
        "conclusion_consistency", "report_completeness")}}
    agent = LifecycleAgent()
    runner = ResearchExecutor(
        settings, db,
        agent_builder=lambda *_args: agent,
        checkpointer_factory=fake_checkpointer,
        research_reviewer=reviewer,
        quality_review_enabled=True,
    )
    output = runner.run(task)
    assert Path(output).read_text(encoding="utf-8").startswith("# 研究报告")
    assessments = db.list_quality_assessments(task["task_id"])
    assert len(assessments) == 1 and assessments[0]["passed"] is True
    events = db.get_events(task["task_id"])
    assert any(event["event_type"] == "research_phase_boundary_observed" for event in events)
    delivered = next(event for event in events if event["event_type"] == "report_delivered")
    assert delivered["payload"]["quality_assessment_id"] == assessments[0]["assessment_id"]


def test_manual_retry_resumes_original_checkpoint_thread_id(settings, db):
    task = db.create_task("Research a topic with enough detail", max_attempts=1)
    claimed = db.claim_next_task("worker-a")
    _register_evidence(settings, claimed)
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


def test_resumed_agent_receives_human_natural_language_instruction(settings, db):
    from langchain_core.messages import HumanMessage

    task = db.create_task("Research a topic with enough detail")
    claimed = db.claim_next_task("worker-before-human")
    _register_evidence(settings, claimed)
    intervention_id = db.set_waiting_for_human(
        task["task_id"], "research_control_requires_human",
        {"message": "需要补充研究范围。", "choices": ["add_research_requirements", "terminate"]},
        worker_id="worker-before-human",
    )
    operator_instruction = "请补充安全边界和权限隔离机制，并优先核验官方文档。"
    db.resolve_human_intervention(
        task["task_id"], intervention_id, "add_research_requirements",
        {"reason": "", "instruction": operator_instruction},
    )
    resumed_task = db.claim_next_task("worker-after-human")
    assert bool(resumed_task["resume_mode"])

    agent = FakeAgent(snapshot=SimpleNamespace(next=("agent",), values={"messages": []}))
    runner = _make_runner(settings, db, agent)
    runner.run(resumed_task)

    assert agent.invocations
    injected = agent.invocations[0]["messages"][0]
    assert isinstance(injected, HumanMessage)
    assert operator_instruction in injected.content
    assert "add_research_requirements" in injected.content
    assert db.list_unconsumed_human_decisions(task["task_id"]) == []


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

def test_executor_retries_once_when_final_model_turn_is_empty(settings, db):
    from app.agents.registry import register_agent

    role = "empty_response_probe_" + __import__("uuid").uuid4().hex[:8]
    class EmptyThenTextAgent:
        def __init__(self):
            self.calls = 0
        def get_state(self, config):
            return None
        def invoke(self, payload, config):
            self.calls += 1
            content = "" if self.calls == 1 else "# Recovered artifact\n\nA complete persisted deliverable."
            return {"messages": [{"role": "assistant", "content": content}]}

    agent = EmptyThenTextAgent()
    register_agent(role, lambda *_args: agent, output_artifact="reports/recovered.md")
    task = db.create_task("Recover a missing final response", agent_type=role)
    task = db.claim_next_task("worker-empty")
    runner = ResearchExecutor(settings, db, checkpointer_factory=fake_checkpointer)

    output = runner.run(task)
    assert agent.calls == 2
    assert Path(output).read_text(encoding="utf-8").startswith("# Recovered artifact")
    assert any(event["event_type"] == "model_empty_final_response_recovery_started" for event in db.get_events(task["task_id"]))
    assert any(event["event_type"] == "artifact_persisted_verified" for event in db.get_events(task["task_id"]))


def test_executor_persists_repair_response_when_agent_does_not_call_save_artifact(settings, db):
    from app.agents.registry import register_agent

    role = "repair_response_probe_" + __import__("uuid").uuid4().hex[:8]
    valid_report = (
        "# Repaired report\n\n## 摘要\n\n"
        + ("The repaired analysis is complete and supported by the available research evidence. " * 8)
        + "\n\n## Sources\n- https://example.org/repaired"
    )
    class RepairAgent:
        def __init__(self):
            self.calls = 0
        def get_state(self, config):
            return None
        def invoke(self, payload, config):
            self.calls += 1
            content = "Initial invalid draft." if self.calls == 1 else valid_report
            return {"messages": [{"role": "assistant", "content": content}]}

    agent = RepairAgent()
    def validator(content, task, artifacts):
        return "Repaired report" in content
    def postprocessor(current_agent, result, config, task, artifacts, validation):
        return current_agent.invoke({"messages": [{"role": "user", "content": "repair"}]}, config=config)

    register_agent(
        role, lambda *_args: agent,
        output_artifact="reports/repaired.md",
        validator=validator,
        postprocessor=postprocessor,
        max_revisions=1,
    )
    task = db.create_task("Exercise repair response persistence", agent_type=role)
    task = db.claim_next_task("worker-repair")
    runner = ResearchExecutor(settings, db, checkpointer_factory=fake_checkpointer)

    output = runner.run(task)
    persisted = Path(output).read_text(encoding="utf-8")
    events = db.get_events(task["task_id"])
    assert "Repaired report" in persisted
    assert any(event["event_type"] == "report_delivered" for event in events)
    assert sum(event["event_type"] == "delivery_validation_completed" for event in events) == 2


def test_research_controller_request_human_parks_worker_and_stops_callbacks(db):
    task = db.create_task("Park when research controller requires operator input")
    claimed = db.claim_next_task("worker-controller-human")

    class ProgressProbe:
        @staticmethod
        def _tool_phase(_tool_name, _input_value):
            return "analysis"

    class LifecycleProbe:
        @staticmethod
        def assess_progress(trigger):
            assert trigger == "phase_boundary"
            return {
                "coverage": {"coverage_ratio": 0.25, "critical_gaps": [{"dimension_id": "security"}]},
                "decision": {
                    "recommended_action": "REQUEST_HUMAN",
                    "reason": "关键安全维度连续评估无进展。",
                },
            }

    callback = TaskEventCallback(
        db, task["task_id"], time.monotonic(), 120,
        worker_id="worker-controller-human",
        worker_generation=claimed["worker_generation"],
        plan_progress_service=ProgressProbe(),
        lifecycle_coordinator=LifecycleProbe(),
    )
    with pytest.raises(TaskWaitingForHuman):
        callback._observe_research_phase("record_finding", "{}")

    assert db.get_task(task["task_id"])["status"] == "waiting_for_human"
    pending = db.get_pending_human_intervention(task["task_id"])
    assert pending["category"] == "research_control_requires_human"
    assert "add_research_requirements" in pending["request"]["choices"]
    with pytest.raises(TaskWaitingForHuman):
        callback._check_control()


def test_executor_injects_human_decision_when_resuming_completed_checkpoint(settings, db):
    task = db.create_task("Research a topic with enough detail")
    first_claim = db.claim_next_task("worker-human-resume")
    _register_evidence(settings, first_claim)
    intervention_id = db.set_waiting_for_human(
        task["task_id"], "research_control_requires_human",
        {"message": "请调整研究重点。", "choices": ["change_research_direction", "terminate"]},
        worker_id="worker-human-resume",
    )
    db.resolve_human_intervention(
        task["task_id"], intervention_id, "change_research_direction",
        {"reason": "优先研究安全边界。", "direction": "将安全边界和权限隔离作为首要研究方向。"},
    )
    resumed_task = db.claim_next_task("worker-human-resume")
    assert resumed_task["resume_mode"] == 1

    snapshot = SimpleNamespace(
        next=(),
        values={"messages": [{"role": "assistant", "content": (
            "# Research report\n\n## 摘要\n\n"
            + ("Evidence-backed result and analysis is supported by the registered primary source [S0001]. " * 8)
            + "\n\n## Sources\n- https://example.org/research-source"
        )}]},
    )
    agent = FakeAgent(snapshot=snapshot)
    runner = _make_runner(settings, db, agent)
    runner.run(resumed_task)

    assert len(agent.invocations) == 1
    injected = agent.invocations[0]["messages"]
    human_message = next(message for message in injected if getattr(message, "id", None) == f"human-intervention-{intervention_id}")
    assert "安全边界和权限隔离" in human_message.content
    assert "change_research_direction" in human_message.content
    events = db.get_events(task["task_id"])
    assert any(
        event["event_type"] == "human_decision_consumed"
        and event["payload"]["intervention_id"] == intervention_id
        for event in events
    )


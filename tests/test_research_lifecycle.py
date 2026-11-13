from __future__ import annotations

import json
from types import SimpleNamespace
import time
import pytest

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.recovery_manager import RecoveryManager
from app.runtime.research_controller import ResearchController
from app.runtime.research_evaluator import ResearchEvaluator
from app.runtime.plan_progress import PlanProgressTracker
from app.tools.research_tools import ResearchToolService


def _running_task(db, goal="验证研究闭环"):
    task = db.create_task(goal)
    claimed = db.claim_next_task("worker-lifecycle-test")
    assert claimed and claimed["task_id"] == task["task_id"]
    return db.get_task(task["task_id"])


@pytest.mark.parametrize("action", ["continue_research", "change_research_direction", "add_research_requirements"])
def test_explicit_human_continuation_resets_only_stagnation_window(settings, db, action):
    task = _running_task(db)
    controller = ResearchController(db, task["task_id"])
    controller.define_requirements({"dimensions": [{"id": "critical", "title": "Evidence",
        "questions": ["What supports the claim?"], "expected_evidence": ["Verified source"],
        "priority": "critical", "min_sources": 1}]})
    for _ in range(3):
        coverage = controller.assess_coverage({"sources": [], "findings": []})
    assert coverage["stagnated"]
    before = db.get_research_control_state(task["task_id"])
    intervention = db.set_waiting_for_human(task["task_id"], "research_control_requires_human", {"choices": [action]})
    assert db.resolve_human_intervention(task["task_id"], intervention, action)["status"] == "queued"
    after = db.get_research_control_state(task["task_id"])
    assert after["stagnation_count"] == 0 and after["coverage"]["stagnated"] is False
    for key in ("requirements", "requirements_version", "progress_vector", "cycle"):
        assert after[key] == before[key]
    assert after["coverage"]["critical_gaps"] == before["coverage"]["critical_gaps"]
    first = controller.assess_coverage({"sources": [], "findings": []})
    assert controller.decide(first, {})["recommended_action"] == "FILL_EVIDENCE_GAP"
    second = controller.assess_coverage({"sources": [], "findings": []})
    assert controller.decide(second, {})["recommended_action"] == "REQUEST_HUMAN"
    resets = [e for e in db.get_events(task["task_id"]) if e["event_type"] == "research_stagnation_reset"]
    assert len(resets) == 1 and resets[0]["payload"]["previous_count"] == 2


def _registered_evidence(artifacts, dimension_id="architecture"):
    registry = EvidenceRegistry(artifacts)
    source = registry.register_source(
        url="https://github.com/example/project",
        title="Official repository",
        content="The official repository documents the implementation and its release behavior.",
        source_type="github",
    )
    finding = registry.record_finding(
        claim="The project implements a persistent graph execution model.",
        source_ids=[source["source_id"]],
        rationale="Verified against the official repository snapshot.",
        confidence="high",
        dimension_id=dimension_id,
    )
    return registry, source, finding


def test_research_controller_persists_requirements_and_computes_dimension_coverage(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    registry, source, finding = _registered_evidence(artifacts)
    controller = ResearchController(db, task["task_id"], artifacts)

    saved = controller.define_requirements({
        "dimensions": [{
            "id": "architecture", "title": "架构机制",
            "questions": ["系统如何组织执行状态？"],
            "expected_evidence": ["官方源码", "设计文档"],
            "priority": "critical", "min_sources": 1,
        }]
    })
    assert saved["saved"] is True
    assert saved["version"] == 1
    duplicate = controller.define_requirements({
        "dimensions": [{
            "id": "architecture", "title": "架构机制",
            "questions": ["系统如何组织执行状态？"],
            "expected_evidence": ["官方源码", "设计文档"],
            "priority": "critical", "min_sources": 1,
        }]
    })
    assert duplicate["saved"] is False

    coverage = controller.assess_coverage(registry.summary(), trigger="phase_boundary")
    assert coverage["coverage_ratio"] == 1.0
    assert coverage["dimensions"][0]["status"] == "covered"
    assert coverage["dimensions"][0]["valid_source_ids"] == [source["source_id"]]
    assert coverage["critical_gaps"] == []
    state = db.get_research_control_state(task["task_id"])
    assert state["requirements"]["version"] == 1
    assert state["coverage"]["coverage_ratio"] == 1.0
    assert finding["dimension_id"] == "architecture"


def test_research_controller_detects_critical_gap_and_stagnation(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    controller = ResearchController(db, task["task_id"], artifacts)
    controller.define_requirements({
        "dimensions": [{
            "id": "primary_evidence", "title": "一手证据",
            "questions": ["是否有官方实现证据？"],
            "expected_evidence": ["官方源码"], "priority": "critical", "min_sources": 2,
        }]
    })
    empty = {"sources": [], "findings": []}
    first = controller.assess_coverage(empty, trigger="phase_boundary")
    second = controller.assess_coverage(empty, trigger="phase_boundary")
    third = controller.assess_coverage(empty, trigger="phase_boundary")
    assert first["critical_gaps"]
    assert second["stagnation_count"] == 1
    assert third["stagnated"] is True
    decision = controller.decide(third, {"phase": "research"})
    assert decision["recommended_action"] == "REQUEST_HUMAN"
    assert "FILL_EVIDENCE_GAP" not in decision["allowed_actions"]


def test_agent_query_does_not_increment_research_stagnation(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    controller = ResearchController(db, task["task_id"], artifacts)
    controller.define_requirements({
        "dimensions": [{
            "id": "scope", "title": "研究范围", "questions": ["研究什么？"],
            "expected_evidence": ["primary source"], "priority": "normal", "min_sources": 1,
        }]
    })
    registry = {"sources": [], "findings": []}
    controller.assess_coverage(registry, trigger="phase_boundary")
    before = db.get_research_control_state(task["task_id"])["stagnation_count"]
    controller.assess_coverage(registry, trigger="agent_query")
    after = db.get_research_control_state(task["task_id"])["stagnation_count"]
    assert after == before


def test_research_evaluator_combines_rule_and_independent_review(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    registry, _, _ = _registered_evidence(artifacts)
    controller = ResearchController(db, task["task_id"], artifacts)
    controller.define_requirements({
        "dimensions": [{
            "id": "architecture", "title": "架构机制", "questions": ["如何执行？"],
            "expected_evidence": ["官方源码"], "priority": "critical", "min_sources": 1,
        }]
    })
    coverage = controller.assess_coverage(registry.summary(), trigger="final_delivery")
    report = """# 研究报告

## 研究范围与方法

本研究依据官方仓库和设计资料分析系统的执行机制，明确比较范围、版本和证据筛选方法。

## 主要发现

系统采用持久化图执行模型。官方仓库中的实现和登记快照支持这一判断，具体设计通过状态持久化与节点恢复实现。

## 局限性

当前验证只覆盖公开仓库和可读取的实现文件，未验证生产部署中的多主机行为。

## 结论

该架构能够支持单 Worker 下的持久化研究执行，后续仍需通过故障注入验证恢复边界。

## Sources

- https://github.com/example/project
""" + ("补充分析说明。" * 240)
    received = {}

    def reviewer(context):
        received.update(context)
        return {
            "scores": {
                "goal_coverage": 90, "evidence_sufficiency": 90,
                "source_reliability": 90, "conclusion_consistency": 90,
                "report_completeness": 90,
            },
            "unsupported_claims": [], "contradictions": [],
            "missing_dimensions": [], "actionable_repairs": [], "summary": "证据链完整",
        }

    evaluator = ResearchEvaluator(
        settings, db, task["task_id"], artifacts, reviewer=reviewer,
        profile={"weights": {
            "goal_coverage": 20, "evidence_sufficiency": 25, "source_reliability": 20,
            "conclusion_consistency": 20, "report_completeness": 15,
        }, "min_total_score": 80, "min_dimension_score": 60, "rule_ratio": 0.6},
    )
    assessment = evaluator.evaluate(task, report, registry.summary(), coverage, allow_llm=True)
    assert assessment["passed"] is True
    assert assessment["total_score"] >= 80
    assert set(received) == {
        "goal", "requirements", "coverage", "registered_source_count", "sources",
        "findings", "rule_scores", "rule_defects", "report_sha256", "report",
    }
    assert received["sources"][0]["snapshot_excerpt"]
    assert assessment["report_sha256"] == assessment["reviewer"]["report_sha256"]
    saved = db.list_quality_assessments(task["task_id"])
    assert len(saved) == 1
    assert saved[0]["assessment_id"] == assessment["assessment_id"]


def test_research_evaluator_raises_task_quality_threshold_without_allowing_lowering(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    registry, _, _ = _registered_evidence(artifacts)
    controller = ResearchController(db, task["task_id"], artifacts)
    controller.define_requirements({
        "dimensions": [{
            "id": "architecture", "title": "架构机制", "questions": ["如何执行？"],
            "expected_evidence": ["官方源码"], "priority": "critical", "min_sources": 1,
        }],
        "quality_policy": {"min_total_score": 92, "min_dimension_score": 75},
    })
    coverage = controller.assess_coverage(registry.summary())
    reviewer = lambda _: {"scores": {key: 95 for key in (
        "goal_coverage", "evidence_sufficiency", "source_reliability",
        "conclusion_consistency", "report_completeness")}}
    evaluator = ResearchEvaluator(
        settings, db, task["task_id"], artifacts, reviewer=reviewer,
        profile={"weights": {key: value for key, value in zip(
            ("goal_coverage", "evidence_sufficiency", "source_reliability",
             "conclusion_consistency", "report_completeness"), (20, 25, 20, 20, 15))},
            "min_total_score": 80, "min_dimension_score": 60, "rule_ratio": 0.6},
    )
    report = "# 研究报告\n\n## 研究范围与方法\n范围。\n\n## 主要发现\n发现。\n\n## 局限性\n局限。\n\n## 结论\n结论。\n\n## Sources\n- https://github.com/example/project\n" + ("补充证据分析。" * 200)
    assessment = evaluator.evaluate(task, report, registry.summary(), coverage, allow_llm=True)
    assert assessment["thresholds"]["min_total_score"] == 92
    assert assessment["thresholds"]["min_dimension_score"] == 75
    assert assessment["passed"] is True

    controller.define_requirements({
        "dimensions": [{
            "id": "architecture", "title": "架构机制", "questions": ["如何执行？"],
            "expected_evidence": ["官方源码"], "priority": "critical", "min_sources": 1,
        }],
        "quality_policy": {"min_total_score": 40, "min_dimension_score": 20},
    })
    lower = evaluator.evaluate(task, report, registry.summary(), coverage, allow_llm=True)
    assert lower["thresholds"]["min_total_score"] == 80
    assert lower["thresholds"]["min_dimension_score"] == 60


def test_research_evaluator_rejects_missing_independent_review_dimension(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    evaluator = ResearchEvaluator(
        settings, db, task["task_id"], artifacts,
        reviewer=lambda _: {"scores": {"goal_coverage": 90}},
    )
    try:
        evaluator.review_with_llm({"goal": "test"})
    except ValueError as exc:
        assert "omitted score dimension" in str(exc)
    else:
        raise AssertionError("incomplete reviewer scores must be rejected")


def test_research_evaluator_never_waives_unknown_source_id(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    registry, _, _ = _registered_evidence(artifacts)
    controller = ResearchController(db, task["task_id"], artifacts)
    controller.define_requirements({
        "dimensions": [{
            "id": "architecture", "title": "架构机制", "questions": ["如何执行？"],
            "expected_evidence": ["官方源码"], "priority": "critical", "min_sources": 1,
        }]
    })
    coverage = controller.assess_coverage(registry.summary())
    evaluator = ResearchEvaluator(settings, db, task["task_id"], artifacts,
                                  reviewer=lambda _: {"scores": {key: 90 for key in (
                                      "goal_coverage", "evidence_sufficiency", "source_reliability",
                                      "conclusion_consistency", "report_completeness")}})
    validation = SimpleNamespace(errors=("报告引用了不存在的来源 ID：S9999",))
    assessment = evaluator.evaluate(task, "# Report\n\n正文。" * 100, registry.summary(),
                                    coverage, validation=validation, allow_llm=True)
    assert assessment["passed"] is False
    assert any(item["code"] == "unknown_source_id" and item["waivable"] is False
               for item in assessment["hard_blocks"])
    assert assessment["reviewer"]["status"] == "skipped_hard_block"


def test_recovery_manager_verifies_artifact_side_effect_by_hash(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    recovery = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    content = "# Report\n\nPersisted before worker crash."
    artifacts.write_text("report.md", content)
    call_id = recovery.begin_tool_call(
        task["task_id"], "call-artifact-1", "save_artifact",
        json.dumps({"path": "report.md", "content": content}), 1, {},
    )
    record = next(item for item in db.get_open_tool_calls(task["task_id"]) if item["call_id"] == call_id)
    db.mark_tool_call_unknown(task["task_id"], call_id, "simulated worker crash")
    result = recovery._verify_unknown_call(
        task["task_id"], record, artifacts, EvidenceRegistry(artifacts)
    )
    assert result["action"] == "mark_succeeded"
    assert result["confidence"] == "high"
    assert result["sha256"]
    assert result["bytes"] == len(content.encode("utf-8"))
    assert db.get_open_tool_calls(task["task_id"]) == []


def test_human_intervention_parks_and_resumes_task_with_directive(db):
    task = _running_task(db)
    intervention_id = db.set_waiting_for_human(
        task["task_id"], "research_control_requires_human",
        {"message": "研究关键维度持续存在缺口。",
         "choices": ["continue_research", "change_research_direction",
                     "add_research_requirements", "terminate"]},
        worker_id="worker-lifecycle-test",
    )
    assert intervention_id
    assert db.get_task(task["task_id"])["status"] == "waiting_for_human"
    resolved = db.resolve_human_intervention(
        task["task_id"], intervention_id, "change_research_direction",
        {"reason": "补充官方源码证据并核验版本差异。",
         "direction": "补充官方源码证据并核验版本差异。"},
    )
    assert resolved["status"] == "queued"
    events = db.get_events(task["task_id"])
    decision = next(event for event in reversed(events)
                    if event["event_type"] == "human_intervention_resolved")
    assert decision["payload"]["payload"]["direction"].startswith("补充官方源码")
    assert decision["payload"]["category"] == "research_control_requires_human"


def test_event_limit_returns_latest_events_for_long_running_tasks(db):
    task = _running_task(db)
    for index in range(30):
        db.add_event(task["task_id"], "synthetic_progress", {"index": index})
    events = db.get_events(task["task_id"], limit=5)
    assert [event["payload"]["index"] for event in events] == [25, 26, 27, 28, 29]


def test_plan_step_completion_requires_existing_nonempty_readback_artifact(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    tracker = PlanProgressTracker(db, task["task_id"], artifacts)
    step = {
        "id": "analysis", "title": "证据分析", "objective": "综合证据",
        "expected_output": "分析报告", "expected_output_path": "reports/analysis.md",
    }
    db.add_event(task["task_id"], "research_plan_updated", {
        "version": 1, "plan": {
            "title": "研究计划", "objective": "完成研究",
            "steps": [step],
        },
    })

    tracker._append_status(step, "completed", "分析阶段已结束", "analysis", 1)
    event = [item for item in db.get_events(task["task_id"])
             if item["event_type"] == "research_plan_step_status"][-1]
    assert event["payload"]["status"] == "awaiting_verification"
    assert event["payload"]["verified_output"] is None

    artifacts.write_text("reports/analysis.md", "# Evidence analysis\n\nVerified output.")
    tracker._append_status(
        step, "completed", "分析报告已读回核验", "analysis", 1,
        output_path="reports/analysis.md",
    )
    event = [item for item in db.get_events(task["task_id"])
             if item["event_type"] == "research_plan_step_status"][-1]
    assert event["payload"]["status"] == "completed"
    assert event["payload"]["verified_output"]["path"] == "reports/analysis.md"
    assert len(event["payload"]["verified_output"]["sha256"]) == 64


def test_agent_cannot_mark_plan_step_completed_without_verified_artifact(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    db.add_event(task["task_id"], "research_plan_updated", {
        "version": 1, "plan": {
            "title": "研究计划", "objective": "完成研究",
            "steps": [{
                "id": "analysis", "title": "证据分析",
                "objective": "综合证据", "expected_output": "分析报告",
                "expected_output_path": "reports/analysis.md",
            }],
        },
    })
    service = ResearchToolService(
        settings, db, task["task_id"], artifacts, time.monotonic(),
    )
    with pytest.raises(ValueError, match="requires an existing"):
        service.update_plan_step_status("analysis", "completed")

    artifacts.write_text("reports/analysis.md", "# Analysis\n\nRead-back verified.")
    service.update_plan_step_status(
        "analysis", "completed", note="已完成并保存", output_path="reports/analysis.md",
    )
    event = [item for item in db.get_events(task["task_id"])
             if item["event_type"] == "research_plan_step_status"][-1]
    assert event["payload"]["status"] == "completed"
    assert event["payload"]["verified_output"]["bytes"] > 0


def test_recovery_manager_does_not_accept_historical_plan_as_current_write(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    old_plan = {
        "title": "旧计划", "objective": "旧目标",
        "steps": [{"id": "step-1", "title": "旧步骤", "objective": "旧工作",
                   "expected_output": "旧产物"}],
    }
    db.add_event(task["task_id"], "research_plan_updated", {
        "version": 1, "plan": old_plan,
    })
    candidate = {
        "title": "新计划", "objective": "新目标",
        "steps": [{"id": "step-1", "title": "新步骤", "objective": "新工作",
                   "expected_output": "新产物"}],
    }
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    result = manager._verify_unknown_call(
        task["task_id"],
        {"call_id": "unknown-plan-write", "tool_name": "update_research_plan",
         "input_json": json.dumps(candidate, ensure_ascii=False)},
        artifacts, EvidenceRegistry(artifacts),
    )
    assert result["action"] == "retry_allowed"
    assert "不完全一致" in result["reason"]

def test_research_toolset_exposes_both_research_status_and_human_decisions(settings, db):
    task = _running_task(db)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(
        settings, db, task["task_id"], artifacts, time.monotonic(),
    )
    tool_names = {item.name for item in service.as_langchain_tools()}
    assert "get_research_status" in tool_names
    assert "get_human_decisions" in tool_names

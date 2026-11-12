from __future__ import annotations

import json
from types import SimpleNamespace

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.recovery_manager import RecoveryManager
from app.runtime.research_controller import ResearchController
from app.runtime.research_evaluator import ResearchEvaluator


def _running_task(db, goal="验证研究闭环"):
    task = db.create_task(goal)
    claimed = db.claim_next_task("worker-lifecycle-test")
    assert claimed and claimed["task_id"] == task["task_id"]
    return db.get_task(task["task_id"])


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
        "goal", "requirements", "coverage", "sources", "findings",
        "rule_scores", "rule_defects", "report",
    }
    saved = db.list_quality_assessments(task["task_id"])
    assert len(saved) == 1
    assert saved[0]["assessment_id"] == assessment["assessment_id"]


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
    assert db.get_open_tool_calls(task["task_id"]) == []


def test_human_intervention_parks_and_resumes_task_with_directive(db):
    task = _running_task(db)
    intervention_id = db.set_waiting_for_human(
        task["task_id"], "quality_not_satisfied",
        {"message": "研究质量未达到门槛。"}, worker_id="worker-lifecycle-test",
    )
    assert intervention_id
    assert db.get_task(task["task_id"])["status"] == "waiting_for_human"
    resolved = db.resolve_human_intervention(
        task["task_id"], intervention_id, "change_research_direction",
        {"directive": "补充官方源码证据并核验版本差异。"},
    )
    assert resolved["status"] == "queued"
    events = db.get_events(task["task_id"])
    decision = next(event for event in reversed(events)
                    if event["event_type"] == "human_intervention_resolved")
    assert decision["payload"]["payload"]["directive"].startswith("补充官方源码")


def test_event_limit_returns_latest_events_for_long_running_tasks(db):
    task = _running_task(db)
    for index in range(30):
        db.add_event(task["task_id"], "synthetic_progress", {"index": index})
    events = db.get_events(task["task_id"], limit=5)
    assert [event["payload"]["index"] for event in events] == [25, 26, 27, 28, 29]

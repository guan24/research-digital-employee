from __future__ import annotations

import json
from typing import Any

from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.research_controller import ResearchController
from app.runtime.research_evaluator import ResearchEvaluator


class ResearchLifecycleCoordinator:
    """Coordinates the research-only controller/evaluator loop at executor boundaries."""

    def __init__(self, settings, db, task_id: str, artifacts,
                 reviewer=None, quality_profile: dict[str, Any] | None = None):
        self.settings = settings
        self.db = db
        self.task_id = task_id
        self.artifacts = artifacts
        self.registry = EvidenceRegistry(artifacts)
        self.controller = ResearchController(db, task_id, artifacts)
        self.evaluator = ResearchEvaluator(
            settings, db, task_id, artifacts, reviewer=reviewer, profile=quality_profile
        )

    def current_plan(self) -> dict[str, Any]:
        events = self.db.get_events(self.task_id, limit=5000)
        event = next((item for item in reversed(events)
                      if item.get("event_type") == "research_plan_updated"), None)
        return (event or {}).get("payload", {}).get("plan") or {}

    def ensure_research_contract(self) -> dict[str, Any]:
        plan = self.current_plan()
        requirements = self.controller.ensure_requirements(plan)
        self.db.add_event(self.task_id, "research_contract_ready", {
            "requirements_version": requirements.get("version", 1),
            "dimension_count": len(requirements.get("dimensions") or []),
            "source": requirements.get("source", "agent"),
        })
        return requirements

    def assess_progress(self, trigger: str = "phase_boundary") -> dict[str, Any]:
        requirements = self.ensure_research_contract()
        coverage = self.controller.assess_coverage(
            self.registry.summary(), self.current_plan(), trigger=trigger
        )
        decision = self.controller.decide(coverage, self.db.get_budget_status(self.task_id))
        result = {"requirements": requirements, "coverage": coverage, "decision": decision}
        self.db.add_event(self.task_id, "research_lifecycle_observed", {
            "trigger": trigger, "coverage_ratio": coverage.get("coverage_ratio"),
            "recommended_action": decision.get("recommended_action"),
            "critical_gap_count": len(coverage.get("critical_gaps") or []),
        })
        return result

    def evaluate_delivery(self, task: dict[str, Any], report: str, validation: Any = None,
                          callback=None, allow_llm: bool = True) -> dict[str, Any]:
        requirements = self.ensure_research_contract()
        registry = self.registry.summary()
        for source in registry.get("sources", []):
            path = source.get("snapshot_path")
            try:
                snapshot = self.artifacts.read_text(path) if path else ""
                source["snapshot_exists"] = bool(snapshot)
                source["snapshot_integrity"] = bool(
                    snapshot and source.get("content_hash") in snapshot
                    and source.get("url") in snapshot
                )
            except (OSError, ValueError):
                source["snapshot_exists"] = False
                source["snapshot_integrity"] = False
        coverage = self.controller.assess_coverage(
            registry, self.current_plan(), trigger="final_delivery"
        )
        assessment = self.evaluator.evaluate(
            task, report, registry, coverage, validation=validation,
            callback=callback, allow_llm=allow_llm, stage="final",
        )
        decision = self.controller.decide(
            coverage, self.db.get_budget_status(self.task_id), assessment
        )
        assessment["control_decision"] = decision
        assessment["requirements_version"] = requirements.get("version", 1)
        self.db.add_event(self.task_id, "research_lifecycle_evaluated", {
            "assessment_id": assessment.get("assessment_id"),
            "total_score": assessment.get("total_score"),
            "passed": assessment.get("passed"),
            "recommended_action": decision.get("recommended_action"),
            "hard_block_count": len(assessment.get("hard_blocks") or []),
            "defect_count": len(assessment.get("defects") or []),
        })
        return assessment

    @staticmethod
    def repair_prompt(assessment: dict[str, Any]) -> str:
        defects = assessment.get("defects") or []
        return (
            "独立研究质量评估未通过。请只针对以下结构化缺陷进行定向补充研究或报告修复，"
            "不要重复已经完成且有证据的工作。若需要新增研究步骤，必须调用 update_research_plan "
            "并填写完整的 trigger、new_information、problem_analysis、decision、changes。"
            "报告修复后必须覆盖原报告文件，并保持来源引用可核验。\n\n"
            + json.dumps({
                "total_score": assessment.get("total_score"),
                "dimension_scores": assessment.get("dimension_scores"),
                "hard_blocks": assessment.get("hard_blocks"),
                "defects": defects,
                "control_decision": assessment.get("control_decision"),
            }, ensure_ascii=False, indent=2)
        )

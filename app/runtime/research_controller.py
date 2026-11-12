from __future__ import annotations

import json
from typing import Any

from app.infrastructure.database import Database


class ResearchController:
    """Deterministic evidence-coverage controller for the research role.

    The controller does not search or rewrite plans. It persists a normalized
    research contract, computes coverage from registered findings, and returns
    a bounded set of actions for the Agent/Coordinator to choose from.
    """

    PRIORITY_WEIGHT = {"critical": 3.0, "high": 2.0, "normal": 1.0}

    def __init__(self, db: Database, task_id: str, artifacts=None):
        self.db = db
        self.task_id = task_id
        self.artifacts = artifacts

    @staticmethod
    def _normalize_requirements(value: dict[str, Any]) -> dict[str, Any]:
        raw_dimensions = value.get("dimensions")
        if not isinstance(raw_dimensions, list) or not raw_dimensions:
            raise ValueError("requirements must contain a non-empty dimensions array")
        if len(raw_dimensions) > 20:
            raise ValueError("at most 20 research dimensions are allowed")
        dimensions = []
        seen = set()
        for index, item in enumerate(raw_dimensions, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"dimension {index} must be an object")
            dimension_id = str(item.get("id") or "").strip().lower()
            title = str(item.get("title") or "").strip()
            if not dimension_id or not title or dimension_id in seen:
                raise ValueError(f"dimension {index} requires a unique id and title")
            if len(dimension_id) > 64:
                raise ValueError("dimension id exceeds 64 characters")
            priority = str(item.get("priority") or "normal").strip().lower()
            if priority not in ResearchController.PRIORITY_WEIGHT:
                raise ValueError(f"invalid priority for dimension {dimension_id}")
            min_sources = int(item.get("min_sources") or 2)
            if not 1 <= min_sources <= 10:
                raise ValueError("min_sources must be between 1 and 10")
            questions = item.get("questions") or []
            expected = item.get("expected_evidence") or []
            if not isinstance(questions, list) or not isinstance(expected, list):
                raise ValueError("questions and expected_evidence must be arrays")
            dimensions.append({
                "id": dimension_id,
                "title": title[:240],
                "questions": [str(v).strip()[:500] for v in questions if str(v).strip()][:12],
                "expected_evidence": [str(v).strip()[:240] for v in expected if str(v).strip()][:12],
                "priority": priority,
                "min_sources": min_sources,
            })
            seen.add(dimension_id)
        return {
            "schema_version": 1,
            "dimensions": dimensions,
            "source": str(value.get("source") or "agent"),
        }

    def define_requirements(self, requirements: dict[str, Any], source: str = "agent") -> dict[str, Any]:
        normalized = self._normalize_requirements({**requirements, "source": source})
        current = self.db.get_research_control_state(self.task_id) or {}
        previous = current.get("requirements") or {}
        previous_dimensions = {item["id"]: item for item in previous.get("dimensions", [])}
        current_dimensions = {item["id"]: item for item in normalized["dimensions"]}
        added = sorted(set(current_dimensions) - set(previous_dimensions))
        removed = sorted(set(previous_dimensions) - set(current_dimensions))
        changed = sorted(
            key for key in set(current_dimensions) & set(previous_dimensions)
            if current_dimensions[key] != previous_dimensions[key]
        )
        if previous and not (added or removed or changed):
            return {"saved": False, "version": int(current.get("requirements_version") or 1),
                    "message": "No material research requirement change."}
        version = int(current.get("requirements_version") or 0) + 1
        normalized["version"] = version
        state = {
            "phase": current.get("phase") or "planning",
            "cycle": int(current.get("cycle") or 0),
            "requirements_version": version,
            "requirements": normalized,
            "coverage": current.get("coverage") or {},
            "progress_vector": current.get("progress_vector") or {},
            "stagnation_count": 0 if previous else int(current.get("stagnation_count") or 0),
            "last_assessment_id": current.get("last_assessment_id"),
        }
        self.db.save_research_control_state(self.task_id, state)
        self.db.add_event(self.task_id, "research_requirements_updated", {
            "version": version, "source": source, "dimension_count": len(normalized["dimensions"]),
            "added": added, "removed": removed, "changed": changed,
        })
        return {"saved": True, "version": version, "added": added, "removed": removed, "changed": changed}

    def ensure_requirements(self, plan: dict[str, Any] | None) -> dict[str, Any]:
        state = self.db.get_research_control_state(self.task_id) or {}
        existing = state.get("requirements") or {}
        if existing.get("dimensions"):
            return existing
        steps = (plan or {}).get("steps") or []
        dimensions = []
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            title = str(step.get("title") or "").strip()
            if not title:
                continue
            dimension_id = str(step.get("id") or f"step-{index}").strip().lower()
            dimensions.append({
                "id": dimension_id[:64],
                "title": title,
                "questions": [str(step.get("objective") or title)],
                "expected_evidence": [str(step.get("expected_output") or "可核验的研究产物")],
                "priority": "high" if index == 1 else "normal",
                "min_sources": 1,
            })
        if not dimensions:
            dimensions = [{
                "id": "research_scope",
                "title": "回答用户研究目标",
                "questions": [str((self.db.get_task(self.task_id) or {}).get("goal") or "")[:500]],
                "expected_evidence": ["可核验的一手或权威来源"],
                "priority": "critical",
                "min_sources": 2,
            }]
        result = self.define_requirements({"dimensions": dimensions}, source="plan_fallback")
        return (self.db.get_research_control_state(self.task_id) or {}).get("requirements") or {
            "version": result.get("version", 1), "dimensions": dimensions, "source": "plan_fallback"
        }

    def _source_integrity(self, source: dict[str, Any]) -> bool:
        if not source.get("verified") or not source.get("snapshot_path"):
            return False
        if self.artifacts is None:
            return bool(source.get("snapshot_integrity", True))
        try:
            snapshot = self.artifacts.read_text(source["snapshot_path"])
        except (FileNotFoundError, OSError, ValueError):
            return False
        expected_hash = str(source.get("content_hash") or "")
        return bool(expected_hash and expected_hash in snapshot and str(source.get("url") or "") in snapshot)

    def assess_coverage(self, registry: dict[str, Any], plan: dict[str, Any] | None = None,
                        trigger: str = "phase_boundary") -> dict[str, Any]:
        state = self.db.get_research_control_state(self.task_id) or {}
        requirements = state.get("requirements") or {}
        if not requirements.get("dimensions"):
            requirements = self.ensure_requirements(plan)
            state = self.db.get_research_control_state(self.task_id) or {}
        sources = [item for item in (registry.get("sources") or []) if isinstance(item, dict)]
        findings = [item for item in (registry.get("findings") or []) if isinstance(item, dict)]
        known_sources = {str(item.get("source_id") or "").upper(): item for item in sources}
        dimensions = []
        weighted_covered = 0.0
        total_weight = 0.0
        critical_gaps = []
        for requirement in requirements.get("dimensions", []):
            dimension_id = requirement["id"]
            linked_findings = [
                finding for finding in findings
                if str(finding.get("dimension_id") or "").lower() == dimension_id
            ]
            linked_ids = {
                str(source_id).upper()
                for finding in linked_findings
                for source_id in (finding.get("source_ids") or [])
            }
            valid_ids = sorted(
                source_id for source_id in linked_ids
                if source_id in known_sources and self._source_integrity(known_sources[source_id])
            )
            minimum = int(requirement.get("min_sources") or 1)
            ratio = min(1.0, len(valid_ids) / max(1, minimum))
            status = "covered" if ratio >= 1 else ("partial" if valid_ids or linked_findings else "uncovered")
            if requirement.get("priority") == "critical" and status != "covered":
                critical_gaps.append({
                    "dimension_id": dimension_id, "title": requirement["title"],
                    "status": status, "valid_source_count": len(valid_ids), "min_sources": minimum,
                })
            weight = self.PRIORITY_WEIGHT.get(requirement.get("priority"), 1.0)
            total_weight += weight
            weighted_covered += ratio * weight
            dimensions.append({
                **requirement, "status": status, "valid_source_ids": valid_ids,
                "valid_source_count": len(valid_ids), "finding_count": len(linked_findings),
                "coverage_ratio": round(ratio, 4),
            })
        coverage_ratio = weighted_covered / total_weight if total_weight else 0.0
        progress_vector = {
            "coverage_ratio": round(coverage_ratio, 4),
            "valid_source_count": sum(item["valid_source_count"] for item in dimensions),
            "finding_count": len(findings),
            "critical_gap_count": len(critical_gaps),
        }
        previous = state.get("progress_vector") or {}
        improved = (
            progress_vector["coverage_ratio"] - float(previous.get("coverage_ratio") or 0) >= 0.03
            or progress_vector["valid_source_count"] > int(previous.get("valid_source_count") or 0)
            or progress_vector["finding_count"] > int(previous.get("finding_count") or 0)
            or progress_vector["critical_gap_count"] < int(previous.get("critical_gap_count") or 0)
        )
        stagnation = 0 if improved or not previous else int(state.get("stagnation_count") or 0) + 1
        snapshot = {
            "trigger": trigger,
            "requirements_version": int(state.get("requirements_version") or requirements.get("version") or 1),
            "coverage_ratio": round(coverage_ratio, 4),
            "dimensions": dimensions,
            "critical_gaps": critical_gaps,
            "uncovered_dimensions": [item["id"] for item in dimensions if item["status"] == "uncovered"],
            "partial_dimensions": [item["id"] for item in dimensions if item["status"] == "partial"],
            "progress_vector": progress_vector,
            "stagnation_count": stagnation,
            "stagnated": stagnation >= 2,
        }
        self.db.save_research_control_state(self.task_id, {
            "phase": state.get("phase") or "evidence_collection",
            "cycle": int(state.get("cycle") or 0) + 1,
            "requirements_version": snapshot["requirements_version"],
            "requirements": requirements,
            "coverage": snapshot,
            "progress_vector": progress_vector,
            "stagnation_count": stagnation,
            "last_assessment_id": state.get("last_assessment_id"),
        })
        self.db.add_event(self.task_id, "research_coverage_assessed", {
            "trigger": trigger, "coverage_ratio": snapshot["coverage_ratio"],
            "critical_gap_count": len(critical_gaps), "stagnation_count": stagnation,
            "dimension_statuses": {item["id"]: item["status"] for item in dimensions},
        })
        return snapshot

    def decide(self, coverage: dict[str, Any], budget: dict[str, Any],
               quality: dict[str, Any] | None = None) -> dict[str, Any]:
        phase = str(budget.get("phase") or "research")
        if phase == "convergence":
            recommended = "ENTER_CONVERGENCE"
            allowed = ["ENTER_CONVERGENCE", "REQUEST_HUMAN"]
            reason = "预算已进入收敛阶段，停止新增探索并优先完成综合与交付。"
        elif coverage.get("stagnated") and coverage.get("critical_gaps"):
            recommended = "REQUEST_HUMAN"
            allowed = ["REQUEST_HUMAN", "ENTER_CONVERGENCE"]
            reason = "连续两个周期没有实质性进展，且仍存在关键研究缺口。"
        elif coverage.get("critical_gaps") or coverage.get("uncovered_dimensions"):
            recommended = "FILL_EVIDENCE_GAP"
            allowed = ["FILL_EVIDENCE_GAP", "REPLAN", "CONTINUE_RESEARCH"]
            reason = "研究覆盖矩阵仍存在未满足的证据要求。"
        elif quality and not quality.get("passed"):
            recommended = "FILL_EVIDENCE_GAP"
            allowed = ["FILL_EVIDENCE_GAP", "REPLAN", "ENTER_CONVERGENCE", "REQUEST_HUMAN"]
            reason = "质量评估未通过，需要针对缺陷继续研究或修复。"
        else:
            recommended = "CONTINUE_RESEARCH"
            allowed = ["CONTINUE_RESEARCH", "ENTER_CONVERGENCE"]
            reason = "当前没有触发硬性收敛条件，可继续执行计划。"
        decision = {
            "recommended_action": recommended,
            "allowed_actions": allowed,
            "reason": reason,
            "coverage_ratio": coverage.get("coverage_ratio", 0),
            "critical_gaps": coverage.get("critical_gaps", []),
            "stagnation_count": coverage.get("stagnation_count", 0),
            "budget_phase": phase,
        }
        self.db.add_event(self.task_id, "research_control_decision", decision)
        return decision

    @staticmethod
    def decision_prompt(decision: dict[str, Any]) -> str:
        return (
            "系统研究控制器已完成证据覆盖评估。你必须在 allowed_actions 中选择下一步行动，"
            "不得绕过预算和质量硬门槛。若需要补充研究，请优先针对 critical_gaps，"
            "并在必要时通过 update_research_plan 记录完整的五项重规划审计信息。\n"
            + json.dumps(decision, ensure_ascii=False, indent=2)
        )

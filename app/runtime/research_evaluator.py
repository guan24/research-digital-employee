from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from app.infrastructure.database import Database


DIMENSIONS = (
    "goal_coverage", "evidence_sufficiency", "source_reliability",
    "conclusion_consistency", "report_completeness",
)
DEFAULT_WEIGHTS = {
    "goal_coverage": 20.0,
    "evidence_sufficiency": 25.0,
    "source_reliability": 20.0,
    "conclusion_consistency": 20.0,
    "report_completeness": 15.0,
}


class EvaluatorUnavailable(RuntimeError):
    """Independent LLM review did not return a valid structured assessment."""


class ResearchEvaluator:
    """Deterministic rule evaluation plus an isolated review call using OPENAI_MODEL."""

    def __init__(self, settings, db: Database, task_id: str, artifacts,
                 reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 profile: dict[str, Any] | None = None):
        self.settings = settings
        self.db = db
        self.task_id = task_id
        self.artifacts = artifacts
        self.reviewer = reviewer
        self.profile = profile or self._load_profile()

    def _load_profile(self) -> dict[str, Any]:
        path = Path(__file__).resolve().parents[1] / "agents" / "research" / "profile.toml"
        try:
            import tomllib
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, ValueError):
            raw = {}
        weights = raw.get("quality_weights") or DEFAULT_WEIGHTS
        normalized = {key: max(0.0, float(weights.get(key, DEFAULT_WEIGHTS[key]))) for key in DIMENSIONS}
        total = sum(normalized.values()) or 100.0
        return {
            "weights": {key: value * 100.0 / total for key, value in normalized.items()},
            "min_total_score": float(raw.get("quality_min_total_score", 80)),
            "min_dimension_score": float(raw.get("quality_min_dimension_score", 60)),
            "max_revisions": max(0, int(raw.get("quality_max_revisions", 2))),
            "rule_ratio": min(0.9, max(0.1, float(raw.get("quality_rule_ratio", 0.6)))),
            "review_attempts": 2,
        }

    @staticmethod
    def _valid_source(source: dict[str, Any], artifacts) -> bool:
        if not source.get("verified") or not source.get("snapshot_path"):
            return False
        try:
            snapshot = artifacts.read_text(source["snapshot_path"])
        except (FileNotFoundError, OSError, ValueError):
            return False
        digest = str(source.get("content_hash") or "")
        return bool(digest and digest in snapshot and str(source.get("url") or "") in snapshot)

    @staticmethod
    def _completeness(report: str) -> float:
        headings = [item.lower() for item in re.findall(r"^#{1,3}\s+(.+)$", report, re.MULTILINE)]
        groups = {
            "scope": ("范围", "method", "scope", "方法"),
            "findings": ("发现", "结果", "findings", "analysis", "分析"),
            "limitations": ("局限", "limitations", "不足"),
            "conclusion": ("结论", "conclusion", "建议"),
            "sources": ("sources", "references", "参考资料", "参考文献", "来源"),
        }
        present = sum(1 for tokens in groups.values() if any(token in heading for heading in headings for token in tokens))
        length_score = min(100.0, len(report.strip()) / 18.0)
        return round(65.0 * present / len(groups) + 35.0 * length_score / 100.0, 2)

    def evaluate_rules(self, report: str, registry: dict[str, Any],
                       coverage: dict[str, Any], validation: Any = None) -> dict[str, Any]:
        text = str(report or "").strip()
        sources = [item for item in registry.get("sources", []) if isinstance(item, dict)]
        findings = [item for item in registry.get("findings", []) if isinstance(item, dict)]
        valid_sources = [item for item in sources if self._valid_source(item, self.artifacts)]
        dimensions = coverage.get("dimensions") or []
        goal_score = float(coverage.get("coverage_ratio") or 0) * 100
        evidence_values = [
            min(100.0, int(item.get("valid_source_count") or 0) /
                max(1, int(item.get("min_sources") or 1)) * 100)
            for item in dimensions
        ]
        evidence_score = sum(evidence_values) / len(evidence_values) if evidence_values else 0.0
        reliability_score = len(valid_sources) / len(sources) * 100 if sources else 0.0
        conflict_count = 0
        positive = ("increases", "promotes", "improves", "增加", "促进", "改善")
        negative = ("decreases", "inhibits", "worsens", "降低", "抑制", "恶化")
        claims = [str(item.get("claim") or "").lower() for item in findings]
        for index, left in enumerate(claims):
            for right in claims[index + 1:]:
                left_words = set(re.findall(r"\w{4,}", left))
                right_words = set(re.findall(r"\w{4,}", right))
                if len(left_words & right_words) >= 2 and (
                    any(word in left for word in positive) and any(word in right for word in negative)
                    or any(word in left for word in negative) and any(word in right for word in positive)
                ):
                    conflict_count += 1
        consistency_score = max(0.0, 100.0 - min(80.0, conflict_count * 20.0))
        scores = {
            "goal_coverage": round(goal_score, 2),
            "evidence_sufficiency": round(evidence_score, 2),
            "source_reliability": round(reliability_score, 2),
            "conclusion_consistency": round(consistency_score, 2),
            "report_completeness": self._completeness(text),
        }
        hard_blocks = []
        defects = []
        if not text:
            hard_blocks.append({"code": "empty_report", "waivable": False, "message": "报告正文为空。"})
        if not sources:
            hard_blocks.append({"code": "no_registered_sources", "waivable": False, "message": "没有登记来源。"})
        invalid_ids = [str(item.get("source_id") or "unknown") for item in sources
                       if not self._valid_source(item, self.artifacts)]
        if invalid_ids:
            hard_blocks.append({"code": "snapshot_integrity_failure", "waivable": False,
                                "source_ids": invalid_ids, "message": "来源快照缺失或完整性校验失败。"})
        for error in getattr(validation, "errors", ()) or ():
            error = str(error)
            if "不存在的来源 ID" in error:
                hard_blocks.append({"code": "unknown_source_id", "waivable": False, "message": error})
            else:
                defects.append({"code": "report_validation", "severity": "major",
                                "waivable": True, "message": error})
        for gap in coverage.get("critical_gaps") or []:
            defects.append({
                "code": "critical_dimension_gap", "severity": "critical", "waivable": True,
                "dimension_id": gap.get("dimension_id"),
                "message": "关键研究维度证据不足：" + str(gap.get("title") or ""),
                "repair_action": "补充该维度的一手或权威来源，并记录关联 Finding。",
            })
        if conflict_count:
            defects.append({"code": "possible_finding_conflict", "severity": "major",
                            "waivable": True, "message": f"检测到 {conflict_count} 组可能矛盾的 Finding。"})
        return {
            "scores": scores, "hard_blocks": hard_blocks, "defects": defects,
            "valid_source_count": len(valid_sources), "registered_source_count": len(sources),
            "finding_count": len(findings), "rule_conflict_count": conflict_count,
        }

    @staticmethod
    def _normalize_review_result(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("scores"), dict):
            raise ValueError("reviewer response must contain a scores object")
        scores = {}
        for key in DIMENSIONS:
            if key not in value["scores"]:
                raise ValueError("reviewer omitted score dimension: " + key)
            if isinstance(value["scores"][key], bool):
                raise ValueError("reviewer score must be numeric: " + key)
            try:
                score = float(value["scores"][key])
            except (TypeError, ValueError) as exc:
                raise ValueError("reviewer score is not numeric: " + key) from exc
            if not math.isfinite(score) or not 0 <= score <= 100:
                raise ValueError("reviewer score outside 0-100: " + key)
            scores[key] = round(score, 2)
        normalized = dict(value)
        normalized["scores"] = scores
        for key in ("unsupported_claims", "contradictions", "missing_dimensions", "actionable_repairs"):
            if not isinstance(normalized.get(key, []), list):
                raise ValueError("reviewer field must be an array: " + key)
            normalized.setdefault(key, [])
        normalized["status"] = normalized.get("status") or "completed"
        return normalized

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        if isinstance(content, list):
            content = "\n".join(str(item.get("text") or item) if isinstance(item, dict) else str(item) for item in content)
        text = str(content or "").strip()
        fence = chr(96) * 3
        text = text.replace(fence + "json", "").replace(fence, "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM reviewer did not return a JSON object")
        return ResearchEvaluator._normalize_review_result(json.loads(text[start:end + 1]))

    def review_with_llm(self, context: dict[str, Any], callback=None) -> dict[str, Any]:
        if self.reviewer is not None:
            return self._normalize_review_result(self.reviewer(context))
        if not getattr(self.settings, "openai_api_key", ""):
            raise EvaluatorUnavailable("OPENAI_API_KEY is not configured for independent review")
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage

        model = ChatOpenAI(
            model=self.settings.openai_model, api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url, temperature=0,
            timeout=self.settings.model_timeout_seconds, max_retries=0, max_tokens=1400,
        )
        system = (
            "你是独立研究质量评审器，不参与研究执行，也不继承 Agent 对话历史。"
            "只依据目标、研究维度、报告、来源快照摘录和登记证据评审，不得编造来源。"
            "逐条检查关键结论是否被对应快照实质支持；仅有 URL 或标题不构成支持。"
            "明确区分直接支持、间接支持、矛盾和无法从摘录确认的主张。"
            "严格输出 JSON：scores（五个 0-100 分数：goal_coverage、evidence_sufficiency、"
            "source_reliability、conclusion_consistency、report_completeness）、unsupported_claims、"
            "contradictions、missing_dimensions、actionable_repairs、summary。"
        )
        attempts = max(1, min(int(self.profile.get("review_attempts", 2)), 3))
        last_error = None
        for _ in range(attempts):
            try:
                config = {"callbacks": [callback]} if callback is not None else None
                response = model.invoke(
                    [SystemMessage(content=system),
                     HumanMessage(content=json.dumps(context, ensure_ascii=False, default=str)[:42000])],
                    config=config,
                )
                parsed = self._parse_json(getattr(response, "content", response))
                parsed["status"] = "completed"
                return parsed
            except Exception as exc:
                last_error = exc
        raise EvaluatorUnavailable(f"independent review failed after {attempts} attempts: {last_error}") from last_error

    def evaluate(self, task: dict[str, Any], report: str, registry: dict[str, Any],
                 coverage: dict[str, Any], validation: Any = None, callback=None,
                 allow_llm: bool = True, stage: str = "final") -> dict[str, Any]:
        rules = self.evaluate_rules(report, registry, coverage, validation)
        if rules["hard_blocks"]:
            reviewer = {"status": "skipped_hard_block"}
            llm_scores = {key: 0.0 for key in DIMENSIONS}
        elif allow_llm:
            finding_source_ids = {
                str(source_id).upper()
                for finding in registry.get("findings", []) if isinstance(finding, dict)
                for source_id in (finding.get("source_ids") or [])
            }
            source_records = [item for item in registry.get("sources", []) if isinstance(item, dict)]
            source_records.sort(key=lambda item: (
                str(item.get("source_id") or "").upper() not in finding_source_ids,
                str(item.get("source_id") or ""),
            ))
            sources = []
            for item in source_records[:8]:
                excerpt = ""
                if self._valid_source(item, self.artifacts):
                    try:
                        excerpt = self.artifacts.read_text(item["snapshot_path"])[-1000:]
                    except (OSError, ValueError):
                        excerpt = ""
                sources.append({
                    **{key: item.get(key) for key in ("source_id", "title", "url", "source_type", "verified", "content_hash")},
                    "snapshot_excerpt": excerpt,
                })
            findings = [{
                "finding_id": item.get("finding_id"),
                "dimension_id": item.get("dimension_id"),
                "claim": str(item.get("claim") or "")[:300],
                "source_ids": list(item.get("source_ids") or [])[:5],
                "confidence": item.get("confidence"),
            } for item in registry.get("findings", []) if isinstance(item, dict)]
            report_sha256 = hashlib.sha256(str(report or "").encode("utf-8")).hexdigest()
            context = {
                "goal": task.get("goal", ""),
                "requirements": (self.db.get_research_control_state(self.task_id) or {}).get("requirements", {}),
                "coverage": coverage, "registered_source_count": len(source_records),
                "sources": sources, "findings": findings[:30],
                "rule_scores": rules["scores"], "rule_defects": rules["defects"],
                "report_sha256": report_sha256, "report": str(report or "")[:16000],
            }
            try:
                reviewer = self.review_with_llm(context, callback)
                llm_scores = reviewer["scores"]
            except EvaluatorUnavailable as exc:
                reviewer = {"status": "unavailable", "error": str(exc)[:1200]}
                llm_scores = dict(rules["scores"])
        else:
            reviewer = {"status": "offline_rules_only"}
            llm_scores = dict(rules["scores"])
        ratio = float(self.profile.get("rule_ratio", 0.6))
        combined = {key: round(rules["scores"][key] * ratio + llm_scores[key] * (1 - ratio), 2)
                    for key in DIMENSIONS}
        weights = self.profile.get("weights") or DEFAULT_WEIGHTS
        total = round(sum(combined[key] * float(weights.get(key, DEFAULT_WEIGHTS[key])) / 100 for key in DIMENSIONS), 2)
        control_state = self.db.get_research_control_state(self.task_id) or {}
        quality_policy = (control_state.get("requirements") or {}).get("quality_policy") or {}
        min_total_score = max(
            float(self.profile.get("min_total_score", 80)),
            float(quality_policy.get("min_total_score", 0)),
        )
        min_dimension_score = max(
            float(self.profile.get("min_dimension_score", 60)),
            float(quality_policy.get("min_dimension_score", 0)),
        )
        report_sha256 = hashlib.sha256(str(report or "").encode("utf-8")).hexdigest()
        reviewer = dict(reviewer)
        reviewer["report_sha256"] = report_sha256
        defects = list(rules["defects"])
        for field, code in (("unsupported_claims", "unsupported_claim"),
                            ("contradictions", "reviewer_contradiction"),
                            ("missing_dimensions", "reviewer_missing_dimension")):
            for item in reviewer.get(field, []) if isinstance(reviewer, dict) else []:
                defects.append({"code": code, "severity": "critical", "waivable": True,
                                "message": str(item)[:1200], "repair_action": "补充证据或修订结论。"})
        if reviewer.get("status") == "unavailable":
            defects.append({"code": "independent_review_unavailable", "severity": "critical",
                            "waivable": True, "message": reviewer.get("error", "独立评审不可用。"),
                            "repair_action": "等待人工决定是否继续评审或采用降级交付。"})
        critical_defects = [item for item in defects if item.get("severity") == "critical"]
        passed = (
            not rules["hard_blocks"]
            and not critical_defects
            and total >= min_total_score
            and all(value >= min_dimension_score for value in combined.values())
            and reviewer.get("status") in {"completed", "offline_rules_only"}
        )
        assessment = {
            "assessment_id": str(uuid.uuid4()), "stage": stage, "total_score": total,
            "report_sha256": report_sha256,
            "dimension_scores": {
                key: {"score": combined[key], "rule_score": rules["scores"][key],
                      "llm_score": llm_scores[key], "weight": float(weights.get(key, DEFAULT_WEIGHTS[key]))}
                for key in DIMENSIONS
            },
            "passed": passed, "hard_blocks": rules["hard_blocks"], "defects": defects,
            "reviewer": reviewer,
            "thresholds": {"min_total_score": min_total_score,
                           "min_dimension_score": min_dimension_score,
                           "role_min_total_score": self.profile.get("min_total_score", 80),
                           "role_min_dimension_score": self.profile.get("min_dimension_score", 60)},
            "rule_metrics": {key: value for key, value in rules.items()
                             if key not in {"scores", "hard_blocks", "defects"}},
            "repairable": not rules["hard_blocks"] and bool(defects),
        }
        assessment_id = self.db.save_quality_assessment(self.task_id, assessment)
        assessment["assessment_id"] = assessment_id
        state = self.db.get_research_control_state(self.task_id) or {}
        self.db.save_research_control_state(self.task_id, {
            "phase": "evaluation", "cycle": int(state.get("cycle") or 0),
            "requirements_version": int(state.get("requirements_version") or 0),
            "requirements": state.get("requirements") or {}, "coverage": state.get("coverage") or coverage,
            "progress_vector": state.get("progress_vector") or {},
            "stagnation_count": int(state.get("stagnation_count") or 0),
            "last_assessment_id": assessment_id,
        })
        return assessment

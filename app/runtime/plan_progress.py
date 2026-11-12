from __future__ import annotations

import json
from typing import Any

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database


class PlanProgressTracker:
    """Persist coarse plan progress inferred from observable research tool phases."""

    PHASE_ORDER = {
        "scope": 0,
        "discovery": 1,
        "reading": 2,
        "analysis": 3,
        "report": 4,
        "validation": 5,
    }
    PHASE_LABELS = {
        "scope": "研究范围确认",
        "discovery": "资料检索",
        "reading": "来源阅读",
        "analysis": "证据分析",
        "report": "报告撰写",
        "validation": "交付校验",
    }
    STEP_KEYWORDS = {
        "scope": ("研究范围", "明确范围", "界定范围", "研究目标", "研究问题", "评价标准", "scope", "research question", "criteria"),
        "discovery": ("搜索", "检索", "搜集", "收集", "查找", "调研", "文献", "资料", "search", "discover", "collect", "literature", "repository"),
        "reading": ("阅读", "读取", "研读", "解读", "提取", "源码", "文档", "论文", "材料", "read", "inspect", "review", "extract", "documentation", "paper", "source code"),
        "analysis": ("分析", "比较", "对比", "归纳", "综合", "评估", "评价", "总结", "研究发现", "analysis", "compare", "evaluate", "synthesize", "findings", "assess"),
        "report": ("报告", "撰写", "写作", "编写", "成稿", "交付", "report", "write", "draft", "deliver"),
        "validation": ("核验", "验证", "校验", "复核", "检查", "引用", "来源", "质量", "verify", "validate", "audit", "citation", "quality"),
    }

    def __init__(self, db: Database, task_id: str, artifacts: ArtifactStore):
        self.db = db
        self.task_id = task_id
        self.artifacts = artifacts

    @staticmethod
    def _normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": str(plan.get("title") or "研究执行计划").strip(),
            "objective": str(plan.get("objective") or "").strip(),
            "steps": [
                {
                    "id": str(step.get("id") or f"step-{index}").strip(),
                    "title": str(step.get("title") or "").strip(),
                    "objective": str(step.get("objective") or "").strip(),
                    "expected_output": str(step.get("expected_output") or "").strip(),
                }
                for index, step in enumerate(plan.get("steps") or [], start=1)
                if isinstance(step, dict)
            ],
        }

    @staticmethod
    def _latest_progress(events: list[dict]) -> dict[str, dict[str, Any]]:
        progress: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.get("event_type") != "research_plan_step_status":
                continue
            payload = event.get("payload") or {}
            step_id = str(payload.get("step_id") or "")
            if not step_id:
                continue
            progress[step_id] = {
                "step_id": step_id,
                "status": payload.get("status") or "pending",
                "note": payload.get("note") or "",
                "source": payload.get("source") or "agent",
                "phase": payload.get("phase"),
                "plan_version": payload.get("plan_version"),
                "event_id": event.get("event_id", 0),
            }
        return progress

    @classmethod
    def _step_phase(cls, step: dict[str, Any]) -> str | None:
        text = " ".join(
            str(step.get(key) or "")
            for key in ("title", "objective", "expected_output")
        ).lower()
        for phase in ("report", "reading", "analysis", "validation", "discovery", "scope"):
            if any(term in text for term in cls.STEP_KEYWORDS[phase]):
                return phase
        return None

    @staticmethod
    def _tool_phase(tool_name: str, input_value: str) -> str | None:
        name = str(tool_name or "").strip().lower().rsplit(".", 1)[-1]
        if name in {
            "update_research_plan", "update_plan_step_status",
            "read_research_memory", "save_research_memory",
            "get_budget_status", "request_budget_extension", "list_artifacts",
        }:
            return None
        if name in {"web_search", "arxiv_search", "github_repo", "github_tree", "github_releases"}:
            return "discovery"
        if name in {"fetch_url", "fetch_pdf", "github_read", "arxiv_paper"}:
            return "reading"
        if name == "record_finding":
            return "analysis"

        try:
            arguments = json.loads(str(input_value or ""))
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        path = str(arguments.get("path") or "").strip().lower() if isinstance(arguments, dict) else ""
        if name == "save_artifact":
            if path in {"notes/research_plan.json", "notes/research_memory.md"} or "memory_versions/" in path:
                return None
            if "report" in path or path.startswith("reports/"):
                return "report"
            return "analysis"
        if name == "read_artifact":
            if "report" in path or path.startswith("reports/"):
                return "validation"
            return "reading" if path.startswith("sources/") else "analysis"
        return None

    def _append_status(
        self, step: dict[str, Any], status: str, note: str, phase: str, version: int,
    ) -> None:
        events = self.db.get_events(self.task_id, limit=5000)
        progress = self._latest_progress(events)
        previous = progress.get(step["id"], {}).get("status", "pending")
        self.db.add_event(self.task_id, "research_plan_step_status", {
            "step_id": step["id"],
            "step_title": step["title"],
            "status": status,
            "previous_status": previous,
            "note": str(note or "")[:1500],
            "plan_version": version,
            "source": "runtime",
            "phase": phase,
        })
        current_events = self.db.get_events(self.task_id, limit=5000)
        current_progress = self._latest_progress(current_events)
        latest_plan_event = next(
            (event for event in reversed(current_events) if event.get("event_type") == "research_plan_updated"),
            None,
        )
        if latest_plan_event is None:
            return
        plan = self._normalize_plan((latest_plan_event.get("payload") or {}).get("plan") or {})
        plan_with_progress = {
            "schema_version": 1,
            "version": version,
            **plan,
            "steps": [
                {
                    **item,
                    "status": current_progress.get(item["id"], {}).get("status", "pending"),
                    "status_note": current_progress.get(item["id"], {}).get("note", ""),
                }
                for item in plan["steps"]
            ],
        }
        self.artifacts.write_text(
            "notes/research_plan.json",
            json.dumps(plan_with_progress, ensure_ascii=False, indent=2),
        )

    def sync_plan_progress_for_tool(self, tool_name: str, input_value: str) -> str | None:
        phase = self._tool_phase(tool_name, input_value)
        if phase is None:
            return None
        return self.sync_plan_progress_for_phase(phase, f"检测到工具 {tool_name} 开始执行")

    def sync_plan_progress_for_phase(self, phase: str, note: str = "") -> str | None:
        if phase not in self.PHASE_ORDER:
            return None
        events = self.db.get_events(self.task_id, limit=5000)
        plan_event = next(
            (event for event in reversed(events) if event.get("event_type") == "research_plan_updated"),
            None,
        )
        if plan_event is None:
            return None
        payload = plan_event.get("payload") or {}
        version = int(payload.get("version") or 1)
        plan = self._normalize_plan(payload.get("plan") or {})
        if not plan["steps"]:
            return None
        step_ids = {step["id"] for step in plan["steps"]}
        progress = self._latest_progress(events)

        # Explicit Agent progress always wins over inferred transitions for this version.
        if any(
            item.get("status") == "in_progress"
            and item.get("source") == "agent"
            and int(item.get("plan_version") or 1) == version
            for item in progress.values()
        ):
            return None

        runtime_progress = [
            item for item in progress.values()
            if item.get("source") == "runtime"
            and item.get("phase") in self.PHASE_ORDER
            and int(item.get("plan_version") or 1) == version
            and item.get("step_id") in step_ids
        ]
        latest_runtime = max(runtime_progress, key=lambda item: item.get("event_id", 0), default=None)
        current_runtime = max(
            (item for item in runtime_progress if item.get("status") == "in_progress"),
            key=lambda item: item.get("event_id", 0),
            default=None,
        )
        current_phase = latest_runtime.get("phase") if latest_runtime else None
        if current_phase and self.PHASE_ORDER[phase] < self.PHASE_ORDER[current_phase]:
            return None
        if current_phase == phase and current_runtime:
            return None

        if current_runtime and current_phase and self.PHASE_ORDER[phase] > self.PHASE_ORDER[current_phase]:
            step = next((item for item in plan["steps"] if item["id"] == current_runtime["step_id"]), None)
            if step is not None:
                self._append_status(
                    step, "completed",
                    f"自动跟踪：Agent 已进入「{self.PHASE_LABELS[phase]}」阶段，上一阶段工具执行结束。",
                    current_phase, version,
                )
            events = self.db.get_events(self.task_id, limit=5000)
            progress = self._latest_progress(events)

        candidates = [
            step for step in plan["steps"]
            if progress.get(step["id"], {}).get("status", "pending")
            not in {"completed", "skipped", "failed", "in_progress"}
        ]
        phase_candidates = [step for step in candidates if self._step_phase(step) == phase]
        candidate = (phase_candidates or candidates or [None])[0]
        if candidate is None:
            return None

        candidate_index = plan["steps"].index(candidate)
        for prerequisite in plan["steps"][:candidate_index]:
            state = progress.get(prerequisite["id"], {})
            if state.get("status", "pending") == "pending" and self._step_phase(prerequisite) == "scope":
                self._append_status(
                    prerequisite, "completed",
                    "自动跟踪：初始规划已明确研究范围与目标。",
                    "scope", version,
                )
                events = self.db.get_events(self.task_id, limit=5000)
                progress = self._latest_progress(events)

        candidate_state = progress.get(candidate["id"], {})
        if candidate_state.get("status") == "in_progress":
            return None
        self._append_status(
            candidate, "in_progress",
            f"自动跟踪：Agent 已进入「{self.PHASE_LABELS[phase]}」阶段。",
            phase, version,
        )
        return f"Runtime progress: {candidate['id']} -> in_progress ({phase})."

    def complete_active_plan_step(self, note: str = "最终交付已完成") -> int:
        events = self.db.get_events(self.task_id, limit=5000)
        plan_event = next(
            (event for event in reversed(events) if event.get("event_type") == "research_plan_updated"),
            None,
        )
        if plan_event is None:
            return 0
        version = int((plan_event.get("payload") or {}).get("version") or 1)
        plan = self._normalize_plan((plan_event.get("payload") or {}).get("plan") or {})
        step_by_id = {step["id"]: step for step in plan["steps"]}
        progress = self._latest_progress(events)
        active = sorted(
            (
                item for item in progress.values()
                if item.get("status") == "in_progress"
                and item.get("source") == "runtime"
                and int(item.get("plan_version") or 1) == version
                and item.get("step_id") in step_by_id
            ),
            key=lambda item: item.get("event_id", 0),
            reverse=True,
        )
        for item in active:
            self._append_status(
                step_by_id[item["step_id"]], "completed", note,
                item.get("phase") or "validation", version,
            )
        return len(active)

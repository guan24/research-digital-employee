from __future__ import annotations

import hashlib
import json
from typing import Any

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database


class PlanProgressTracker:
    PHASE_ORDER = {"scope": 0, "discovery": 1, "reading": 2, "analysis": 3, "report": 4, "validation": 5}
    PHASE_LABELS = {"scope": "研究范围确认", "discovery": "资料检索", "reading": "来源阅读",
                    "analysis": "证据分析", "report": "报告撰写", "validation": "交付校验"}
    KEYWORDS = {
        "scope": ("研究范围", "研究目标", "研究问题", "scope", "criteria"),
        "discovery": ("搜索", "检索", "搜集", "收集", "查找", "调研", "文献", "资料", "search", "discover", "collect", "repository"),
        "reading": ("阅读", "读取", "研读", "解读", "提取", "源码", "文档", "论文", "材料", "read", "inspect", "review", "extract", "documentation", "paper"),
        "analysis": ("分析", "比较", "对比", "归纳", "综合", "评估", "评价", "总结", "研究发现", "analysis", "compare", "evaluate", "synthesize", "findings"),
        "report": ("报告", "撰写", "写作", "编写", "成稿", "交付", "report", "write", "draft", "deliver"),
        "validation": ("核验", "验证", "校验", "复核", "检查", "引用", "质量", "verify", "validate", "audit", "citation", "quality"),
    }
    STATUSES = {"pending", "in_progress", "awaiting_verification", "retrying", "blocked", "paused", "completed", "skipped", "failed"}
    TRANSITIONS = {
        "pending": {"in_progress", "awaiting_verification", "completed", "blocked", "paused", "skipped", "failed"},
        "in_progress": {"awaiting_verification", "completed", "retrying", "blocked", "paused", "skipped", "failed"},
        "awaiting_verification": {"in_progress", "completed", "retrying", "blocked", "paused", "skipped", "failed"},
        "retrying": {"in_progress", "blocked", "paused", "failed", "skipped"},
        "blocked": {"pending", "in_progress", "retrying", "paused", "failed", "skipped"},
        "paused": {"pending", "in_progress", "blocked", "failed", "skipped"},
        "failed": {"pending", "in_progress", "retrying", "skipped"},
        "completed": set(), "skipped": set(),
    }

    def __init__(self, db: Database, task_id: str, artifacts: ArtifactStore):
        self.db, self.task_id, self.artifacts = db, task_id, artifacts

    @classmethod
    def _normalize_plan(cls, plan: dict[str, Any]) -> dict[str, Any]:
        steps = []
        for i, raw in enumerate(plan.get("steps") or [], 1):
            if not isinstance(raw, dict):
                continue
            step = {k: str(raw.get(k) or "").strip() for k in
                    ("id", "title", "objective", "expected_output", "expected_output_path")}
            step["id"] = step["id"] or f"step-{i}"
            if str(raw.get("phase") or "").strip().lower() in cls.PHASE_ORDER:
                step["phase"] = str(raw["phase"]).strip().lower()
            if isinstance(raw.get("dependencies"), list):
                step["dependencies"] = list(dict.fromkeys(str(x).strip() for x in raw["dependencies"] if str(x).strip()))
            steps.append(step)
        return {"title": str(plan.get("title") or "研究执行计划").strip(),
                "objective": str(plan.get("objective") or "").strip(), "steps": steps}

    @staticmethod
    def _latest_plan_event(events):
        return next((e for e in reversed(events) if e.get("event_type") == "research_plan_updated"), None)

    @staticmethod
    def _latest_progress(events):
        result = {}
        for event in events:
            if event.get("event_type") != "research_plan_step_status":
                continue
            p = event.get("payload") or {}
            sid = str(p.get("step_id") or "")
            if sid:
                result[sid] = {
                    "step_id": sid, "status": p.get("status", "pending"), "note": p.get("note", ""),
                    "source": p.get("source", "agent"), "phase": p.get("phase"),
                    "plan_version": p.get("plan_version"), "event_id": event.get("event_id", 0),
                    "updated_at": event.get("timestamp"), "verified_output": p.get("verified_output"),
                    "tool_call_id": p.get("tool_call_id"), "tool_name": p.get("tool_name"),
                }
        return result

    @classmethod
    def _step_phase(cls, step):
        phase = str(step.get("phase") or "").strip().lower()
        if phase in cls.PHASE_ORDER:
            return phase
        text = " ".join(str(step.get(k) or "") for k in ("title", "objective", "expected_output")).lower()
        for phase in ("validation", "report", "reading", "analysis", "discovery", "scope"):
            if any(word in text for word in cls.KEYWORDS[phase]):
                return phase
        return None

    @staticmethod
    def _args(value):
        try:
            parsed = json.loads(str(value or ""))
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    @classmethod
    def _tool_phase(cls, name, input_value):
        name = str(name or "").lower().rsplit(".", 1)[-1]
        if name in {"update_research_plan", "update_plan_step_status", "set_active_plan_step",
                    "read_research_memory", "save_research_memory", "get_research_status",
                    "get_evidence_registry", "get_budget_status", "request_budget_extension",
                    "list_artifacts", "get_human_decisions", "define_research_requirements"}:
            return None
        if name in {"web_search", "arxiv_search", "github_repo", "github_tree", "github_releases"}:
            return "discovery"
        if name in {"fetch_url", "fetch_pdf", "github_read", "arxiv_paper"}:
            return "reading"
        path = str(cls._args(input_value).get("path") or "").lower()
        if name == "record_finding":
            return "analysis"
        if name == "save_artifact":
            if path in {"notes/research_plan.json", "notes/research_memory.md"} or "memory_versions/" in path:
                return None
            return "report" if "report" in path or path.startswith("reports/") else "analysis"
        if name == "read_artifact":
            if "report" in path or path.startswith("reports/"):
                return "validation"
            return "reading" if path.startswith(("sources/", "evidence/sources/")) else "analysis"
        return None

    def _verified_output(self, step, output_path=None):
        path = str(output_path or step.get("expected_output_path") or "").strip()
        if not path:
            expected = str(step.get("expected_output") or "").strip()
            if expected.lower().endswith((".md", ".txt", ".json", ".csv", ".yaml", ".yml")):
                path = expected
        if not path or not self.artifacts.exists(path):
            return None
        try:
            content = self.artifacts.read_text(path)
        except (OSError, ValueError):
            return None
        if not content.strip():
            return None
        data = content.encode("utf-8")
        return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def _active_context(self, events, plan, version):
        steps = {s["id"]: s for s in plan.get("steps", [])}
        focus = [e for e in events if e.get("event_type") == "research_plan_step_focus_changed"
                 and int((e.get("payload") or {}).get("plan_version") or 1) == version]
        if focus:
            p = focus[-1].get("payload") or {}
            if not p.get("active"):
                return None
            sid = str(p.get("step_id") or "")
            state = self._latest_progress(events).get(sid, {})
            if sid in steps and state.get("status") in {"in_progress", "retrying"}:
                return {"step_id": sid, "plan_version": version, "phase": self._step_phase(steps[sid]),
                        "source": p.get("source", "agent")}
            return None
        active = [p for p in self._latest_progress(events).values()
                  if p.get("source") == "agent" and p.get("status") in {"in_progress", "retrying"}
                  and int(p.get("plan_version") or 1) == version and p.get("step_id") in steps]
        if not active:
            return None
        item = max(active, key=lambda x: x.get("event_id", 0))
        return {"step_id": item["step_id"], "plan_version": version,
                "phase": self._step_phase(steps[item["step_id"]]), "source": "agent"}

    def _focus_event(self, step_id, version, active, source, note, phase=None):
        self.db.add_event(self.task_id, "research_plan_step_focus_changed", {
            "step_id": step_id, "plan_version": version, "active": bool(active),
            "source": source, "phase": phase, "note": str(note or "")[:1200],
        })

    def transition_step(self, step_id, status, note="", *, source="runtime", phase=None,
                        output_path=None, tool_call_id=None, tool_name=None,
                        expected_version=None, force=False):
        sid, status, source = str(step_id or "").strip(), str(status or "").strip().lower(), str(source or "runtime").lower()
        if not sid:
            raise ValueError("step_id cannot be empty")
        if status not in self.STATUSES:
            raise ValueError("unsupported plan step status: " + status)
        if source not in {"agent", "runtime", "system"}:
            raise ValueError("source must be agent, runtime or system")
        if source == "agent" and status in {"retrying", "paused", "awaiting_verification"}:
            raise ValueError("retrying, paused and awaiting_verification are runtime-managed statuses")
        events = self.db.get_events(self.task_id, limit=5000)
        plan_event = self._latest_plan_event(events)
        if not plan_event:
            raise ValueError("create the overall research plan before updating step progress")
        pp = plan_event.get("payload") or {}
        version = int(pp.get("version") or 1)
        if expected_version is not None and int(expected_version) != version:
            raise ValueError("plan version changed; refresh the current plan before updating step progress")
        plan = self._normalize_plan(pp.get("plan") or {})
        step = next((s for s in plan["steps"] if s["id"] == sid), None)
        if step is None:
            raise ValueError(f"step_id '{sid}' is not present in the current plan")
        previous = self._latest_progress(events).get(sid, {})
        previous_status = str(previous.get("status") or "pending")
        note = str(note or "").strip()
        verified = None
        if status == "completed":
            verified = self._verified_output(step, output_path)
            if verified is None:
                if source == "runtime":
                    status = "awaiting_verification"
                    note = (note + " 预期产物尚未通过存在性、非空和读回校验。").strip()
                else:
                    raise ValueError("completing a plan step requires an existing expected_output_path/output_path artifact")
        if not force and status not in self.TRANSITIONS.get(previous_status, set()):
            if status == previous_status and previous.get("note", "") == note:
                active = self._active_context(events, plan, version)
                if source == "agent" and status == "in_progress" and (not active or active.get("step_id") != sid):
                    self._focus_event(sid, version, True, source, note, phase or self._step_phase(step))
                    self._persist_snapshot()
                return {"step_id": sid, "status": status, "previous_status": previous_status,
                        "plan_version": version, "verified_output": previous.get("verified_output"), "changed": False}
            if status != previous_status:
                raise ValueError(f"invalid plan step transition: {previous_status} -> {status} for step '{sid}'")
        self.db.add_event(self.task_id, "research_plan_step_status", {
            "step_id": sid, "step_title": step["title"], "status": status,
            "previous_status": previous_status, "note": note[:1500],
            "plan_version": version, "source": source, "phase": phase or self._step_phase(step),
            "verified_output": verified, "tool_call_id": tool_call_id, "tool_name": tool_name,
        })
        active = self._active_context(events, plan, version)
        if source == "agent" and status == "in_progress":
            self._focus_event(sid, version, True, source, note, phase or self._step_phase(step))
        elif active and active.get("step_id") == sid and status not in {"in_progress", "retrying"}:
            self._focus_event(None, version, False, source, note or f"步骤状态变更为 {status}", phase or self._step_phase(step))
        self._persist_snapshot()
        return {"step_id": sid, "status": status, "previous_status": previous_status,
                "plan_version": version, "verified_output": verified, "changed": True}

    def _append_status(self, step, status, note, phase, version, output_path=None):
        self.transition_step(step["id"], status, note, source="runtime", phase=phase,
                             output_path=output_path, expected_version=version)

    def activate_step(self, step_id, note=""):
        return self.transition_step(step_id, "in_progress", note or "Agent 开始执行该研究步骤。", source="agent")

    def sync_plan_progress_for_phase(self, phase, note=""):
        if phase not in self.PHASE_ORDER:
            return None
        events = self.db.get_events(self.task_id, limit=5000)
        pe = self._latest_plan_event(events)
        if not pe:
            return None
        pp = pe.get("payload") or {}
        version = int(pp.get("version") or 1)
        plan = self._normalize_plan(pp.get("plan") or {})
        progress = self._latest_progress(events)
        explicit = self._active_context(events, plan, version)
        if explicit and (explicit.get("phase") is None or explicit.get("phase") == phase):
            if progress.get(explicit["step_id"], {}).get("status") == "retrying":
                self.transition_step(explicit["step_id"], "in_progress", note or "重试已开始",
                                     source="runtime", phase=phase, expected_version=version)
            return explicit
        valid_ids = {s["id"] for s in plan["steps"]}
        runtime = [p for p in progress.values() if p.get("source") == "runtime"
                   and p.get("phase") in self.PHASE_ORDER and int(p.get("plan_version") or 1) == version
                   and p.get("step_id") in valid_ids]
        current = max((p for p in runtime if p.get("status") == "in_progress"),
                      key=lambda p: p.get("event_id", 0), default=None)
        current_phase = current.get("phase") if current else None
        if current_phase and self.PHASE_ORDER[phase] < self.PHASE_ORDER[current_phase]:
            return None
        if current_phase == phase and current:
            return {"step_id": current["step_id"], "plan_version": version, "phase": phase, "source": "runtime"}
        if current and current_phase and self.PHASE_ORDER[phase] > self.PHASE_ORDER[current_phase]:
            self.transition_step(current["step_id"], "awaiting_verification",
                f"Agent 已进入「{self.PHASE_LABELS[phase]}」阶段，上一阶段产物等待核验。",
                source="runtime", phase=current_phase, expected_version=version)
            events = self.db.get_events(self.task_id, limit=5000)
            progress = self._latest_progress(events)
        phase_steps = [s for s in plan["steps"] if self._step_phase(s) == phase]
        for step in phase_steps:
            if progress.get(step["id"], {}).get("status") == "in_progress":
                state = progress[step["id"]]
                return {"step_id": step["id"], "plan_version": version, "phase": phase,
                        "source": state.get("source") or "runtime"}
        candidate = next((s for s in phase_steps
                          if progress.get(s["id"], {}).get("status", "pending") not in {"completed", "skipped"}), None)
        if candidate is None:
            candidate = next((s for s in plan["steps"] if self._step_phase(s) is None
                              and progress.get(s["id"], {}).get("status", "pending")
                              not in {"completed", "skipped", "in_progress"}), None)
        if candidate is None:
            return None
        index = plan["steps"].index(candidate)
        for prior in plan["steps"][:index]:
            prior_phase = self._step_phase(prior)
            should_close = prior_phase == "scope" or (phase == "report" and prior_phase == "analysis") or (phase == "validation" and prior_phase == "report")
            if progress.get(prior["id"], {}).get("status", "pending") == "pending" and should_close:
                self.transition_step(prior["id"], "completed",
                    "自动跟踪：进入后续阶段，前置步骤等待产物核验。",
                    source="runtime", phase=prior_phase or phase, expected_version=version)
                events = self.db.get_events(self.task_id, limit=5000)
                progress = self._latest_progress(events)
        self.transition_step(candidate["id"], "in_progress",
            note or f"自动跟踪：Agent 已进入「{self.PHASE_LABELS[phase]}」阶段。",
            source="runtime", phase=phase, expected_version=version)
        return {"step_id": candidate["id"], "plan_version": version, "phase": phase, "source": "runtime"}

    def sync_plan_progress_for_tool(self, tool_name, input_value, metadata=None):
        name = str(tool_name or "").lower().rsplit(".", 1)[-1]
        args = self._args(input_value)
        metadata = metadata if isinstance(metadata, dict) else {}
        sid = str(metadata.get("plan_step_id") or args.get("step_id") or "").strip()
        events = self.db.get_events(self.task_id, limit=5000)
        pe = self._latest_plan_event(events)
        if not pe:
            return None
        pp = pe.get("payload") or {}
        version = int(pp.get("version") or 1)
        plan = self._normalize_plan(pp.get("plan") or {})
        steps = {s["id"]: s for s in plan["steps"]}
        if sid in steps:
            return {"step_id": sid, "plan_version": version, "phase": self._step_phase(steps[sid]),
                    "source": "agent" if name in {"set_active_plan_step", "update_plan_step_status"} else "metadata"}
        if name in {"set_active_plan_step", "update_plan_step_status"}:
            return None
        phase = self._tool_phase(tool_name, input_value)
        if phase is None:
            return None
        active = self._active_context(events, plan, version)
        if active and (active.get("phase") is None or active.get("phase") == phase):
            if self._latest_progress(events).get(active["step_id"], {}).get("status") == "retrying":
                self.transition_step(active["step_id"], "in_progress", "工具重试已开始",
                                     source="runtime", phase=phase, expected_version=version)
            return active
        return self.sync_plan_progress_for_phase(phase, f"检测到工具 {tool_name} 开始执行") or active

    def record_tool_failure(self, context, *, tool_call_id, tool_name, error, retryable=True):
        if not context or not context.get("step_id"):
            return
        try:
            self.transition_step(context["step_id"], "retrying" if retryable else "blocked",
                f"工具 {tool_name} 调用失败：{str(error)[:900]}", source="runtime",
                phase=context.get("phase"), tool_call_id=tool_call_id, tool_name=tool_name,
                expected_version=int(context.get("plan_version") or 1))
        except ValueError as exc:
            self.db.add_event(self.task_id, "research_plan_progress_sync_failed", {
                "tool": tool_name, "step_id": context.get("step_id"),
                "error_type": type(exc).__name__, "error": str(exc)[:1000]})

    def complete_active_plan_step(self, note="最终交付已完成", output_path=None):
        events = self.db.get_events(self.task_id, limit=5000)
        pe = self._latest_plan_event(events)
        if not pe:
            return 0
        version = int((pe.get("payload") or {}).get("version") or 1)
        plan = self._normalize_plan((pe.get("payload") or {}).get("plan") or {})
        ids = {s["id"]: s for s in plan["steps"]}
        eligible = []
        for item in self._latest_progress(events).values():
            if int(item.get("plan_version") or 1) != version or item.get("step_id") not in ids:
                continue
            if item.get("status") == "in_progress":
                eligible.append(item)
                continue
            if item.get("status") != "awaiting_verification" or not output_path:
                continue
            step = ids[item["step_id"]]
            expected = str(step.get("expected_output_path") or step.get("expected_output") or "").strip()
            if expected and expected.replace("\\\\", "/") == str(output_path).replace("\\\\", "/"):
                eligible.append(item)
        for item in sorted(eligible, key=lambda x: x.get("event_id", 0), reverse=True):
            self.transition_step(
                item["step_id"], "completed", note, source="runtime",
                phase=item.get("phase") or self._step_phase(ids[item["step_id"]]) or "validation",
                output_path=output_path, expected_version=version,
            )
        return len(eligible)

    def pause_active_steps(self, note="任务已暂停，等待恢复执行"):
        events = self.db.get_events(self.task_id, limit=5000)
        pe = self._latest_plan_event(events)
        if not pe:
            return 0
        version = int((pe.get("payload") or {}).get("version") or 1)
        active = [p for p in self._latest_progress(events).values()
                  if p.get("status") in {"in_progress", "retrying"} and int(p.get("plan_version") or 1) == version]
        for p in active:
            self.transition_step(p["step_id"], "paused", note, source="system",
                                 phase=p.get("phase"), expected_version=version)
        return len(active)

    def close_open_steps_failed(self, reason):
        events = self.db.get_events(self.task_id, limit=5000)
        pe = self._latest_plan_event(events)
        if not pe:
            return 0
        version = int((pe.get("payload") or {}).get("version") or 1)
        active = [p for p in self._latest_progress(events).values()
                  if p.get("status") in {"in_progress", "retrying", "blocked", "paused", "awaiting_verification"}]
        for p in active:
            self.transition_step(p["step_id"], "failed", reason or "任务执行失败，步骤未能完成",
                source="system", phase=p.get("phase"), expected_version=version, force=True)
        return len(active)

    def _persist_snapshot(self):
        events = self.db.get_events(self.task_id, limit=5000)
        pe = self._latest_plan_event(events)
        if not pe:
            return
        payload = pe.get("payload") or {}
        version = int(payload.get("version") or 1)
        plan = self._normalize_plan(payload.get("plan") or {})
        progress = self._latest_progress(events)
        live = {"schema_version": 2, "version": version, **plan,
                "active_step": self._active_context(events, plan, version),
                "steps": [{**s,
                    "status": progress.get(s["id"], {}).get("status", "pending"),
                    "status_note": progress.get(s["id"], {}).get("note", ""),
                    "status_updated_at": progress.get(s["id"], {}).get("updated_at"),
                    "status_source": progress.get(s["id"], {}).get("source"),
                    "tool_call_id": progress.get(s["id"], {}).get("tool_call_id"),
                    "tool_name": progress.get(s["id"], {}).get("tool_name"),
                    "verified_output": progress.get(s["id"], {}).get("verified_output")}
                    for s in plan["steps"]]}
        self.artifacts.write_text("notes/research_plan.json", json.dumps(live, ensure_ascii=False, indent=2))
        try:
            progress = self._latest_progress(events)
            history = []
            previous_signature = None
            for event in events:
                if event.get("event_type") != "research_plan_updated":
                    continue
                p = event.get("payload") or {}
                archived_plan = self._normalize_plan(p.get("plan") or {})
                signature = json.dumps(archived_plan, ensure_ascii=False, sort_keys=True)
                if signature == previous_signature:
                    continue
                previous_signature = signature
                history.append({
                    "version": len(history) + 1,
                    "timestamp": event.get("timestamp"),
                    "change_type": p.get("change_type") or ("initial" if not history else "replan"),
                    "trigger": p.get("trigger") or p.get("change_reason") or "",
                    "new_information": p.get("new_information") or "",
                    "problem_analysis": p.get("problem_analysis") or "",
                    "decision": p.get("decision") or "",
                    "changes": p.get("changes") or p.get("change_reason") or "",
                    "plan_diff": p.get("plan_diff") or {},
                    "original_plan_text": p.get("original_plan_text") or "",
                    "plan": archived_plan,
                })
            step_progress = {
                step["id"]: {
                    "status": progress.get(step["id"], {}).get("status", "pending"),
                    "updated_at": progress.get(step["id"], {}).get("updated_at"),
                    "note": progress.get(step["id"], {}).get("note", ""),
                    "source": progress.get(step["id"], {}).get("source"),
                    "phase": progress.get(step["id"], {}).get("phase"),
                    "plan_version": progress.get(step["id"], {}).get("plan_version"),
                    "verified_output": progress.get(step["id"], {}).get("verified_output"),
                    "tool_call_id": progress.get(step["id"], {}).get("tool_call_id"),
                    "tool_name": progress.get(step["id"], {}).get("tool_name"),
                }
                for step in plan["steps"]
            }
            final_plan_with_progress = {
                **plan,
                "steps": [
                    {
                        **step,
                        "status": step_progress[step["id"]]["status"],
                        "status_updated_at": step_progress[step["id"]]["updated_at"],
                        "status_note": step_progress[step["id"]]["note"],
                        "status_source": step_progress[step["id"]]["source"],
                        "verified_output": step_progress[step["id"]]["verified_output"],
                    }
                    for step in plan["steps"]
                ],
            }
            counts = {status: sum(1 for item in step_progress.values() if item["status"] == status)
                      for status in sorted(self.STATUSES)}
            latest_focus = next(
                (event.get("payload") or {} for event in reversed(events)
                 if event.get("event_type") == "research_plan_step_focus_changed"),
                {},
            )
            task = self.db.get_task(self.task_id) or {}
            archive = {
                "schema_version": 2,
                "task_id": self.task_id,
                "goal": task.get("goal", ""),
                "status": task.get("status", "in_progress"),
                "outcome": None,
                "finalized_at": None,
                "current_version": len(history),
                "final_plan": plan,
                "final_plan_with_progress": final_plan_with_progress,
                "progress": step_progress,
                "progress_counts": counts,
                "active_step": (
                    {"step_id": latest_focus.get("step_id"),
                     "plan_version": latest_focus.get("plan_version"),
                     "phase": latest_focus.get("phase"),
                     "source": latest_focus.get("source")}
                    if latest_focus.get("active") else None
                ),
                "history": history,
                "note": "Strategic plan revisions and execution progress are stored separately.",
            }
            self.artifacts.write_text(
                "notes/agent_execution_plan.json",
                json.dumps(archive, ensure_ascii=False, indent=2),
            )
        except Exception as exc:
            self.db.add_event(self.task_id, "research_plan_snapshot_refresh_failed", {
                "error_type": type(exc).__name__, "error": str(exc)[:1000]})

    def snapshot(self):
        events = self.db.get_events(self.task_id, limit=5000)
        archive, live = {}, {}
        if self.artifacts.exists("notes/agent_execution_plan.json"):
            try:
                value = json.loads(self.artifacts.read_text("notes/agent_execution_plan.json"))
                archive = value if isinstance(value, dict) else {}
            except (ValueError, OSError):
                pass
        if self.artifacts.exists("notes/research_plan.json"):
            try:
                value = json.loads(self.artifacts.read_text("notes/research_plan.json"))
                live = value if isinstance(value, dict) else {}
            except (ValueError, OSError):
                pass
        pe = self._latest_plan_event(events)
        event_plan = self._normalize_plan((pe.get("payload") or {}).get("plan") or {}) if pe else {}
        plan = self._normalize_plan(live) if live.get("steps") else (
            self._normalize_plan(archive.get("final_plan") or {}) if archive.get("final_plan") else event_plan)
        version_value = live.get("version") or archive.get("current_version") or ((pe.get("payload") or {}).get("version") if pe else 0)
        version = int(version_value or 0)
        progress = dict(archive.get("progress") or {})
        progress.update(self._latest_progress(events))
        for step in plan.get("steps", []):
            state = progress.get(step["id"], {})
            step.update({"status": state.get("status", "pending"), "status_note": state.get("note", ""),
                         "status_updated_at": state.get("updated_at"), "status_source": state.get("source"),
                         "tool_call_id": state.get("tool_call_id"), "tool_name": state.get("tool_name"),
                         "verified_output": state.get("verified_output")})
        history = archive.get("history") or []
        if not history:
            history, signature = [], None
            for event in events:
                if event.get("event_type") != "research_plan_updated":
                    continue
                p = event.get("payload") or {}
                normalized = self._normalize_plan(p.get("plan") or {})
                current_signature = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
                if current_signature == signature:
                    continue
                signature = current_signature
                history.append({"version": int(p.get("version") or len(history) + 1),
                    "timestamp": event.get("timestamp"), "change_type": p.get("change_type") or ("initial" if not history else "replan"),
                    "trigger": p.get("trigger") or p.get("change_reason") or "",
                    "new_information": p.get("new_information") or "", "problem_analysis": p.get("problem_analysis") or "",
                    "decision": p.get("decision") or "", "changes": p.get("changes") or p.get("change_reason") or "",
                    "plan_diff": p.get("plan_diff") or {}, "plan": normalized,
                    "original_plan_text": p.get("original_plan_text") or ""})
        finalized = next((e for e in reversed(events) if e.get("event_type") == "research_plan_finalized"), None)
        relevant = [e for e in events if e.get("event_type") in {
            "research_plan_updated", "research_plan_step_status", "research_plan_step_focus_changed", "research_plan_finalized"}]
        counts = {s: sum(1 for p in progress.values() if p.get("status") == s) for s in sorted(self.STATUSES)}
        return {"task_id": self.task_id, "current_version": version,
            "current_plan": plan if plan.get("steps") else None, "progress": progress,
            "progress_counts": counts, "history": history,
            "active_step": self._active_context(events, plan, version) if plan.get("steps") else None,
            "finalized": bool(finalized or archive.get("status") == "finalized"),
            "finalized_at": (finalized or {}).get("timestamp") or archive.get("finalized_at"),
            "outcome": archive.get("outcome"), "status": archive.get("status"),
            "last_updated_at": relevant[-1].get("timestamp") if relevant else archive.get("finalized_at")}

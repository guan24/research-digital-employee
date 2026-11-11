from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
import random
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    from langchain_core.tools import tool
except (
    ImportError
):  # Keeps non-agent tool service tests runnable without the model stack.

    def tool(name=None):
        def decorate(func):
            func.name = name or func.__name__
            return func

        return decorate


from app.config import Settings
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database


class TaskControlSignal(RuntimeError):
    pass


class TaskPaused(TaskControlSignal):
    pass


class TaskCancelled(TaskControlSignal):
    pass


class TaskDeadlineExceeded(TaskControlSignal):
    pass


class WorkerLeaseLost(TaskControlSignal):
    pass


class ResearchToolService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        task_id: str,
        artifacts: ArtifactStore,
        started_monotonic: float,
    ):
        self.settings = settings
        self.db = db
        self.task_id = task_id
        self.artifacts = artifacts
        self.started_monotonic = started_monotonic

    def check_control(self) -> None:
        if time.monotonic() - self.started_monotonic > self._task_runtime_limit():
            raise TaskDeadlineExceeded("task exceeded max_runtime_seconds")
        flags = self.db.get_control_flags(self.task_id)
        if not flags:
            raise TaskCancelled("task record no longer exists")
        if flags["cancel_requested"] or flags["status"] == "cancelled":
            raise TaskCancelled("task cancellation requested")
        if flags["pause_requested"] or flags["status"] == "paused":
            raise TaskPaused("task pause requested")

    def _task_runtime_limit(self) -> int:
        task = self.db.get_task(self.task_id)
        return (
            int(task["max_runtime_seconds"])
            if task
            else self.settings.default_max_runtime_seconds
        )

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
        return slug[:40] or "research"

    def web_search(self, query: str, max_results: int = 5) -> str:
        self.check_control()
        if not self.settings.tavily_api_key:
            raise RuntimeError(
                "TAVILY_API_KEY is required for web search; add it to .env"
            )
        if not query.strip():
            raise ValueError("query cannot be empty")
        max_results = max(1, min(int(max_results), 10))
        payload = {
            "api_key": self.settings.tavily_api_key,
            "query": query.strip(),
            "search_depth": "advanced",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with httpx.Client(
                    timeout=self.settings.search_timeout_seconds
                ) as client:
                    response = client.post(
                        "https://api.tavily.com/search", json=payload
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                data = response.json()
                results = []
                for item in data.get("results", [])[:max_results]:
                    url = item.get("url")
                    if not url:
                        continue
                    results.append(
                        {
                            "title": item.get("title", "Untitled"),
                            "url": url,
                            "snippet": item.get("content", ""),
                            "score": item.get("score"),
                            "published_date": item.get("published_date"),
                        }
                    )
                digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
                artifact_path = self.artifacts.write_text(
                    f"sources/search_{digest}.json",
                    json.dumps(
                        {"query": query, "results": results},
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                self.db.add_event(
                    self.task_id,
                    "research_search_completed",
                    {
                        "query": query,
                        "result_count": len(results),
                        "artifact": artifact_path,
                    },
                )
                self.check_control()
                return json.dumps(
                    {"query": query, "results": results, "saved_to": artifact_path},
                    ensure_ascii=False,
                )
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                httpx.HTTPStatusError,
            ) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status < 500 and status != 429:
                    break
                if attempt < 2:
                    time.sleep(1.5 * (2**attempt))
        raise RuntimeError(
            f"web search failed after retries: {last_error}"
        ) from last_error

    @staticmethod
    def _assert_public_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("only http and https URLs are allowed")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid URL")
        host = parsed.hostname.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise ValueError("local hostnames are not allowed")
        try:
            addresses = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError(f"cannot resolve host: {host}") from exc
        if not addresses:
            raise ValueError(f"cannot resolve host: {host}")
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                raise ValueError(f"non-public IP targets are not allowed: {ip}")

    def fetch_url(self, url: str) -> str:
        """Fetch a public webpage with isolated retries.

        Network/content failures are returned as structured JSON instead of
        raising into LangGraph's ToolNode. Task-control signals still propagate.
        HTTPX's default trust_env=True behavior is intentionally preserved so
        Windows system proxy settings remain available.
        """
        self.check_control()
        requested_url = url.strip()
        if not requested_url:
            return json.dumps(
                {
                    "success": False,
                    "url": url,
                    "error_type": "invalid_url",
                    "error": "URL cannot be empty",
                    "retryable": False,
                    "attempts": 0,
                },
                ensure_ascii=False,
            )

        headers = {"User-Agent": "ResearchDigitalEmployee/0.1 (+local research tool)"}
        max_attempts = 3
        last_error: Exception | None = None
        last_error_type = "network_error"
        failed_url = requested_url

        def failure_result(
            error_type: str,
            message: str,
            retryable: bool,
            attempts: int,
            failed_at: str,
        ) -> str:
            payload = {
                "success": False,
                "url": requested_url,
                "failed_url": failed_at,
                "error_type": error_type,
                "error": message[:1000],
                "retryable": retryable,
                "attempts": attempts,
            }
            self.db.add_event(
                self.task_id,
                "research_page_fetch_failed",
                {
                    "url": requested_url,
                    "failed_url": failed_at,
                    "error_type": error_type,
                    "error": message[:500],
                    "retryable": retryable,
                    "attempts": attempts,
                },
            )
            return json.dumps(payload, ensure_ascii=False)

        for attempt in range(1, max_attempts + 1):
            self.check_control()
            current_url = requested_url
            try:
                response_content = b""
                final_url = requested_url
                content_type = ""

                # Do not set trust_env=False: on Windows HTTPX may obtain the
                # proxy from system settings even when proxy environment
                # variables are absent.
                with httpx.Client(
                    timeout=self.settings.fetch_timeout_seconds,
                    follow_redirects=False,
                    headers=headers,
                ) as client:
                    for redirect_count in range(6):
                        self.check_control()
                        failed_url = current_url
                        self._assert_public_url(current_url)

                        with client.stream("GET", current_url) as response:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location:
                                    raise RuntimeError(
                                        "redirect response did not include Location"
                                    )
                                current_url = urljoin(current_url, location)
                                continue

                            response.raise_for_status()

                            content_length = response.headers.get("content-length")
                            if content_length:
                                try:
                                    declared_length = int(content_length)
                                except ValueError:
                                    declared_length = None
                                if (
                                    declared_length is not None
                                    and declared_length > self.settings.max_fetch_bytes
                                ):
                                    raise RuntimeError(
                                        "webpage exceeds MAX_FETCH_BYTES"
                                    )

                            chunks: list[bytes] = []
                            total = 0
                            for chunk in response.iter_bytes():
                                total += len(chunk)
                                if total > self.settings.max_fetch_bytes:
                                    raise RuntimeError(
                                        "webpage exceeds MAX_FETCH_BYTES"
                                    )
                                chunks.append(chunk)

                            response_content = b"".join(chunks)
                            final_url = str(response.url)
                            content_type = response.headers.get("content-type", "")
                            break
                    else:
                        raise RuntimeError("too many redirects")

                self.check_control()
                html = response_content.decode("utf-8", errors="replace")

                if (
                    "html" in content_type.lower()
                    or "xml" in content_type.lower()
                    or "<html" in html[:1000].lower()
                ):
                    soup = BeautifulSoup(html, "html.parser")
                    title = soup.title.get_text(" ", strip=True) if soup.title else ""
                    for node in soup(
                        [
                            "script",
                            "style",
                            "noscript",
                            "svg",
                            "nav",
                            "footer",
                            "header",
                        ]
                    ):
                        node.decompose()
                    main = (
                        soup.find("main") or soup.find("article") or soup.body or soup
                    )
                    text = "\\n".join(
                        line.strip()
                        for line in main.get_text("\\n").splitlines()
                        if line.strip()
                    )
                else:
                    title = ""
                    text = html.strip()

                text = text[:60_000]
                if len(text) < 80:
                    return failure_result(
                        "insufficient_content",
                        "webpage yielded too little readable text",
                        False,
                        attempt,
                        final_url,
                    )

                digest = hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:12]
                content = (
                    f"# {title or final_url}\\n\\nSource: {final_url}\\n\\n{text}\\n"
                )
                artifact_path = self.artifacts.write_text(
                    f"sources/page_{digest}.md",
                    content,
                )
                self.db.add_event(
                    self.task_id,
                    "research_page_fetched",
                    {
                        "url": final_url,
                        "characters": len(text),
                        "artifact": artifact_path,
                        "attempts": attempt,
                    },
                )
                return json.dumps(
                    {
                        "success": True,
                        "url": final_url,
                        "title": title,
                        "text": text[:14_000],
                        "saved_to": artifact_path,
                        "attempts": attempt,
                    },
                    ensure_ascii=False,
                )

            except TaskControlSignal:
                raise
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                last_error_type = "http_status_error"
                retryable = status in {408, 425, 429} or status >= 500
                if not retryable:
                    return failure_result(
                        last_error_type,
                        f"HTTP {status}: {exc}",
                        False,
                        attempt,
                        current_url,
                    )
            except httpx.TransportError as exc:
                last_error = exc
                message = str(exc)
                last_error_type = (
                    "ssl_error"
                    if "ssl" in message.lower() or "tls" in message.lower()
                    else "network_error"
                )
                retryable = True
            except ValueError as exc:
                return failure_result(
                    "invalid_url",
                    str(exc),
                    False,
                    attempt,
                    current_url,
                )
            except RuntimeError as exc:
                return failure_result(
                    "fetch_error",
                    str(exc),
                    False,
                    attempt,
                    current_url,
                )

            if attempt < max_attempts:
                self.db.add_event(
                    self.task_id,
                    "research_page_fetch_retry",
                    {
                        "url": requested_url,
                        "failed_url": current_url,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "error_type": last_error_type,
                        "error": (
                            str(last_error)[:500] if last_error else "unknown error"
                        ),
                    },
                )
                self.check_control()
                delay = min(8.0, (2 ** (attempt - 1)) + random.uniform(0.0, 0.25))
                time.sleep(delay)

        return failure_result(
            last_error_type,
            str(last_error) if last_error else "unknown webpage fetch error",
            True,
            max_attempts,
            failed_url,
        )

    def save_artifact(self, path: str, content: str) -> str:
        self.check_control()
        saved = self.artifacts.write_text(path, content)
        self.db.add_event(
            self.task_id,
            "artifact_saved",
            {"path": saved, "bytes": len(content.encode("utf-8"))},
        )
        return f"Saved artifact: {saved}"

    def read_artifact(self, path: str) -> str:
        self.check_control()
        content = self.artifacts.read_text(path)
        return content[:20_000]

    def list_artifacts(self) -> str:
        self.check_control()
        return json.dumps(self.artifacts.list_files(), ensure_ascii=False)

    @staticmethod
    def _normalize_plan_structure(plan: dict) -> dict:
        """Strip execution telemetry from a plan; only strategic structure is versioned."""
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
    def _plan_diff(previous: dict, current: dict) -> dict:
        previous_steps = {step["id"]: step for step in previous.get("steps", [])}
        current_steps = {step["id"]: step for step in current.get("steps", [])}
        added = [
            {"id": step["id"], "title": step["title"]}
            for step in current.get("steps", [])
            if step["id"] not in previous_steps
        ]
        removed = [
            {"id": step["id"], "title": step["title"]}
            for step in previous.get("steps", [])
            if step["id"] not in current_steps
        ]
        modified = []
        for step in current.get("steps", []):
            step_id = step["id"]
            if step_id not in previous_steps:
                continue
            changed_fields = [
                field
                for field in ("title", "objective", "expected_output")
                if step.get(field) != previous_steps[step_id].get(field)
            ]
            if changed_fields:
                modified.append({
                    "id": step_id,
                    "title": step["title"],
                    "changed_fields": changed_fields,
                })
        previous_order = [
            step["id"] for step in previous.get("steps", [])
            if step["id"] in current_steps
        ]
        current_order = [
            step["id"] for step in current.get("steps", [])
            if step["id"] in previous_steps
        ]
        return {
            "title_changed": previous.get("title") != current.get("title"),
            "objective_changed": previous.get("objective") != current.get("objective"),
            "added_steps": added,
            "removed_steps": removed,
            "modified_steps": modified,
            "order_changed": previous_order != current_order,
        }

    @staticmethod
    def persist_plan_archive(
        db: Database,
        task_id: str,
        artifacts: ArtifactStore,
        status: str = "in_progress",
        outcome: str | None = None,
        finalized_at: str | None = None,
    ) -> str | None:
        """Write a durable, deduplicated history of strategic plan versions."""
        events = db.get_events(task_id, limit=5000)
        plan_events = [
            event for event in events
            if event["event_type"] == "research_plan_updated"
        ]
        history = []
        previous_signature = None
        for event in plan_events:
            payload = event.get("payload", {})
            plan = ResearchToolService._normalize_plan_structure(
                payload.get("plan") or {}
            )
            signature = json.dumps(plan, ensure_ascii=False, sort_keys=True)
            # Older releases emitted a new plan event for every step-status change.
            # Collapse those records so the archive represents only strategic revisions.
            if signature == previous_signature:
                continue
            previous_signature = signature
            history.append({
                "version": len(history) + 1,
                "timestamp": event.get("timestamp"),
                "change_type": payload.get("change_type") or ("initial" if not history else "replan"),
                "trigger": payload.get("trigger") or payload.get("change_reason") or "",
                "new_information": payload.get("new_information") or "",
                "problem_analysis": payload.get("problem_analysis") or "",
                "decision": payload.get("decision") or "",
                "changes": payload.get("changes") or payload.get("change_reason") or "",
                "plan_diff": payload.get("plan_diff") or {},
                "plan": plan,
            })

        if not history:
            return None

        task = db.get_task(task_id) or {}
        archive = {
            "schema_version": 1,
            "task_id": task_id,
            "goal": task.get("goal", ""),
            "status": status,
            "outcome": outcome,
            "finalized_at": finalized_at,
            "current_version": history[-1]["version"],
            "final_plan": history[-1]["plan"],
            "history": history,
            "note": "Only initial planning and material strategic replans are versioned; step progress belongs to the execution trace.",
        }
        return artifacts.write_text(
            "notes/agent_execution_plan.json",
            json.dumps(archive, ensure_ascii=False, indent=2),
        )

    def update_research_plan(
        self,
        plan_json: str,
        change_reason: str = "",
        trigger: str = "",
        new_information: str = "",
        problem_analysis: str = "",
        decision: str = "",
        changes: str = "",
    ) -> str:
        """Create the initial plan or record a material, evidence-driven replan."""
        self.check_control()
        plan_text = str(plan_json or "")
        try:
            raw_plan = json.loads(plan_text)
        except (TypeError, json.JSONDecodeError) as exc:
            extracted = [
                re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", "", line.strip()).strip()
                for line in plan_text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if not extracted:
                raise ValueError(
                    "plan_json must be valid JSON or contain extractable plan steps"
                ) from exc
            raw_plan = {
                "title": "执行计划（文本提取）",
                "objective": "从 Agent 原始规划文本中提取",
                "steps": [
                    {"id": f"step-{index}", "title": title}
                    for index, title in enumerate(extracted[:20], start=1)
                ],
            }

        if not isinstance(raw_plan, dict) or not isinstance(raw_plan.get("steps"), list):
            raise ValueError("plan_json must be an object containing a steps array")
        if not 1 <= len(raw_plan["steps"]) <= 20:
            raise ValueError("a research plan must contain between 1 and 20 steps")
        if any(not isinstance(step, dict) for step in raw_plan["steps"]):
            raise ValueError("every plan step must be an object")

        plan = self._normalize_plan_structure(raw_plan)
        seen_ids = set()
        for index, step in enumerate(plan["steps"], start=1):
            if not step["id"] or not step["title"] or step["id"] in seen_ids:
                raise ValueError(
                    f"plan step {index} requires a unique id and non-empty title"
                )
            seen_ids.add(step["id"])

        history = self.db.get_events(self.task_id, limit=5000)
        plan_events = [
            event for event in history
            if event["event_type"] == "research_plan_updated"
        ]
        previous_event = plan_events[-1] if plan_events else None
        previous_plan = self._normalize_plan_structure(
            (previous_event or {}).get("payload", {}).get("plan") or {}
        )

        unique_signatures = []
        for event in plan_events:
            existing_plan = self._normalize_plan_structure(
                event.get("payload", {}).get("plan") or {}
            )
            signature = json.dumps(
                existing_plan, ensure_ascii=False, sort_keys=True
            )
            if not unique_signatures or unique_signatures[-1] != signature:
                unique_signatures.append(signature)

        if previous_event:
            previous_signature = json.dumps(
                previous_plan, ensure_ascii=False, sort_keys=True
            )
            current_signature = json.dumps(
                plan, ensure_ascii=False, sort_keys=True
            )
            if current_signature == previous_signature:
                return (
                    "No material plan change detected. "
                    f"Kept strategic plan version {len(unique_signatures)}."
                )

        trigger_text = str(trigger or change_reason or "").strip()
        new_information_text = str(new_information or "").strip()
        problem_analysis_text = str(problem_analysis or "").strip()
        decision_text = str(decision or "").strip()
        changes_text = str(changes or "").strip()

        if previous_event:
            required = {
                "trigger": trigger_text,
                "new_information": new_information_text,
                "problem_analysis": problem_analysis_text,
                "decision": decision_text,
                "changes": changes_text,
            }
            missing = [field for field, value in required.items() if not value]
            if missing:
                raise ValueError(
                    "a material replan must explain: " + ", ".join(missing)
                )
            change_type = "replan"
            plan_diff = self._plan_diff(previous_plan, plan)
            version = len(unique_signatures) + 1
        else:
            change_type = "initial"
            trigger_text = trigger_text or "任务启动"
            new_information_text = new_information_text or "用户提供的研究目标与交付要求"
            problem_analysis_text = problem_analysis_text or "尚无执行期证据，先建立可验证的研究路径"
            decision_text = decision_text or "将研究目标拆解为有序、可执行且可验证的工作步骤"
            changes_text = changes_text or "建立初始研究执行计划"
            plan_diff = {
                "title_changed": False,
                "objective_changed": False,
                "added_steps": [
                    {"id": step["id"], "title": step["title"]}
                    for step in plan["steps"]
                ],
                "removed_steps": [],
                "modified_steps": [],
                "order_changed": False,
            }
            version = 1

        saved_path = self.artifacts.write_text(
            "notes/research_plan.json",
            json.dumps(
                {"schema_version": 1, "version": version, **plan},
                ensure_ascii=False,
                indent=2,
            ),
        )
        self.db.add_event(
            self.task_id,
            "research_plan_updated",
            {
                "version": version,
                "change_type": change_type,
                "source": "agent",
                "trigger": trigger_text[:1200],
                "new_information": new_information_text[:4000],
                "problem_analysis": problem_analysis_text[:4000],
                "decision": decision_text[:3000],
                "changes": changes_text[:3000],
                "change_reason": (change_reason or trigger_text)[:1200],
                "plan_diff": plan_diff,
                "artifact_path": saved_path,
                "plan": plan,
                "original_plan_text": plan_text[:12000],
            },
        )
        archive_path = self.persist_plan_archive(
            self.db, self.task_id, self.artifacts, status="in_progress"
        )
        return (
            f"Saved {'initial' if change_type == 'initial' else 'revised'} "
            f"research plan version {version} to {saved_path}; "
            f"full plan history: {archive_path}."
        )

    def as_langchain_tools(self):
        @tool("update_research_plan")
        def update_research_plan_tool(
            plan_json: str,
            change_reason: str = "",
            trigger: str = "",
            new_information: str = "",
            problem_analysis: str = "",
            decision: str = "",
            changes: str = "",
        ) -> str:
            """Create the initial overall research plan or record a material strategic replan.
            Pass plan_json as JSON with title, objective and ordered steps [{id,title,objective,expected_output}].
            Do NOT call this tool for routine step progress, searches, reads, or step completion.
            For every later replan, explain trigger, new_information, problem_analysis, decision and changes.
            The system records only meaningful plan revisions, not per-step status updates."""
            return self.update_research_plan(
                plan_json,
                change_reason,
                trigger,
                new_information,
                problem_analysis,
                decision,
                changes,
            )

        @tool("web_search")
        def web_search_tool(query: str, max_results: int = 5) -> str:
            """Search the public web using Tavily. Returns titles, URLs, snippets and a saved JSON source file."""
            return self.web_search(query, max_results)

        @tool("fetch_url")
        def fetch_url_tool(url: str) -> str:
            """Read a public webpage and save its main text as Markdown. Returns structured JSON; transient network failures are retried and reported without aborting the whole agent tool batch."""
            return self.fetch_url(url)

        @tool("save_artifact")
        def save_artifact_tool(path: str, content: str) -> str:
            """Persist research notes, plans, evidence tables or report files in the task workspace. Use relative paths ending in .md, .txt, .json, .csv or .yaml."""
            return self.save_artifact(path, content)

        @tool("read_artifact")
        def read_artifact_tool(path: str) -> str:
            """Read a previously saved research artifact from the current task workspace."""
            return self.read_artifact(path)

        @tool("list_artifacts")
        def list_artifacts_tool() -> str:
            """List files saved in the current task workspace."""
            return self.list_artifacts()

        return [
            update_research_plan_tool,
            web_search_tool,
            fetch_url_tool,
            save_artifact_tool,
            read_artifact_tool,
            list_artifacts_tool,
        ]

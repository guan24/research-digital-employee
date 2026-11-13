from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
import random
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    from langchain_core.tools import tool
except (
    ImportError
):  # Keeps non-agent tool service tests runnable without the model stack.

    def tool(name=None, **kwargs):
        def decorate(func):
            func.name = name or func.__name__
            return func

        return decorate


from app.config import Settings
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.control import TaskControlSignal, TaskPaused, TaskCancelled, TaskDeadlineExceeded, TaskStepLimitExceeded, WorkerLeaseLost
from app.tools.common import TaskToolService
from app.tools.research_schemas import ResearchPlan, ResearchPlanStep, ResearchPlanInput, ResearchRequirementsInput, ResearchDimension, ResearchQualityPolicy
from app.runtime.research_safety import correction_failure


class ResearchToolService(TaskToolService):
    def __init__(
        self,
        settings: Settings,
        db: Database,
        task_id: str,
        artifacts: ArtifactStore,
        started_monotonic: float,
        worker_id: str | None = None,
        worker_generation: int | None = None,
    ):
        super().__init__(settings, db, task_id, artifacts, started_monotonic, worker_id, worker_generation)
        self._github_source_adapter = None
        self._arxiv_adapter = None

    def _github_sources(self):
        if self._github_source_adapter is None:
            from app.tools.github_sources import GitHubSourceAdapter
            self._github_source_adapter = GitHubSourceAdapter(
                self.settings, self.db, self.task_id, self.artifacts, self
            )
        return self._github_source_adapter

    def _arxiv_sources(self):
        if self._arxiv_adapter is None:
            from app.tools.arxiv import ArxivAdapter
            self._arxiv_adapter = ArxivAdapter(
                self.settings, self.db, self.task_id, self.artifacts, self
            )
        return self._arxiv_adapter

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
        return slug[:40] or "research"

    def web_search(self, query: str, max_results: int = 5) -> str:
        self.check_control()
        blocked = self.budget_tool_gate("web_search")
        if blocked:
            return blocked
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
        blocked = self.budget_tool_gate("fetch_url")
        if blocked:
            return blocked
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
                    text = "\n".join(
                        line.strip()
                        for line in main.get_text("\n").splitlines()
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

                evidence = EvidenceRegistry(self.artifacts).register_source(url=final_url, title=title or final_url, content=text, source_type="webpage")
                digest = hashlib.sha256((final_url + evidence["content_hash"]).encode("utf-8")).hexdigest()[:12]
                content = (
                    f"# {title or final_url}\n\nSource: {final_url}\n\n{text}\n"
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
                        "text": text[:8_000],
                        "saved_to": artifact_path,
                        "source_id": evidence["source_id"],
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

    def fetch_pdf(
        self, url: str, *, source_type: str = "pdf",
        evidence_url: str | None = None, evidence_title: str | None = None,
        evidence_metadata: dict | None = None,
    ) -> str:
        """Download and extract a public PDF into the task evidence workspace."""
        self.check_control()
        blocked = self.budget_tool_gate("fetch_pdf")
        if blocked:
            return blocked
        from io import BytesIO
        from pypdf import PdfReader

        requested = url.strip()
        self._assert_public_url(requested)
        try:
            with httpx.Client(timeout=self.settings.fetch_timeout_seconds, follow_redirects=False) as client:
                response = client.get(requested)
                response.raise_for_status()
                if len(response.content) > self.settings.max_fetch_bytes:
                    raise ValueError("PDF exceeds MAX_FETCH_BYTES")
                content_type = response.headers.get("content-type", "").lower()
                if "pdf" not in content_type and not response.content.startswith(b"%PDF"):
                    raise ValueError("URL did not return a PDF document")
            reader = PdfReader(BytesIO(response.content))
            pages = []
            for index, page in enumerate(reader.pages):
                self.check_control()
                text = (page.extract_text() or "").strip()
                if text:
                    pages.append(f"## Page {index + 1}\n\n{text}")
            extracted = "\n\n".join(pages)
            if len(extracted) < 80:
                raise ValueError("PDF contains insufficient extractable text; OCR may be required")
            registered_url = evidence_url or requested
            registered_title = evidence_title or "PDF source"
            registered_metadata = dict(evidence_metadata or {})
            registered_metadata.setdefault("pdf_url", requested)
            evidence = EvidenceRegistry(self.artifacts).register_source(
                url=registered_url, title=registered_title, content=extracted,
                source_type=source_type, metadata=registered_metadata,
            )
            digest = hashlib.sha256((registered_url + evidence["content_hash"]).encode("utf-8")).hexdigest()[:12]
            path = self.artifacts.write_text(
                f"sources/pdf_{digest}.md",
                f"# PDF source\n\nSource: {requested}\n\n{extracted[:60000]}\n",
            )
            self.db.add_event(self.task_id, "research_pdf_fetched", {
                "url": registered_url, "pdf_url": requested, "source_type": source_type,
                "pages": len(reader.pages), "characters": len(extracted),
                "artifact": path, "source_id": evidence["source_id"]
            })
            return json.dumps({"success": True, "url": registered_url, "pdf_url": requested,
                               "source_type": source_type, "pages": len(reader.pages),
                               "characters": len(extracted), "saved_to": path,
                               "source_id": evidence["source_id"]}, ensure_ascii=False)
        except TaskControlSignal:
            raise
        except Exception as exc:
            self.db.add_event(self.task_id, "research_pdf_fetch_failed", {
                "url": requested, "error_type": type(exc).__name__, "error": str(exc)[:1000]
            })
            return json.dumps({"success": False, "url": requested, "error": str(exc)[:1000]}, ensure_ascii=False)

    def save_artifact(self, path: str, content: str) -> str:
        return super().save_artifact(path, content)

    def read_artifact(self, path: str, start_char: int = 0, max_chars: int = 8_000) -> str:
        return super().read_artifact(path, start_char, max_chars)

    def list_artifacts(self) -> str:
        return super().list_artifacts()

    @staticmethod
    def _normalize_plan_structure(plan: dict) -> dict:
        """Strip execution telemetry from a plan; only strategic structure is versioned."""
        return {
            "title": str(plan.get("title") or "研究执行计划").strip(),
            "objective": str(plan.get("objective") or "").strip(),
            "steps": [
                {
                    **{
                        "id": str(step.get("id") or f"step-{index}").strip(),
                        "title": str(step.get("title") or "").strip(),
                        "objective": str(step.get("objective") or "").strip(),
                        "expected_output": str(step.get("expected_output") or "").strip(),
                        "expected_output_path": str(step.get("expected_output_path") or "").strip(),
                    },
                    **({"phase": str(step.get("phase") or "").strip().lower()} if str(step.get("phase") or "").strip().lower() in {"scope", "discovery", "reading", "analysis", "report", "validation"} else {}),
                    **({"dependencies": list(dict.fromkeys(str(value).strip() for value in step.get("dependencies", []) if str(value).strip()))} if isinstance(step.get("dependencies"), list) else {}),
                }
                for index, step in enumerate(plan.get("steps") or [], start=1)
                if isinstance(step, dict)
            ],
        }

    @staticmethod
    def _latest_step_progress(events: list[dict]) -> dict[str, dict]:
        """Return the latest independently persisted progress event for each step."""
        progress: dict[str, dict] = {}
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
                "updated_at": event.get("timestamp"),
                "note": payload.get("note") or "",
                "plan_version": payload.get("plan_version"),
                "source": payload.get("source") or "agent",
                "phase": payload.get("phase"),
                "event_id": event.get("event_id", 0),
                "verified_output": payload.get("verified_output"),
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
            }
        return progress

    @staticmethod
    def mark_open_plan_steps_failed(db: Database, task_id: str, reason: str, artifacts: ArtifactStore | None = None) -> int:
        """Close in-progress plan steps as failed when the task exhausts its retries."""
        events = db.get_events(task_id, limit=5000)
        plan_events = [
            event for event in events
            if event.get("event_type") == "research_plan_updated"
        ]
        if not plan_events:
            return 0
        latest_plan_event = plan_events[-1]
        plan = ResearchToolService._normalize_plan_structure(
            latest_plan_event.get("payload", {}).get("plan") or {}
        )
        progress = ResearchToolService._latest_step_progress(events)
        if artifacts is not None:
            from app.runtime.plan_progress import PlanProgressTracker
            return PlanProgressTracker(db, task_id, artifacts).close_open_steps_failed(reason)
        count = 0
        for step in plan["steps"]:
            state = progress.get(step["id"], {})
            if state.get("status") not in {"in_progress", "retrying", "blocked", "paused", "awaiting_verification"}:
                continue
            db.add_event(task_id, "research_plan_step_status", {
                "step_id": step["id"], "step_title": step["title"],
                "status": "failed", "previous_status": state.get("status", "in_progress"),
                "note": (reason or "任务执行失败，步骤未能完成")[:1200],
                "plan_version": latest_plan_event.get("payload", {}).get("version", 1),
                "source": "system",
            })
            count += 1
        return count

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
                for field in ("title", "objective", "expected_output", "expected_output_path", "phase", "dependencies")
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
        existing_archive = {}
        if artifacts.exists("notes/agent_execution_plan.json"):
            try:
                loaded = json.loads(artifacts.read_text("notes/agent_execution_plan.json"))
                existing_archive = loaded if isinstance(loaded, dict) else {}
            except (ValueError, OSError):
                existing_archive = {}
        history = list(existing_archive.get("history") or [])
        previous_signature = (
            json.dumps(history[-1].get("plan") or {}, ensure_ascii=False, sort_keys=True)
            if history else None
        )
        previous_timestamp = history[-1].get("timestamp") if history else None
        for event in plan_events:
            payload = event.get("payload", {})
            event_timestamp = event.get("timestamp")
            previous_event_id = history[-1].get("event_id") if history else None
            if previous_event_id is not None and event.get("event_id") is not None:
                if int(event.get("event_id")) <= int(previous_event_id):
                    continue
            elif previous_timestamp and event_timestamp and str(event_timestamp) < str(previous_timestamp):
                continue
            plan = ResearchToolService._normalize_plan_structure(payload.get("plan") or {})
            signature = json.dumps(plan, ensure_ascii=False, sort_keys=True)
            # Preserve archived versions outside the bounded event window and append only newer revisions.
            if signature == previous_signature:
                previous_timestamp = event_timestamp or previous_timestamp
                continue
            previous_signature = signature
            history.append({
                "version": len(history) + 1,
                "event_id": event.get("event_id"),
                "timestamp": event_timestamp,
                "change_type": payload.get("change_type") or ("initial" if not history else "replan"),
                "trigger": payload.get("trigger") or payload.get("change_reason") or "",
                "new_information": payload.get("new_information") or "",
                "problem_analysis": payload.get("problem_analysis") or "",
                "decision": payload.get("decision") or "",
                "changes": payload.get("changes") or payload.get("change_reason") or "",
                "plan_diff": payload.get("plan_diff") or {},
                "plan": plan,
                "original_plan_text": payload.get("original_plan_text") or "",
            })
            previous_timestamp = event_timestamp or previous_timestamp

        if not history:
            return None

        task = db.get_task(task_id) or {}
        latest_plan = history[-1]["plan"]
        latest_progress = dict(existing_archive.get("progress") or {})
        latest_progress.update(ResearchToolService._latest_step_progress(events))
        progress = {
            step["id"]: {
                "status": latest_progress.get(step["id"], {}).get("status", "pending"),
                "updated_at": latest_progress.get(step["id"], {}).get("updated_at"),
                "note": latest_progress.get(step["id"], {}).get("note", ""),
                "source": latest_progress.get(step["id"], {}).get("source"),
                "phase": latest_progress.get(step["id"], {}).get("phase"),
                "plan_version": latest_progress.get(step["id"], {}).get("plan_version"),
                "verified_output": latest_progress.get(step["id"], {}).get("verified_output"),
                "tool_call_id": latest_progress.get(step["id"], {}).get("tool_call_id"),
                "tool_name": latest_progress.get(step["id"], {}).get("tool_name"),
            }
            for step in latest_plan["steps"]
        }
        final_plan_with_progress = {
            **latest_plan,
            "steps": [
                {
                    **step,
                    "status": progress[step["id"]]["status"],
                    "status_updated_at": progress[step["id"]]["updated_at"],
                    "status_note": progress[step["id"]]["note"],
                }
                for step in latest_plan["steps"]
            ],
        }
        progress_counts = {
            status: sum(1 for item in progress.values() if item["status"] == status)
            for status in ("pending", "in_progress", "awaiting_verification", "retrying", "blocked", "paused", "completed", "skipped", "failed")
        }
        latest_focus = next(
            (event.get("payload") or {} for event in reversed(events)
             if event.get("event_type") == "research_plan_step_focus_changed"),
            {},
        )
        active_step = (
            {"step_id": latest_focus.get("step_id"), "plan_version": latest_focus.get("plan_version"),
             "phase": latest_focus.get("phase"), "source": latest_focus.get("source")}
            if latest_focus.get("active") else None
        )
        archive = {
            "schema_version": 2,
            "task_id": task_id,
            "goal": task.get("goal", ""),
            "status": status,
            "outcome": outcome,
            "finalized_at": finalized_at,
            "current_version": history[-1]["version"],
            "final_plan": latest_plan,
            "final_plan_with_progress": final_plan_with_progress,
            "progress": progress,
            "progress_counts": progress_counts,
            "active_step": active_step,
            "history": history,
            "note": "Strategic plan revisions and execution progress are stored separately. Step progress does not create a strategic plan version.",
        }
        artifacts.write_text(
            "notes/agent_execution_plan.json",
            json.dumps(archive, ensure_ascii=False, indent=2),
        )
        return "notes/agent_execution_plan.json"

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
        plan_text = plan_json
        raw_plan = ResearchPlan.model_validate_json(plan_json).model_dump(exclude_none=True)
        plan = self._normalize_plan_structure(raw_plan)

        history = self.db.get_events(self.task_id, limit=5000)
        plan_events = [
            event for event in history
            if event["event_type"] == "research_plan_updated"
        ]
        previous_event = plan_events[-1] if plan_events else None
        previous_plan = self._normalize_plan_structure(
            (previous_event or {}).get("payload", {}).get("plan") or {}
        )
        progress_before = self._latest_step_progress(history)

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
        reset_step_ids = set()
        if not previous_event:
            reset_step_ids = {step["id"] for step in plan["steps"]}
        else:
            reset_step_ids = {
                item["id"]
                for item in (plan_diff.get("added_steps", []) + plan_diff.get("modified_steps", []))
            }
        from app.runtime.plan_progress import PlanProgressTracker
        progress_tracker = PlanProgressTracker(self.db, self.task_id, self.artifacts)
        for step in plan["steps"]:
            if step["id"] not in reset_step_ids:
                continue
            progress_tracker.transition_step(
                step["id"], "pending",
                "初始计划建立，等待执行" if not previous_event else "计划结构发生调整，该步骤等待重新执行",
                source="system", expected_version=version, force=True,
            )

        archive_path = self.persist_plan_archive(
            self.db, self.task_id, self.artifacts, status="in_progress"
        )
        return (
            f"Saved {'initial' if change_type == 'initial' else 'revised'} "
            f"research plan version {version} to {saved_path}; "
            f"full plan history: {archive_path}."
        )

    def update_plan_step_status(
        self, step_id: str, status: str, note: str = "", output_path: str | None = None,
    ) -> str:
        """Persist one plan-step transition through the shared state machine."""
        self.check_control()
        from app.runtime.plan_progress import PlanProgressTracker
        result = PlanProgressTracker(self.db, self.task_id, self.artifacts).transition_step(
            step_id, status, note, source="agent", output_path=output_path,
        )
        return (
            f"Updated plan step '{result['step_id']}': "
            f"{result['previous_status']} -> {result['status']}. "
            "The transition and verified output metadata were persisted."
        )

    def set_active_plan_step(self, step_id: str, note: str = "") -> str:
        """Bind subsequent tool calls to the plan step currently being worked on."""
        self.check_control()
        from app.runtime.plan_progress import PlanProgressTracker
        result = PlanProgressTracker(self.db, self.task_id, self.artifacts).activate_step(step_id, note)
        return (
            f"Active research step is now '{result['step_id']}' "
            f"(plan v{result['plan_version']}). Subsequent tool calls will be linked to this step."
        )

    def as_langchain_tools(self):
        def feedback(name, error):
            self.check_control()
            return correction_failure(self.db, self.task_id, name, error,
                                      self.worker_id, self.worker_generation)

        @tool("update_research_plan", args_schema=ResearchPlanInput)
        def update_research_plan_tool(
            title: str,
            objective: str,
            steps: list[ResearchPlanStep],
            change_reason: str = "",
            trigger: str = "",
            new_information: str = "",
            problem_analysis: str = "",
            decision: str = "",
            changes: str = "",
        ) -> str:
            """Create or materially revise the overall plan.
            Supply title, objective and ordered step objects with id, title, objective, expected_output and optional expected_output_path (a task-relative artifact path).
            Do not send a JSON document inside a string argument.
            Do not call for routine progress updates; use update_plan_step_status for those.
            Every later strategic replan must explain trigger, new_information, problem_analysis, decision and changes."""
            plan_json = json.dumps(
                {"title": title, "objective": objective, "steps": [s.model_dump(exclude_none=True) for s in steps]},
                ensure_ascii=False,
            )
            try:
                return self.update_research_plan(
                    plan_json, change_reason, trigger, new_information,
                    problem_analysis, decision, changes,
                )
            except ValueError as exc:
                return feedback("update_research_plan", exc)

        @tool("update_plan_step_status")
        def update_plan_step_status_tool(
            step_id: str,
            status: str,
            note: str = "",
            output_path: str | None = None,
        ) -> str:
            """Update one plan-step state. completed requires a non-empty artifact that the runtime can read back and hash. blocked, failed and skipped are Agent decisions; retrying and paused are runtime-managed."""
            return self.update_plan_step_status(step_id, status, note, output_path)

        @tool("set_active_plan_step")
        def set_active_plan_step_tool(step_id: str, note: str = "") -> str:
            """Declare the current plan step before starting work; subsequent search/read/analyze tool calls are correlated with this step in the execution trace."""
            return self.set_active_plan_step(step_id, note)

        @tool("record_finding")
        def record_finding_tool(claim: str, source_ids: list[str], rationale: str = "",
                                confidence: str = "medium", dimension_id: str | None = None) -> str:
            """Record an evidence-linked claim. Always supply the matching research dimension_id when one exists."""
            self.check_control()
            finding = EvidenceRegistry(self.artifacts).record_finding(
                claim=claim, source_ids=source_ids, rationale=rationale,
                confidence=confidence, dimension_id=dimension_id,
            )
            self.db.add_event(self.task_id, "research_finding_recorded", finding)
            return json.dumps(finding, ensure_ascii=False)

        @tool("define_research_requirements", args_schema=ResearchRequirementsInput)
        def define_research_requirements_tool(
            dimensions: list[ResearchDimension], quality_policy: ResearchQualityPolicy | None = None
        ) -> str:
            """Define the research coverage matrix. Optional quality_policy may only raise the role baseline min_total_score and min_dimension_score."""
            self.check_control()
            from app.runtime.research_controller import ResearchController
            try:
                result = ResearchController(self.db, self.task_id, self.artifacts).define_requirements(
                    {"dimensions": [d.model_dump() for d in dimensions],
                     "quality_policy": quality_policy.model_dump(exclude_none=True) if quality_policy else {}}, source="agent"
                )
                return json.dumps(result, ensure_ascii=False, indent=2)
            except ValueError as exc:
                return feedback("define_research_requirements", exc)

        @tool("get_human_decisions")
        def get_human_decisions_tool() -> str:
            """Read audited operator decisions after a task was parked for human intervention. Apply the requested direction, repair action or continuation before doing more research."""
            self.check_control()
            return json.dumps(self.db.list_unconsumed_human_decisions(self.task_id, limit=10), ensure_ascii=False, indent=2)

        @tool("get_research_status")
        def get_research_status_tool() -> str:
            """Inspect current research-dimension coverage, critical evidence gaps, stagnation and the controller action recommendation."""
            self.check_control()
            from app.runtime.lifecycle_coordinator import ResearchLifecycleCoordinator
            coordinator = ResearchLifecycleCoordinator(self.settings, self.db, self.task_id, self.artifacts)
            result = coordinator.assess_progress("agent_query")
            return json.dumps(result, ensure_ascii=False, indent=2)

        @tool("get_evidence_registry")
        def get_evidence_registry_tool() -> str:
            """Read registered evidence sources, immutable snapshots and findings."""
            self.check_control()
            return json.dumps(EvidenceRegistry(self.artifacts).summary(), ensure_ascii=False, indent=2)

        @tool("web_search")
        def web_search_tool(query: str, max_results: int = 5) -> str:
            """Search the public web using Tavily. Returns titles, URLs, snippets and a saved JSON source file."""
            return self.web_search(query, max_results)

        @tool("fetch_url")
        def fetch_url_tool(url: str) -> str:
            """Read a public webpage and save its main text as Markdown. Returns structured JSON; transient network failures are retried and reported without aborting the whole agent tool batch."""
            return self.fetch_url(url)

        @tool("fetch_pdf")
        def fetch_pdf_tool(url: str) -> str:
            """Download a public PDF, extract its text by page, and save it as task evidence."""
            return self.fetch_pdf(url)

        @tool("github_repo")
        def github_repo_tool(repo: str) -> str:
            """Read a GitHub repository overview: description, default branch, language, license, stars and latest push time."""
            return self._github_sources().github_repo(repo)

        @tool("github_tree")
        def github_tree_tool(repo: str, path: str = "", ref: str | None = None) -> str:
            """Read a filtered directory/file index for a GitHub repository path."""
            return self._github_sources().github_tree(repo, path, ref)

        @tool("github_read")
        def github_read_tool(repo: str, paths: list[str], ref: str | None = None) -> str:
            """Batch-read up to five safe text files from a GitHub repository; files are snapshotted and registered as evidence in one tool step."""
            return self._github_sources().github_read(repo, paths, ref)

        @tool("github_releases")
        def github_releases_tool(repo: str, limit: int = 10) -> str:
            """Read a repository's recent GitHub release timeline."""
            return self._github_sources().github_releases(repo, limit)

        @tool("arxiv_search")
        def arxiv_search_tool(
            query: str, max_results: int = 5, sort_by: str = "relevance",
            category: str | None = None,
        ) -> str:
            """Search arXiv using its official Atom API; supports field qualifiers, boolean queries and an optional category."""
            return self._arxiv_sources().search(query, max_results, sort_by, category)

        @tool("arxiv_paper")
        def arxiv_paper_tool(arxiv_id: str) -> str:
            """Read arXiv paper metadata and extract its public PDF through the existing SSRF-safe PDF evidence pipeline."""
            return self._arxiv_sources().paper(arxiv_id)

        @tool("save_artifact")
        def save_artifact_tool(path: str, content: str) -> str:
            """Persist research notes, plans, evidence tables or report files in the task workspace. Use relative paths ending in .md, .txt, .json, .csv or .yaml."""
            return self.save_artifact(path, content)

        @tool("read_artifact")
        def read_artifact_tool(path: str, start_char: int = 0, max_chars: int = 8_000) -> str:
            """Read a bounded chunk from a task artifact; continue large files with start_char."""
            return self.read_artifact(path, start_char, max_chars)

        @tool("list_artifacts")
        def list_artifacts_tool() -> str:
            """List files saved in the current task workspace."""
            return self.list_artifacts()

        update_research_plan_tool.handle_validation_error = lambda exc: feedback("update_research_plan", exc)
        define_research_requirements_tool.handle_validation_error = lambda exc: feedback("define_research_requirements", exc)
        return [
            *self.as_common_tools(),
            record_finding_tool,
            define_research_requirements_tool,
            get_human_decisions_tool,
            get_research_status_tool,
            get_evidence_registry_tool,
            update_research_plan_tool,
            update_plan_step_status_tool,
            set_active_plan_step_tool,
            web_search_tool,
            fetch_url_tool,
            fetch_pdf_tool,
            github_repo_tool,
            github_tree_tool,
            github_read_tool,
            github_releases_tool,
            arxiv_search_tool,
            arxiv_paper_tool,
        ]

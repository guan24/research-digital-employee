from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
try:
    from langchain_core.tools import tool
except ImportError:  # Keeps non-agent tool service tests runnable without the model stack.
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
    def __init__(self, settings: Settings, db: Database, task_id: str, artifacts: ArtifactStore, started_monotonic: float):
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
        return int(task["max_runtime_seconds"]) if task else self.settings.default_max_runtime_seconds

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
        return (slug[:40] or "research")

    def web_search(self, query: str, max_results: int = 5) -> str:
        self.check_control()
        if not self.settings.tavily_api_key:
            raise RuntimeError("TAVILY_API_KEY is required for web search; add it to .env")
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
                with httpx.Client(timeout=self.settings.search_timeout_seconds) as client:
                    response = client.post("https://api.tavily.com/search", json=payload)
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
                    json.dumps({"query": query, "results": results}, ensure_ascii=False, indent=2),
                )
                self.db.add_event(
                    self.task_id,
                    "research_search_completed",
                    {"query": query, "result_count": len(results), "artifact": artifact_path},
                )
                self.check_control()
                return json.dumps(
                    {"query": query, "results": results, "saved_to": artifact_path},
                    ensure_ascii=False,
                )
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status < 500 and status != 429:
                    break
                if attempt < 2:
                    time.sleep(1.5 * (2**attempt))
        raise RuntimeError(f"web search failed after retries: {last_error}") from last_error

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
            addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"cannot resolve host: {host}") from exc
        if not addresses:
            raise ValueError(f"cannot resolve host: {host}")
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                raise ValueError(f"non-public IP targets are not allowed: {ip}")

    def fetch_url(self, url: str) -> str:
        self.check_control()
        current_url = url.strip()
        response_content = b""
        final_url = current_url
        headers = {"User-Agent": "ResearchDigitalEmployee/0.1 (+local research tool)"}
        try:
            with httpx.Client(timeout=self.settings.fetch_timeout_seconds, follow_redirects=False, headers=headers) as client:
                for redirect_count in range(6):
                    self._assert_public_url(current_url)
                    with client.stream("GET", current_url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise RuntimeError("redirect response did not include Location")
                            current_url = urljoin(current_url, location)
                            continue
                        response.raise_for_status()
                        content_length = response.headers.get("content-length")
                        if content_length and int(content_length) > self.settings.max_fetch_bytes:
                            raise RuntimeError("webpage exceeds MAX_FETCH_BYTES")
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > self.settings.max_fetch_bytes:
                                raise RuntimeError("webpage exceeds MAX_FETCH_BYTES")
                            chunks.append(chunk)
                        response_content = b"".join(chunks)
                        final_url = str(response.url)
                        content_type = response.headers.get("content-type", "")
                        break
                else:
                    raise RuntimeError("too many redirects")
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise RuntimeError(f"webpage fetch failed: {exc}") from exc

        self.check_control()
        encoding = "utf-8"
        try:
            html = response_content.decode(encoding, errors="replace")
        except Exception:
            html = response_content.decode("utf-8", errors="replace")
        if "html" in content_type.lower() or "xml" in content_type.lower() or "<html" in html[:1000].lower():
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
                node.decompose()
            main = soup.find("main") or soup.find("article") or soup.body or soup
            text = "\n".join(line.strip() for line in main.get_text("\n").splitlines() if line.strip())
        else:
            title = ""
            text = html.strip()
        text = text[:60_000]
        if len(text) < 80:
            raise RuntimeError("webpage yielded too little readable text")
        digest = hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:12]
        content = f"# {title or final_url}\n\nSource: {final_url}\n\n{text}\n"
        artifact_path = self.artifacts.write_text(f"sources/page_{digest}.md", content)
        self.db.add_event(
            self.task_id,
            "research_page_fetched",
            {"url": final_url, "characters": len(text), "artifact": artifact_path},
        )
        return json.dumps(
            {"url": final_url, "title": title, "text": text[:14_000], "saved_to": artifact_path},
            ensure_ascii=False,
        )

    def save_artifact(self, path: str, content: str) -> str:
        self.check_control()
        saved = self.artifacts.write_text(path, content)
        self.db.add_event(self.task_id, "artifact_saved", {"path": saved, "bytes": len(content.encode("utf-8"))})
        return f"Saved artifact: {saved}"

    def read_artifact(self, path: str) -> str:
        self.check_control()
        content = self.artifacts.read_text(path)
        return content[:20_000]

    def list_artifacts(self) -> str:
        self.check_control()
        return json.dumps(self.artifacts.list_files(), ensure_ascii=False)

    def as_langchain_tools(self):
        @tool("web_search")
        def web_search_tool(query: str, max_results: int = 5) -> str:
            """Search the public web using Tavily. Returns titles, URLs, snippets and a saved JSON source file."""
            return self.web_search(query, max_results)

        @tool("fetch_url")
        def fetch_url_tool(url: str) -> str:
            """Read the main text of a public webpage and save a Markdown copy in the task workspace."""
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

        return [web_search_tool, fetch_url_tool, save_artifact_tool, read_artifact_tool, list_artifacts_tool]

from __future__ import annotations

import copy
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from app.infrastructure.evidence_registry import EvidenceRegistry

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
_ARXIV_ID_RE = re.compile(r"^(?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)$")
_CATEGORY_RE = re.compile(r"^[a-z-]+\.[A-Z]{2}$")
_FIELD_RE = re.compile(r"(^|[\s(])(?:all|ti|au|abs|co|jr|cat|rn|id|submittedDate|lastUpdatedDate):", re.I)


class ArxivRequestError(RuntimeError):
    def __init__(self, message: str, *, error_type: str, code: str,
                 retryable: bool, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def normalize_search_query(query: str, category: str | None = None) -> str:
    value = str(query or "").strip()
    if not value:
        raise ValueError("arXiv query cannot be empty")
    if len(value) > 500:
        raise ValueError("arXiv query exceeds 500 characters")
    normalized = value if _FIELD_RE.search(value) else f"all:{value}"
    if category:
        category = str(category).strip()
        if not _CATEGORY_RE.fullmatch(category):
            raise ValueError("category must look like cs.AI or stat.ML")
        normalized = f"({normalized}) AND cat:{category}"
    return normalized


def _entry_to_paper(entry: ET.Element) -> dict[str, Any]:
    def text(tag: str) -> str:
        node = entry.find(f"atom:{tag}", ATOM_NS)
        return " ".join((node.text or "").split()) if node is not None else ""

    identifier_url = text("id")
    identifier = identifier_url.rstrip("/").rsplit("/", 1)[-1]
    authors = [
        " ".join((node.text or "").split())
        for node in entry.findall("atom:author/atom:name", ATOM_NS)
        if node.text
    ]
    categories = [
        node.attrib.get("term", "")
        for node in entry.findall("atom:category", ATOM_NS)
        if node.attrib.get("term")
    ]
    primary = entry.find("arxiv:primary_category", ATOM_NS)
    pdf_url = ""
    for link in entry.findall("atom:link", ATOM_NS):
        if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
            pdf_url = link.attrib.get("href", "")
            break
    if not pdf_url and identifier:
        pdf_url = f"https://arxiv.org/pdf/{identifier}"
    return {
        "arxiv_id": identifier,
        "url": f"https://arxiv.org/abs/{identifier}" if identifier else identifier_url,
        "title": text("title"),
        "summary": text("summary"),
        "published": text("published"),
        "updated": text("updated"),
        "authors": authors,
        "categories": categories,
        "primary_category": primary.attrib.get("term", "") if primary is not None else "",
        "pdf_url": pdf_url,
    }


class ArxivClient:
    """Standard-library Atom client with a process-wide 3-second throttle and short cache."""

    _rate_lock = threading.Lock()
    _last_request_at = 0.0
    _cache_lock = threading.Lock()
    _cache: dict[tuple, tuple[float, Any]] = {}
    CACHE_TTL_SECONDS = 300
    MIN_REQUEST_INTERVAL_SECONDS = 3.0

    def __init__(self, *, timeout_seconds: float = 20.0, urlopen_factory=None,
                 clock=None, sleep=None):
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._urlopen = urlopen_factory or urllib.request.urlopen
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

    @classmethod
    def reset_process_state_for_tests(cls) -> None:
        with cls._rate_lock:
            cls._last_request_at = 0.0
        with cls._cache_lock:
            cls._cache.clear()

    def _throttle(self) -> None:
        with self._rate_lock:
            now = self._clock()
            wait = self.MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if self._last_request_at and wait > 0:
                self._sleep(wait)
            self.__class__._last_request_at = self._clock()

    def _fetch_atom(self, params: dict[str, Any]) -> bytes:
        query = urllib.parse.urlencode(params, doseq=True)
        url = "https://export.arxiv.org/api/query?" + query
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "ResearchDigitalEmployee/0.1 (research; contact: local-operator)"},
        )
        self._throttle()
        try:
            with self._urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            retry_after = exc.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else None
            if status == 429:
                raise ArxivRequestError(
                    "arXiv API rate limit reached.", error_type="rate_limited",
                    code="ARXIV_RATE_LIMITED", retryable=True,
                    retry_after_seconds=wait or 60,
                ) from exc
            if status >= 500 or status in {408, 425}:
                raise ArxivRequestError(
                    f"arXiv API returned HTTP {status}.", error_type="upstream_5xx",
                    code="ARXIV_UPSTREAM_5XX", retryable=True,
                ) from exc
            raise ArxivRequestError(
                f"arXiv API returned HTTP {status}.", error_type="http_error",
                code="ARXIV_HTTP_ERROR", retryable=False,
            ) from exc
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            raise ArxivRequestError(
                f"arXiv API connection failed: {type(exc).__name__}.",
                error_type="timeout" if isinstance(exc, TimeoutError) else "network_error",
                code="ARXIV_TIMEOUT" if isinstance(exc, TimeoutError) else "ARXIV_NETWORK_ERROR",
                retryable=True,
            ) from exc

    @staticmethod
    def _parse_atom(payload: bytes) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ArxivRequestError(
                "arXiv returned malformed Atom XML.", error_type="invalid_xml",
                code="ARXIV_INVALID_XML", retryable=False,
            ) from exc
        return [_entry_to_paper(entry) for entry in root.findall("atom:entry", ATOM_NS)]

    def search(self, query: str, max_results: int = 5, sort_by: str = "relevance",
               category: str | None = None) -> tuple[list[dict[str, Any]], bool, str]:
        normalized = normalize_search_query(query, category)
        max_results = max(1, min(int(max_results), 20))
        allowed_sorts = {"relevance", "lastUpdatedDate", "submittedDate"}
        if sort_by not in allowed_sorts:
            raise ValueError("sort_by must be relevance, lastUpdatedDate or submittedDate")
        key = ("search", normalized, max_results, sort_by)
        now = self._clock()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] <= self.CACHE_TTL_SECONDS:
                return copy.deepcopy(cached[1]), True, normalized
        payload = self._fetch_atom({
            "search_query": normalized,
            "start": 0,
            "max_results": max_results,
            "sortBy": sort_by,
            "sortOrder": "descending",
        })
        papers = self._parse_atom(payload)
        with self._cache_lock:
            self._cache[key] = (self._clock(), copy.deepcopy(papers))
        return papers, False, normalized

    def paper(self, arxiv_id: str) -> dict[str, Any] | None:
        identifier = str(arxiv_id or "").strip()
        if not _ARXIV_ID_RE.fullmatch(identifier):
            raise ValueError("invalid arXiv identifier")
        payload = self._fetch_atom({"id_list": identifier, "start": 0, "max_results": 1})
        papers = self._parse_atom(payload)
        return papers[0] if papers else None


class ArxivAdapter:
    def __init__(self, settings, db, task_id: str, artifacts, task_tools, client: ArxivClient | None = None):
        self.settings = settings
        self.db = db
        self.task_id = task_id
        self.artifacts = artifacts
        self.task_tools = task_tools
        self.client = client or ArxivClient(timeout_seconds=getattr(settings, "fetch_timeout_seconds", 20))

    def _record_error(self, operation: str, exc: Exception, context: dict | None = None) -> dict:
        if isinstance(exc, ArxivRequestError):
            error_type, code, retryable, retry_after = (
                exc.error_type, exc.code, exc.retryable, exc.retry_after_seconds,
            )
        elif isinstance(exc, ValueError):
            error_type, code, retryable, retry_after = "invalid_request", "ARXIV_INVALID_REQUEST", False, None
        else:
            error_type, code, retryable, retry_after = "unknown", "UNKNOWN_ARXIV_ERROR", True, None
        message = str(exc)[:1200]
        task = self.db.get_task(self.task_id) or {}
        self.db.record_classified_error(
            self.task_id, message, "retryable" if retryable else "terminal",
            code, retryable, int(task.get("attempts", 0)),
        )
        payload = {
            "operation": operation, "error_type": error_type, "error_code": code,
            "retryable": retryable, "retry_after_seconds": retry_after, "error": message,
        }
        payload.update(context or {})
        self.db.add_event(self.task_id, f"arxiv_{operation}_failed", payload)
        return {"success": False, **payload}

    def search(self, query: str, max_results: int = 5, sort_by: str = "relevance",
               category: str | None = None) -> str:
        try:
            self.task_tools.check_control()
            blocked = self.task_tools.budget_tool_gate("arxiv_search")
            if blocked:
                return blocked
            papers, cache_hit, normalized = self.client.search(query, max_results, sort_by, category)
            self.task_tools.check_control()
            registered = []
            for paper in papers:
                self.task_tools.check_control()
                evidence = EvidenceRegistry(self.artifacts).register_source(
                    url=paper["url"], title=paper["title"],
                    content=paper["summary"], source_type="arxiv",
                    verified=True,
                    metadata={
                        "arxiv_id": paper["arxiv_id"], "published": paper["published"],
                        "updated": paper["updated"], "authors": paper["authors"],
                        "categories": paper["categories"], "pdf_url": paper["pdf_url"],
                    },
                )
                registered.append({**paper, "source_id": evidence["source_id"]})
            self.db.add_event(self.task_id, "arxiv_search_completed", {
                "query": normalized, "result_count": len(registered),
                "cache_hit": cache_hit,
                "source_ids": [item["source_id"] for item in registered],
            })
            return json.dumps({
                "success": True, "query": normalized, "cache_hit": cache_hit,
                "result_count": len(registered), "papers": registered,
                "suggestion": None if registered else "No papers found. Try broader terms, field qualifiers, or remove the category filter.",
            }, ensure_ascii=False, indent=2)
        except Exception as exc:
            from app.runtime.control import TaskControlSignal
            if isinstance(exc, TaskControlSignal):
                raise
            return json.dumps(self._record_error("search", exc, {
                "query": str(query)[:500], "category": category,
            }), ensure_ascii=False)

    def paper(self, arxiv_id: str) -> str:
        try:
            self.task_tools.check_control()
            blocked = self.task_tools.budget_tool_gate("arxiv_paper")
            if blocked:
                return blocked
            paper = self.client.paper(arxiv_id)
            self.task_tools.check_control()
            if paper is None:
                raise ArxivRequestError(
                    f"arXiv paper not found: {arxiv_id}", error_type="not_found",
                    code="ARXIV_NOT_FOUND", retryable=False,
                )
            evidence = EvidenceRegistry(self.artifacts).register_source(
                url=paper["url"], title=paper["title"],
                content=paper["summary"], source_type="arxiv", verified=True,
                metadata={
                    "arxiv_id": paper["arxiv_id"], "published": paper["published"],
                    "updated": paper["updated"], "authors": paper["authors"],
                    "categories": paper["categories"], "pdf_url": paper["pdf_url"],
                },
            )
            pdf_result = self.task_tools.fetch_pdf(
                paper["pdf_url"], source_type="arxiv",
                evidence_url=paper["url"], evidence_title=paper["title"],
                evidence_metadata={
                    "arxiv_id": paper["arxiv_id"], "pdf_url": paper["pdf_url"],
                    "published": paper["published"], "authors": paper["authors"],
                    "categories": paper["categories"],
                },
            )
            self.task_tools.check_control()
            try:
                pdf_payload = json.loads(pdf_result)
            except (TypeError, ValueError):
                pdf_payload = {"success": False, "error": str(pdf_result)[:1000]}
            self.db.add_event(self.task_id, "arxiv_paper_read_completed", {
                "arxiv_id": paper["arxiv_id"], "source_id": evidence["source_id"],
                "pdf_success": bool(pdf_payload.get("success")),
                "pdf_source_id": pdf_payload.get("source_id"),
            })
            return json.dumps({
                "success": True, "paper": {**paper, "source_id": evidence["source_id"]},
                "full_text": pdf_payload,
            }, ensure_ascii=False, indent=2)
        except Exception as exc:
            from app.runtime.control import TaskControlSignal
            if isinstance(exc, TaskControlSignal):
                raise
            return json.dumps(self._record_error("paper", exc, {
                "arxiv_id": str(arxiv_id)[:100],
            }), ensure_ascii=False)

from __future__ import annotations

import json
import time

import pytest
from urllib.parse import parse_qs, urlparse

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.tools.arxiv import ArxivAdapter, ArxivClient, ArxivRequestError, normalize_search_query
from app.tools.research_tools import ResearchToolService
from app.runtime.control import TaskPaused

ATOM = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>https://arxiv.org/api/query</id>
  <entry>
    <id>https://arxiv.org/abs/2401.01234v2</id>
    <updated>2026-10-30T12:00:00Z</updated>
    <published>2026-01-15T09:00:00Z</published>
    <title>  A Study of Multivariate Time Series Anomaly Detection </title>
    <summary>We study cross-channel relationships and temporal patterns for unsupervised anomaly detection.</summary>
    <author><name>Alice Researcher</name></author>
    <author><name>Bob Scientist</name></author>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <arxiv:primary_category term="cs.LG"/>
    <link href="https://arxiv.org/abs/2401.01234v2" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2401.01234v2" title="pdf" rel="related" type="application/pdf"/>
  </entry>
</feed>'''


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {"Content-Type": "application/atom+xml"}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class FakeClock:
    def __init__(self, start=100.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_atom_parser_extracts_arxiv_metadata_authors_categories_and_pdf():
    papers = ArxivClient._parse_atom(ATOM)
    assert len(papers) == 1
    paper = papers[0]
    assert paper["arxiv_id"] == "2401.01234v2"
    assert paper["url"] == "https://arxiv.org/abs/2401.01234v2"
    assert paper["title"] == "A Study of Multivariate Time Series Anomaly Detection"
    assert paper["authors"] == ["Alice Researcher", "Bob Scientist"]
    assert paper["categories"] == ["cs.LG", "cs.AI"]
    assert paper["primary_category"] == "cs.LG"
    assert paper["pdf_url"] == "https://arxiv.org/pdf/2401.01234v2"
    assert paper["published"] == "2026-01-15T09:00:00Z"


def test_search_defaults_to_all_field_and_supports_boolean_field_queries():
    assert normalize_search_query("transformer anomaly") == "all:transformer anomaly"
    explicit = normalize_search_query('(ti:"time series" OR abs:anomaly) AND cat:cs.LG')
    assert explicit.startswith("(ti:")
    assert normalize_search_query("anomaly", "cs.LG") == "(all:anomaly) AND cat:cs.LG"


def test_arxiv_search_uses_official_atom_api_and_short_term_cache():
    ArxivClient.reset_process_state_for_tests()
    requests = []

    def opener(request, timeout):
        requests.append((request.full_url, timeout))
        return FakeResponse(ATOM)

    client = ArxivClient(urlopen_factory=opener)
    first, first_cached, normalized = client.search("cross-channel anomaly", max_results=5, category="cs.LG")
    second, second_cached, _ = client.search("cross-channel anomaly", max_results=5, category="cs.LG")

    assert len(requests) == 1
    query = parse_qs(urlparse(requests[0][0]).query)
    assert query["search_query"] == ["(all:cross-channel anomaly) AND cat:cs.LG"]
    assert query["max_results"] == ["5"]
    assert query["sortBy"] == ["relevance"]
    assert first_cached is False and second_cached is True
    assert first == second
    assert normalized == "(all:cross-channel anomaly) AND cat:cs.LG"


def test_arxiv_global_throttle_waits_at_least_three_seconds_between_requests():
    ArxivClient.reset_process_state_for_tests()
    clock = FakeClock()
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(ATOM)

    client = ArxivClient(urlopen_factory=opener, clock=clock.monotonic, sleep=clock.sleep)
    client.search("first distinct query")
    client.search("second distinct query")

    assert len(requests) == 2
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] >= 3.0


def test_empty_arxiv_search_is_success_with_query_suggestion():
    ArxivClient.reset_process_state_for_tests()
    empty_feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><id>empty</id></feed>'''
    client = ArxivClient(urlopen_factory=lambda request, timeout: FakeResponse(empty_feed))
    papers, cache_hit, query = client.search("unlikely phrase 000000")
    assert papers == []
    assert cache_hit is False
    assert query == "all:unlikely phrase 000000"


def test_arxiv_paper_uses_existing_fetch_pdf_and_registers_arxiv_source(settings, db, monkeypatch):
    task = db.create_task("Read an arXiv paper and extract its PDF in detail")
    task = db.claim_next_task("arxiv-worker")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())

    class FakeArxivClient:
        def paper(self, arxiv_id):
            assert arxiv_id == "2401.01234v2"
            return ArxivClient._parse_atom(ATOM)[0]

    fetch_calls = []

    def fake_fetch_pdf(url, **kwargs):
        fetch_calls.append((url, kwargs))
        source = EvidenceRegistry(artifacts).register_source(
            url=kwargs["evidence_url"], title=kwargs["evidence_title"],
            content="Extracted full paper text with methods and results. " * 5,
            source_type=kwargs["source_type"], metadata=kwargs["evidence_metadata"],
        )
        return json.dumps({
            "success": True, "source_id": source["source_id"],
            "source_type": kwargs["source_type"], "pdf_url": url,
        })

    monkeypatch.setattr(service, "fetch_pdf", fake_fetch_pdf)
    adapter = ArxivAdapter(settings, db, task["task_id"], artifacts, service, client=FakeArxivClient())
    result = json.loads(adapter.paper("2401.01234v2"))
    registry = EvidenceRegistry(artifacts).summary()

    assert result["success"] is True
    assert len(fetch_calls) == 1
    pdf_url, kwargs = fetch_calls[0]
    assert pdf_url == "https://arxiv.org/pdf/2401.01234v2"
    assert kwargs["source_type"] == "arxiv"
    assert kwargs["evidence_url"] == "https://arxiv.org/abs/2401.01234v2"
    assert result["full_text"]["success"] is True
    assert {item["source_type"] for item in registry["sources"]} == {"arxiv"}
    assert any(item["url"] == "https://arxiv.org/abs/2401.01234v2" for item in registry["sources"])
    assert any(event["event_type"] == "arxiv_paper_read_completed" for event in db.get_events(task["task_id"]))


def test_arxiv_search_registers_abstract_evidence_and_empty_result_suggestion(settings, db):
    task = db.create_task("Search arXiv for evidence in detail")
    task = db.claim_next_task("arxiv-search-worker")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())

    class FakeArxivClient:
        def search(self, query, max_results, sort_by, category):
            return ArxivClient._parse_atom(ATOM), False, "all:anomaly"

    adapter = ArxivAdapter(settings, db, task["task_id"], artifacts, service, client=FakeArxivClient())
    result = json.loads(adapter.search("anomaly"))
    assert result["success"] is True
    assert result["papers"][0]["source_id"] == "S0001"
    assert EvidenceRegistry(artifacts).summary()["sources"][0]["source_type"] == "arxiv"

    class EmptyArxivClient:
        def search(self, query, max_results, sort_by, category):
            return [], False, "all:rare phrase"

    adapter.client = EmptyArxivClient()
    empty = json.loads(adapter.search("rare phrase"))
    assert empty["success"] is True
    assert empty["papers"] == []
    assert "Try broader terms" in empty["suggestion"]


def test_arxiv_search_is_blocked_in_restricted_budget_state(settings, db):
    task = db.create_task("Restrict arXiv exploration at the budget gate", max_total_tokens=1000)
    task = db.claim_next_task("restricted-arxiv-worker")
    with db.transaction() as conn:
        conn.execute("UPDATE tasks SET total_tokens_used=960 WHERE task_id=?", (task["task_id"],))
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())

    class NeverCalledArxivClient:
        called = False
        def search(self, *args):
            self.called = True
            raise AssertionError("restricted arXiv search must not reach the provider")

    client = NeverCalledArxivClient()
    adapter = ArxivAdapter(settings, db, task["task_id"], artifacts, service, client=client)
    result = json.loads(adapter.search("cross-channel anomaly"))
    assert result["error_type"] == "budget_intercept"
    assert result["tool"] == "arxiv_search"
    assert client.called is False


def test_arxiv_search_caps_max_results_at_twenty():
    ArxivClient.reset_process_state_for_tests()
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(ATOM)

    client = ArxivClient(urlopen_factory=opener)
    client.search("large result set", max_results=99)
    query = parse_qs(urlparse(requests[0]).query)
    assert query["max_results"] == ["20"]


def test_arxiv_search_checks_task_control_after_provider_response_before_evidence(settings, db):
    task = db.create_task("Pause after arXiv search response")
    task = db.claim_next_task("arxiv-control-worker")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())

    class FakeArxivClient:
        def search(self, *args):
            return [ArxivClient._parse_atom(ATOM)[0]], False, "all:anomaly"

    checks = {"count": 0}
    def check_control():
        checks["count"] += 1
        if checks["count"] == 2:
            raise TaskPaused("operator requested pause")

    service.check_control = check_control
    service.budget_tool_gate = lambda tool_name: None
    adapter = ArxivAdapter(settings, db, task["task_id"], artifacts, service, client=FakeArxivClient())

    with pytest.raises(TaskPaused):
        adapter.search("anomaly")

    assert checks["count"] == 2
    assert EvidenceRegistry(artifacts).summary()["source_count"] == 0
    assert not any(event["event_type"] == "arxiv_search_completed" for event in db.get_events(task["task_id"]))


def test_arxiv_paper_checks_task_control_after_pdf_before_completion_event(settings, db, monkeypatch):
    task = db.create_task("Pause after arXiv PDF extraction")
    task = db.claim_next_task("arxiv-control-worker")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())

    class FakeArxivClient:
        def paper(self, arxiv_id):
            return ArxivClient._parse_atom(ATOM)[0]

    fetch_calls = []
    def fake_fetch_pdf(url, **kwargs):
        fetch_calls.append(url)
        return json.dumps({"success": True, "source_id": "S0002"})

    monkeypatch.setattr(service, "fetch_pdf", fake_fetch_pdf)
    checks = {"count": 0}
    def check_control():
        checks["count"] += 1
        if checks["count"] == 3:
            raise TaskPaused("operator requested pause")

    service.check_control = check_control
    service.budget_tool_gate = lambda tool_name: None
    adapter = ArxivAdapter(settings, db, task["task_id"], artifacts, service, client=FakeArxivClient())

    with pytest.raises(TaskPaused):
        adapter.paper("2401.01234v2")

    assert fetch_calls == ["https://arxiv.org/pdf/2401.01234v2"]
    assert checks["count"] == 3
    assert any(item["source_type"] == "arxiv" for item in EvidenceRegistry(artifacts).summary()["sources"])
    assert not any(event["event_type"] == "arxiv_paper_read_completed" for event in db.get_events(task["task_id"]))

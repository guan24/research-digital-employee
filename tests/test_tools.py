from __future__ import annotations

from app.infrastructure.artifact_store import ArtifactStore
from app.tools.research_tools import ResearchToolService


def test_research_tools_persist_search_evidence_and_artifacts(settings, db, monkeypatch):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, __import__("time").monotonic())
    service.save_artifact("notes/plan.md", "# Plan")
    assert service.read_artifact("notes/plan.md") == "# Plan"
    assert "notes/plan.md" in service.list_artifacts()


def test_web_search_requires_tavily_key(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, __import__("time").monotonic())
    try:
        service.web_search("example research")
    except RuntimeError as exc:
        assert "TAVILY_API_KEY" in str(exc)
    else:
        raise AssertionError("expected missing search key error")


def test_fetch_rejects_localhost(settings, db):
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, __import__("time").monotonic())
    try:
        service.fetch_url("http://127.0.0.1:8000/internal")
    except ValueError as exc:
        assert "local" in str(exc).lower() or "non-public" in str(exc).lower()
    else:
        raise AssertionError("expected local URL to be rejected")


def test_web_search_saves_results_when_provider_returns_data(settings, db, monkeypatch):
    from dataclasses import replace
    import app.tools.research_tools as research_tools

    configured = replace(settings, tavily_api_key="test-tavily-key")
    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    artifacts = ArtifactStore(configured.workspace_dir, task["task_id"])
    service = ResearchToolService(configured, db, task["task_id"], artifacts, __import__("time").monotonic())

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"title": "Official docs", "url": "https://example.org/docs", "content": "Primary source."}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(research_tools.httpx, "Client", FakeClient)
    result = service.web_search("official agent docs")
    assert "https://example.org/docs" in result
    assert any(item["path"].startswith("sources/search_") for item in artifacts.list_files())


def test_fetch_url_extracts_and_saves_html(settings, db, monkeypatch):
    import app.tools.research_tools as research_tools

    task = db.create_task("Research a topic with enough detail")
    task = db.claim_next_task("worker-a")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, __import__("time").monotonic())
    monkeypatch.setattr(service, "_assert_public_url", lambda _url: None)

    html = b"<html><head><title>Research Source</title></head><body><nav>Menu</nav><main><h1>Primary finding</h1><p>" + b"Useful evidence. " * 20 + b"</p></main></body></html>"

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8", "content-length": str(len(html))}
        url = "https://example.org/research"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield html

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(research_tools.httpx, "Client", FakeClient)
    result = service.fetch_url("https://example.org/research")
    assert "Primary finding" in result
    assert any(item["path"].startswith("sources/page_") for item in artifacts.list_files())

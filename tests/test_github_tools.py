from __future__ import annotations

import json
import time
from dataclasses import replace

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.executor import TaskEventCallback
from app.runtime.report_validator import ReportValidator
from app.tools.github_mcp import GitHubMCPToolError, GitHubMCPTransportError
from app.tools.github_sources import GitHubSourceAdapter
from app.tools.research_tools import ResearchToolService

REPO = "octo-labs/research-demo"
COMMIT = "a" * 40


class FakeMCPClient:
    def __init__(self):
        self.calls = []
        self.failures = {}
        self.file_text = {
            "README.md": "# Demo repository\n\nRepository overview and purpose.\n",
            "app/agent.py": "class ResearchAgent:\n    pass\n",
            "app/runtime.py": "class Runtime:\n    pass\n",
            "app/tools.py": "def register_tools():\n    return []\n",
        }
        self.directories = {
            "": [
                {"name": "README.md", "path": "README.md", "type": "file", "size": 60, "sha": "1"},
                {"name": "app", "path": "app", "type": "dir", "size": 0, "sha": "2"},
                {"name": ".env", "path": ".env", "type": "file", "size": 20, "sha": "3"},
                {"name": "logo.png", "path": "logo.png", "type": "file", "size": 100, "sha": "4"},
            ],
            "app": [
                {"name": "agent.py", "path": "app/agent.py", "type": "file", "size": 35, "sha": "5"},
                {"name": "runtime.py", "path": "app/runtime.py", "type": "file", "size": 30, "sha": "6"},
                {"name": "tools.py", "path": "app/tools.py", "type": "file", "size": 35, "sha": "7"},
            ],
        }

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.failures:
            failure = self.failures.pop(name)
            raise failure
        if name == "list_commits":
            return {"structuredContent": [{"sha": COMMIT, "html_url": f"https://github.com/{REPO}/commit/{COMMIT}"}]}
        if name == "get_file_contents":
            if "fields" in arguments:
                path = "" if arguments.get("path") == "/" else arguments.get("path", "")
                return {"structuredContent": self.directories.get(path, [])}
            path = arguments["path"]
            if path not in self.file_text:
                raise GitHubMCPToolError(
                    f"404 Not Found: {path}", error_type="mcp_tool_error",
                    code="GITHUB_MCP_TOOL_ERROR", retryable=False,
                )
            return {"structuredContent": {
                "content": [{"type": "text", "text": self.file_text[path]}],
            }}
        if name == "search_repositories":
            return {"structuredContent": {"items": [{
                "full_name": REPO, "description": "A demo research repository",
                "default_branch": "main", "language": "Python",
                "license": {"spdx_id": "MIT"}, "stargazers_count": 123,
                "pushed_at": "2026-11-10T10:00:00Z", "html_url": f"https://github.com/{REPO}",
            }]}}
        if name == "list_releases":
            return {"structuredContent": [{
                "tag_name": "v1.2.0", "name": "Version 1.2.0",
                "published_at": "2026-11-01T00:00:00Z",
                "html_url": f"https://github.com/{REPO}/releases/tag/v1.2.0",
            }]}
        raise AssertionError(f"unexpected MCP tool: {name}")


def build_service(settings, db, *, client=None, max_file_bytes=None):
    task = db.create_task("Research a GitHub repository architecture in detail")
    task = db.claim_next_task("github-test-worker")
    if max_file_bytes is not None:
        settings = replace(settings, github_mcp_max_file_bytes=max_file_bytes)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())
    adapter = GitHubSourceAdapter(settings, db, task["task_id"], artifacts, service, client=client or FakeMCPClient())
    service._github_source_adapter = adapter
    return task, service, adapter, artifacts


def test_batch_read_registers_four_github_file_sources_and_snapshots_in_one_tool_step(settings, db):
    client = FakeMCPClient()
    task, service, adapter, artifacts = build_service(settings, db, client=client)
    callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60)
    callback.on_tool_start({"name": "github_read"}, "{}", run_id="one-batch-read")

    result = json.loads(adapter.github_read(REPO, [
        "README.md", "app/agent.py", "app/runtime.py", "app/tools.py",
    ]))

    current = db.get_task(task["task_id"])
    registry = EvidenceRegistry(artifacts).summary()
    assert result["success"] is True
    assert len(result["files"]) == 4
    assert current["execution_steps"] == 1
    assert [item["source_id"] for item in result["files"]] == ["S0001", "S0002", "S0003", "S0004"]
    assert all(item["sha"] == COMMIT for item in result["files"])
    assert all(item["saved_to"].startswith(f"{task['task_id']}/sources/github/{COMMIT}/") for item in result["files"])
    assert all(item["source_id"] for item in result["files"])
    assert [item["source_type"] for item in registry["sources"]] == ["github_file"] * 4
    assert all(item["metadata"]["repo"] == REPO for item in registry["sources"])
    assert all(artifacts.exists(f"sources/github/{COMMIT}/{path}.md") for path in [
        "README.md", "app/agent.py", "app/runtime.py", "app/tools.py",
    ])
    assert sum(name == "get_file_contents" for name, _ in client.calls) >= 6
    assert sum(event["event_type"] == "github_file_read_completed" for event in db.get_events(task["task_id"])) == 4


def test_github_repo_tree_and_releases_expose_required_metadata_and_evidence(settings, db):
    client = FakeMCPClient()
    task, service, adapter, artifacts = build_service(settings, db, client=client)

    repo_result = json.loads(adapter.github_repo(REPO))
    tree_result = json.loads(adapter.github_tree(REPO))
    release_result = json.loads(adapter.github_releases(REPO, limit=5))
    registry = EvidenceRegistry(artifacts).summary()

    assert repo_result["success"] is True
    assert repo_result["description"] == "A demo research repository"
    assert repo_result["default_branch"] == "main"
    assert repo_result["language"] == "Python"
    assert repo_result["license"] == "MIT"
    assert repo_result["stars"] == 123
    assert repo_result["pushed_at"] == "2026-11-10T10:00:00Z"
    assert tree_result["success"] is True
    assert {item["path"] for item in tree_result["entries"]} == {"README.md", "app"}
    assert release_result["success"] is True
    assert release_result["releases"][0]["tag_name"] == "v1.2.0"
    assert {item["source_type"] for item in registry["sources"]} == {"github_repo"}


def test_unsafe_binary_and_oversized_paths_are_rejected_before_file_reads(settings, db):
    client = FakeMCPClient()
    task, service, adapter, artifacts = build_service(settings, db, client=client)

    for path in [
        "/etc/passwd", "C:/Windows/win.ini", "../secret.md", ".env",
        "config/.env.local", "assets/logo.png", "keys/id_rsa",
        ".aws/credentials", ".ssh/config", ".npmrc",
    ]:
        result = json.loads(adapter.github_read(REPO, [path]))
        assert result["success"] is False
        assert result["error_type"] == "policy_rejected"
    assert client.calls == []

    adapter._tree_cache[(REPO.lower(), "", "")] = {
        "huge.md": {"path": "huge.md", "type": "file", "size": 250_000},
    }
    oversized = json.loads(adapter.github_read(REPO, ["huge.md"]))
    assert oversized["success"] is False
    assert oversized["error_type"] == "policy_rejected"
    assert client.calls == []


def test_github_read_rejects_more_than_five_paths_without_network(settings, db):
    client = FakeMCPClient()
    task, service, adapter, artifacts = build_service(settings, db, client=client)
    result = json.loads(adapter.github_read(REPO, [f"file-{i}.md" for i in range(6)]))
    assert result["success"] is False
    assert result["retryable"] is False
    assert client.calls == []


def test_github_404_and_5xx_are_normalized_and_written_to_error_history(settings, db):
    client = FakeMCPClient()
    task, service, adapter, artifacts = build_service(settings, db, client=client)

    client.failures["search_repositories"] = GitHubMCPToolError(
        "404 Not Found", error_type="mcp_tool_error",
        code="GITHUB_MCP_TOOL_ERROR", retryable=False,
    )
    missing = json.loads(adapter.github_repo(REPO))
    assert missing["error_type"] == "not_found"
    assert missing["error_code"] == "GITHUB_NOT_FOUND"
    assert missing["retryable"] is False

    client.failures["list_releases"] = GitHubMCPTransportError(
        "upstream HTTP 503", error_type="upstream_5xx", code="GITHUB_UPSTREAM_5XX",
    )
    unavailable = json.loads(adapter.github_releases(REPO))
    assert unavailable["error_type"] == "upstream_5xx"
    assert unavailable["error_code"] == "GITHUB_UPSTREAM_5XX"
    assert unavailable["retryable"] is True

    current = db.get_task(task["task_id"])
    history = json.loads(current["error_history_json"])
    first = json.loads(current["first_error_json"])
    assert len(history) == 2
    assert first["error_code"] == "GITHUB_NOT_FOUND"
    assert history[1]["error_code"] == "GITHUB_UPSTREAM_5XX"
    events = db.get_events(task["task_id"])
    assert any(event["event_type"] == "github_repo_failed" and event["payload"]["retryable"] is False for event in events)
    assert any(event["event_type"] == "github_releases_failed" and event["payload"]["retryable"] is True for event in events)


def test_rate_limit_returns_retry_after_and_is_recorded(settings, db):
    client = FakeMCPClient()
    task, service, adapter, artifacts = build_service(settings, db, client=client)
    client.failures["list_releases"] = GitHubMCPToolError(
        "API rate limit exceeded", error_type="rate_limited",
        code="GITHUB_RATE_LIMITED", retryable=True, retry_after_seconds=45,
    )
    result = json.loads(adapter.github_releases(REPO))
    assert result["error_type"] == "rate_limited"
    assert result["retryable"] is True
    assert result["retry_after_seconds"] == 45
    assert json.loads(db.get_task(task["task_id"])["error_history_json"])[0]["error_code"] == "GITHUB_RATE_LIMITED"


def test_github_tools_are_restricted_at_tool_layer_but_artifact_writes_remain_available(settings, db):
    task = db.create_task("Restricted research source gate", max_total_tokens=1000)
    task = db.claim_next_task("restricted-github-worker")
    with db.transaction() as conn:
        conn.execute("UPDATE tasks SET total_tokens_used=960 WHERE task_id=?", (task["task_id"],))
    client = FakeMCPClient()
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())
    adapter = GitHubSourceAdapter(settings, db, task["task_id"], artifacts, service, client=client)
    service._github_source_adapter = adapter

    blocked = json.loads(adapter.github_repo(REPO))
    assert blocked["error_type"] == "budget_intercept"
    assert blocked["tool"] == "github_repo"
    assert client.calls == []
    service.save_artifact("notes/final-scope.md", "Use existing evidence only.")
    assert artifacts.read_text("notes/final-scope.md") == "Use existing evidence only."


def test_github_file_evidence_satisfies_existing_report_validator(settings, db):
    client = FakeMCPClient()
    task, service, adapter, artifacts = build_service(settings, db, client=client)
    result = json.loads(adapter.github_read(REPO, ["README.md"]))
    source_id = result["files"][0]["source_id"]
    source_url = result["files"][0]["url"]
    report = (
        "# GitHub 架构调研报告\n\n"
        "## 摘要\n\n"
        + ("该仓库通过模块化方式组织研究逻辑，README 描述了主要功能和项目边界，"
           "可将其作为分析入口并进一步结合实现文件核验关键职责。" * 2)
        + f" [{source_id}]\n\n"
        "## 结论\n\n"
        + ("从一手仓库文件可以核验项目的核心结构与模块边界，结论应以源代码和项目文档为依据。"
           * 2)
        + f" [{source_id}]\n\n"
        + f"## Sources\n\n- [{source_id}] {source_url}\n"
    )
    validation = ReportValidator().validate(report, "research repository architecture", EvidenceRegistry(artifacts).summary())
    assert validation.valid is True
    assert validation.cited_source_ids == (source_id,)
    assert validation.evidence_coverage >= 0.4


def test_research_role_registers_exactly_six_new_tools(settings, db):
    task, service, adapter, artifacts = build_service(settings, db, client=FakeMCPClient())
    names = {item.name for item in service.as_langchain_tools()}
    assert {
        "github_repo", "github_tree", "github_read", "github_releases",
        "arxiv_search", "arxiv_paper",
    }.issubset(names)


def test_missing_github_pat_is_a_structured_tool_error_and_does_not_fail_task(settings, db):
    from app.tools.github_mcp import GitHubMCPClient

    task = db.create_task("Continue research when optional GitHub credentials are absent")
    task = db.claim_next_task("no-token-worker")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())
    client = GitHubMCPClient(token="")
    adapter = GitHubSourceAdapter(settings, db, task["task_id"], artifacts, service, client=client)

    result = json.loads(adapter.github_repo(REPO))
    current = db.get_task(task["task_id"])
    assert result["success"] is False
    assert result["error_type"] == "configuration_error"
    assert result["retryable"] is False
    assert current["status"] == "running"
    assert current["error_code"] == "GITHUB_MCP_NOT_CONFIGURED"


def test_all_six_source_tools_are_individual_task_step_gated(settings, db):
    task = db.create_task("Count each newly registered research source tool", max_steps=10)
    task = db.claim_next_task("six-source-tools-worker")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())
    source_names = {
        "github_repo", "github_tree", "github_read", "github_releases",
        "arxiv_search", "arxiv_paper",
    }
    tools = {item.name: item for item in service.as_langchain_tools()}
    assert source_names.issubset(tools)
    callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60)
    for index, name in enumerate(sorted(source_names), start=1):
        callback.on_tool_start({"name": name}, "{}", run_id=f"source-tool-{index}")
    assert db.get_task(task["task_id"])["execution_steps"] == 6


def test_permission_denied_is_terminal_and_does_not_retry(settings, db):
    client = FakeMCPClient()
    task, service, adapter, artifacts = build_service(settings, db, client=client)
    client.failures["list_releases"] = GitHubMCPToolError(
        "403 Forbidden: repository permission denied",
        error_type="mcp_tool_error", code="GITHUB_MCP_TOOL_ERROR", retryable=False,
    )
    result = json.loads(adapter.github_releases(REPO))
    assert result["error_type"] == "permission_denied"
    assert result["error_code"] == "GITHUB_PERMISSION_DENIED"
    assert result["retryable"] is False
    history = json.loads(db.get_task(task["task_id"])["error_history_json"])
    assert history[0]["error_code"] == "GITHUB_PERMISSION_DENIED"
    assert history[0]["retryable"] is False


def test_unavailable_local_server_is_configuration_error_and_task_keeps_running(settings, db):
    from app.tools.github_mcp import GitHubMCPClient

    task = db.create_task("Continue with web sources if local GitHub MCP is unavailable")
    task = db.claim_next_task("missing-mcp-binary-worker")
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())

    def missing_binary(*args, **kwargs):
        raise FileNotFoundError("github-mcp-server not installed")

    client = GitHubMCPClient(token="configured-pat", popen_factory=missing_binary)
    adapter = GitHubSourceAdapter(settings, db, task["task_id"], artifacts, service, client=client)
    result = json.loads(adapter.github_repo(REPO))
    current = db.get_task(task["task_id"])

    assert result["error_type"] == "configuration_error"
    assert result["error_code"] == "GITHUB_MCP_NOT_CONFIGURED"
    assert result["retryable"] is False
    assert current["status"] == "running"

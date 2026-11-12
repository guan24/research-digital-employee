from __future__ import annotations

import json
import queue
import threading

import pytest

from app.tools.github_mcp import (
    READ_ONLY_GITHUB_TOOLS,
    GitHubMCPClient,
    GitHubMCPConfigurationError,
)


class FakeStdout:
    def __init__(self, messages):
        self.messages = messages

    def __iter__(self):
        while True:
            item = self.messages.get()
            if item is None:
                break
            yield item


class FakeStdin:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def write(self, line):
        if self.closed:
            raise BrokenPipeError("closed")
        message = json.loads(line)
        method = message.get("method")
        if method == "notifications/initialized":
            return len(line)
        if method == "tools/call" and self.process.crash_on_first_tool_call:
            self.process.crash_on_first_tool_call = False
            self.process.alive = False
            self.process.messages.put(None)
            return len(line)
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            names = list(READ_ONLY_GITHUB_TOOLS)
            if self.process.expose_write_tool:
                names.append("create_issue")
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in names]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "read-only-result"}], "isError": False}
        else:
            result = {}
        if "id" in message:
            self.process.messages.put(json.dumps({
                "jsonrpc": "2.0", "id": message["id"], "result": result,
            }) + "\n")
        return len(line)

    def flush(self):
        return None

    def close(self):
        self.closed = True
        self.process.alive = False
        self.process.messages.put(None)


class FakeProcess:
    def __init__(self, *, crash_on_first_tool_call=False, expose_write_tool=False):
        self.messages = queue.Queue()
        self.alive = True
        self.crash_on_first_tool_call = crash_on_first_tool_call
        self.expose_write_tool = expose_write_tool
        self.stdin = FakeStdin(self)
        self.stdout = FakeStdout(self.messages)

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        self.alive = False
        return 0

    def terminate(self):
        self.alive = False
        self.messages.put(None)

    def kill(self):
        self.alive = False
        self.messages.put(None)


def test_local_server_is_started_with_server_side_read_only_allow_list(monkeypatch):
    monkeypatch.setenv("GITHUB_TOOLSETS", "all")
    processes = []
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        process = FakeProcess()
        processes.append(process)
        return process

    client = GitHubMCPClient(
        token="test-pat", transport="stdio", command="github-mcp-server", popen_factory=popen,
    )
    result = client.call_tool("get_file_contents", {"owner": "octo", "repo": "demo"})
    assert result["content"][0]["text"] == "read-only-result"
    args, env = calls[0]
    assert args[:2] == ["github-mcp-server", "stdio"]
    assert "--read-only" in args
    assert "--tools=" + ",".join(READ_ONLY_GITHUB_TOOLS) in args
    assert env["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "test-pat"
    assert env["env"]["GITHUB_TOOLS"] == ",".join(READ_ONLY_GITHUB_TOOLS)
    assert env["env"]["GITHUB_READ_ONLY"] == "true"
    assert "GITHUB_TOOLSETS" not in env["env"]
    assert set(client.server_tools) == set(READ_ONLY_GITHUB_TOOLS)
    client.close()


def test_write_tool_is_rejected_before_server_start():
    calls = []
    client = GitHubMCPClient(
        token="test-pat", popen_factory=lambda *args, **kwargs: calls.append(args),
    )
    with pytest.raises(GitHubMCPConfigurationError):
        client.call_tool("create_issue", {"title": "must never be possible"})
    assert calls == []


def test_settings_default_to_github_hosted_readonly_mcp(settings):
    assert settings.github_mcp_transport == "remote"
    assert settings.github_mcp_remote_url == "https://api.githubcopilot.com/mcp/readonly"


def test_missing_token_returns_configuration_error_without_starting_process():
    calls = []
    client = GitHubMCPClient(token="", popen_factory=lambda *args, **kwargs: calls.append(args))
    with pytest.raises(GitHubMCPConfigurationError, match="GITHUB_PERSONAL_ACCESS_TOKEN"):
        client.call_tool("get_file_contents", {"owner": "octo", "repo": "demo"})
    assert calls == []


def test_mcp_crash_restarts_once_and_retries_the_read_call():
    processes = []

    def popen(args, **kwargs):
        process = FakeProcess(crash_on_first_tool_call=(len(processes) == 0))
        processes.append(process)
        return process

    client = GitHubMCPClient(
        token="test-pat", transport="stdio", popen_factory=popen, max_restarts=1,
    )
    result = client.call_tool("get_file_contents", {"owner": "octo", "repo": "demo"})
    assert result["content"][0]["text"] == "read-only-result"
    assert len(processes) == 2
    assert client._restart_count == 1
    client.close()
    assert all(not process.alive for process in processes)


def test_client_rejects_non_allowlisted_calls_even_if_server_advertises_extra_tools():
    process = FakeProcess(expose_write_tool=True)
    client = GitHubMCPClient(
        token="test-pat", transport="stdio", popen_factory=lambda *args, **kwargs: process,
    )
    result = client.call_tool("get_file_contents", {"owner": "octo", "repo": "demo"})
    assert result["content"][0]["text"] == "read-only-result"
    assert "create_issue" in client.server_tools
    with pytest.raises(GitHubMCPConfigurationError, match="not in the read-only allow-list"):
        client.call_tool("create_issue", {"title": "must never be possible"})
    client.close()


def test_shared_runtime_has_no_github_or_arxiv_provider_imports():
    from pathlib import Path
    import re

    runtime_dir = Path(__file__).resolve().parents[1] / "app" / "runtime"
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(?:app\.tools\.(?:github_mcp|github_sources|arxiv)|app\.agents\.research)",
        re.M,
    )
    hits = []
    for path in runtime_dir.rglob("*.py"):
        if forbidden.search(path.read_text(encoding="utf-8")):
            hits.append(path.as_posix())
    assert hits == []


def test_worker_run_once_closes_registered_github_mcp_process(settings, db, monkeypatch):
    from dataclasses import replace
    from app.runtime.worker import Worker
    from app.tools.github_mcp import get_github_mcp_client
    from app.tools import github_mcp

    process = FakeProcess()
    settings_with_pat = replace(
        settings, github_personal_access_token="worker-test-pat", github_mcp_transport="stdio"
    )
    monkeypatch.setattr(github_mcp.subprocess, "Popen", lambda *args, **kwargs: process)
    client = get_github_mcp_client(settings_with_pat)
    client.call_tool("get_file_contents", {"owner": "octo", "repo": "demo"})
    assert process.alive is True

    worker = Worker(settings_with_pat, db, executor=object())
    assert worker.run_once() is False
    assert process.alive is False


def test_default_transport_uses_github_hosted_readonly_endpoint():
    client = GitHubMCPClient(token="test-pat")
    assert client.transport == "remote"
    assert client.remote_url == "https://api.githubcopilot.com/mcp/readonly"
    client.close()


def test_remote_transport_sends_server_side_read_only_headers():
    requests = []

    class Response:
        def __init__(self, payload, status=200):
            self.payload = payload
            self.status = status
            self.headers = {"Content-Type": "application/json", "MCP-Session-Id": "session-1"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.payload

    def opener(request, timeout):
        requests.append(request)
        message = json.loads(request.data.decode("utf-8"))
        method = message["method"]
        if method == "initialize":
            payload = {"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2024-11-05"}}
            return Response(json.dumps(payload).encode())
        if method == "notifications/initialized":
            return Response(b"", status=202)
        if method == "tools/list":
            payload = {"jsonrpc": "2.0", "id": message["id"], "result": {
                "tools": [{"name": name} for name in READ_ONLY_GITHUB_TOOLS],
            }}
            return Response(json.dumps(payload).encode())
        payload = {"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": "remote-read-only"}],
        }}
        return Response(json.dumps(payload).encode())

    client = GitHubMCPClient(
        token="remote-test-pat", transport="remote",
        remote_url="https://api.githubcopilot.com/mcp/readonly",
        urlopen_factory=opener,
    )
    result = client.call_tool("get_file_contents", {"owner": "octo", "repo": "demo"})
    assert result["content"][0]["text"] == "remote-read-only"
    assert len(requests) == 4
    for request in requests:
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["x-mcp-tools"] == ",".join(READ_ONLY_GITHUB_TOOLS)
        assert headers["x-mcp-readonly"] == "true"
        assert headers["authorization"] == "Bearer remote-test-pat"
        assert request.full_url == "https://api.githubcopilot.com/mcp/readonly"
    client.close()


def test_remote_http_403_is_permission_denied_not_configuration_or_retryable():
    import io
    import urllib.error

    class Response:
        def __init__(self, payload, status=200):
            self.payload = payload
            self.status = status
            self.headers = {"Content-Type": "application/json", "MCP-Session-Id": "permission-session"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.payload

    def opener(request, timeout):
        message = json.loads(request.data.decode("utf-8"))
        if message["method"] == "initialize":
            return Response(json.dumps({
                "jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2024-11-05"},
            }).encode())
        if message["method"] == "notifications/initialized":
            return Response(b"", status=202)
        if message["method"] == "tools/list":
            return Response(json.dumps({
                "jsonrpc": "2.0", "id": message["id"],
                "result": {"tools": [{"name": name} for name in READ_ONLY_GITHUB_TOOLS]},
            }).encode())
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {"Content-Type": "application/json"},
            io.BytesIO(b'{"message":"Resource not accessible by integration"}'),
        )

    from app.tools.github_mcp import GitHubMCPToolError
    client = GitHubMCPClient(
        token="permission-pat", transport="remote",
        remote_url="https://api.githubcopilot.com/mcp/readonly", urlopen_factory=opener,
    )
    with pytest.raises(GitHubMCPToolError) as captured:
        client.call_tool("get_file_contents", {"owner": "octo", "repo": "private"})
    assert captured.value.error_type == "permission_denied"
    assert captured.value.code == "GITHUB_PERMISSION_DENIED"
    assert captured.value.retryable is False
    client.close()


def test_missing_official_server_binary_is_a_nonretryable_configuration_error():
    def missing_binary(*args, **kwargs):
        raise FileNotFoundError("not installed")

    client = GitHubMCPClient(
        token="configured-pat", transport="stdio", popen_factory=missing_binary
    )
    with pytest.raises(GitHubMCPConfigurationError, match="Install github-mcp-server"):
        client.call_tool("get_file_contents", {"owner": "octo", "repo": "demo"})

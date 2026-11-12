from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

READ_ONLY_GITHUB_TOOLS = (
    "get_file_contents",
    "search_code",
    "search_repositories",
    "list_commits",
    "get_commit",
    "list_releases",
    "list_branches",
)


class GitHubMCPError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        code: str,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class GitHubMCPConfigurationError(GitHubMCPError):
    def __init__(self, message: str):
        super().__init__(
            message, error_type="configuration_error",
            code="GITHUB_MCP_NOT_CONFIGURED", retryable=False,
        )


class GitHubMCPTransportError(GitHubMCPError):
    def __init__(self, message: str, *, error_type: str = "mcp_unavailable",
                 code: str = "GITHUB_MCP_UNAVAILABLE"):
        super().__init__(
            message, error_type=error_type, code=code, retryable=True,
        )


class GitHubMCPToolError(GitHubMCPError):
    pass


class GitHubMCPClient:
    """Worker-process-scoped GitHub MCP client with a server-enforced read-only allow-list."""

    def __init__(
        self,
        *,
        token: str,
        transport: str = "stdio",
        command: str = "github-mcp-server",
        remote_url: str = "https://api.githubcopilot.com/mcp/",
        timeout_seconds: float = 20.0,
        max_restarts: int = 1,
        popen_factory=None,
        urlopen_factory=None,
    ):
        self.token = str(token or "").strip()
        self.transport = str(transport or "stdio").strip().lower()
        self.command = command or "github-mcp-server"
        self.remote_url = remote_url or "https://api.githubcopilot.com/mcp/"
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_restarts = max(0, min(int(max_restarts), 3))
        self._popen = popen_factory or subprocess.Popen
        self._urlopen = urlopen_factory or urllib.request.urlopen
        self._lock = threading.RLock()
        self._process = None
        self._responses: queue.Queue = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._request_id = 0
        self._session_id: str | None = None
        self._ready = False
        self._ever_ready = False
        self._closed = False
        self._restart_count = 0
        self._server_tools: set[str] = set()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def server_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._server_tools))

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _ensure_configuration(self) -> None:
        if not self.token:
            raise GitHubMCPConfigurationError(
                "GitHub MCP is not configured: set GITHUB_PERSONAL_ACCESS_TOKEN."
            )
        if self.transport not in {"stdio", "remote"}:
            raise GitHubMCPConfigurationError(
                "GITHUB_MCP_TRANSPORT must be either 'stdio' or 'remote'."
            )
        if self.transport == "remote" and not self.remote_url.startswith("https://"):
            raise GitHubMCPConfigurationError(
                "The remote GitHub MCP endpoint must use HTTPS."
            )

    def _read_stdout(self, process, responses: queue.Queue) -> None:
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    responses.put(payload)
        finally:
            responses.put({"_mcp_eof": True})

    def _start_stdio(self, *, restarting: bool = False) -> None:
        args = shlex.split(self.command)
        if not args:
            raise GitHubMCPConfigurationError("GITHUB_MCP_SERVER_COMMAND is empty.")
        args.extend([
            "stdio",
            "--read-only",
            "--tools=" + ",".join(READ_ONLY_GITHUB_TOOLS),
        ])
        env = os.environ.copy()
        env["GITHUB_PERSONAL_ACCESS_TOKEN"] = self.token
        env["GITHUB_TOOLS"] = ",".join(READ_ONLY_GITHUB_TOOLS)
        env["GITHUB_READ_ONLY"] = "true"
        try:
            process = self._popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise GitHubMCPConfigurationError(
                "Cannot start the official GitHub MCP server. Install github-mcp-server "
                "and set GITHUB_MCP_SERVER_COMMAND, or select remote transport."
            ) from exc

        self._process = process
        self._responses = queue.Queue()
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            args=(process, self._responses),
            name="github-mcp-stdout",
            daemon=True,
        )
        self._reader_thread.start()
        try:
            self._rpc_stdio("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "research-digital-employee", "version": "0.1.0"},
            })
            self._notify_stdio("notifications/initialized")
            listed = self._rpc_stdio("tools/list", {})
            self._validate_server_tools(listed)
            self._ready = True
            self._ever_ready = True
        except GitHubMCPError as exc:
            self._close_process()
            if restarting:
                raise GitHubMCPTransportError(
                    f"GitHub MCP server restart failed: {exc}",
                    error_type="mcp_restart_failed",
                    code="GITHUB_MCP_RESTART_FAILED",
                ) from exc
            raise GitHubMCPConfigurationError(
                f"GitHub MCP server did not complete initialization: {exc}"
            ) from exc

    def _validate_server_tools(self, payload: Any) -> None:
        tools = payload.get("tools", []) if isinstance(payload, dict) else []
        names = {
            str(item.get("name"))
            for item in tools
            if isinstance(item, dict) and item.get("name")
        }
        unexpected = sorted(names.difference(READ_ONLY_GITHUB_TOOLS))
        if unexpected:
            raise GitHubMCPConfigurationError(
                "GitHub MCP server exposed tools outside the read-only allow-list: "
                + ", ".join(unexpected)
            )
        self._server_tools = names

    def _rpc_stdio(self, method: str, params: dict[str, Any]) -> Any:
        process = self._process
        if process is None or process.poll() is not None:
            raise GitHubMCPTransportError(
                "GitHub MCP stdio process is not running.",
                error_type="mcp_crashed",
                code="GITHUB_MCP_CRASHED",
            )
        request_id = self._next_id()
        message = {
            "jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params,
        }
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise GitHubMCPTransportError(
                "GitHub MCP process closed its input stream.",
                error_type="mcp_crashed",
                code="GITHUB_MCP_CRASHED",
            ) from exc

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitHubMCPTransportError(
                    f"GitHub MCP request timed out after {self.timeout_seconds:g}s.",
                    error_type="timeout",
                    code="GITHUB_MCP_TIMEOUT",
                )
            try:
                response = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise GitHubMCPTransportError(
                    f"GitHub MCP request timed out after {self.timeout_seconds:g}s.",
                    error_type="timeout",
                    code="GITHUB_MCP_TIMEOUT",
                ) from exc
            if response.get("_mcp_eof"):
                raise GitHubMCPTransportError(
                    "GitHub MCP server exited unexpectedly.",
                    error_type="mcp_crashed",
                    code="GITHUB_MCP_CRASHED",
                )
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                error = response["error"]
                raise GitHubMCPToolError(
                    str(error.get("message") or error)[:1000],
                    error_type="mcp_tool_error",
                    code="GITHUB_MCP_TOOL_ERROR",
                    retryable=False,
                )
            return response.get("result", {})

    def _notify_stdio(self, method: str) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": method,
        }) + "\n")
        process.stdin.flush()

    @staticmethod
    def _decode_http_response(body: bytes, content_type: str) -> dict[str, Any]:
        text = body.decode("utf-8", errors="replace").strip()
        if "text/event-stream" in content_type:
            candidates = [
                line[5:].strip() for line in text.splitlines()
                if line.startswith("data:")
            ]
            for candidate in reversed(candidates):
                try:
                    value = json.loads(candidate)
                    if isinstance(value, dict):
                        return value
                except ValueError:
                    continue
            raise GitHubMCPTransportError(
                "Remote GitHub MCP returned an invalid event stream.",
                error_type="invalid_mcp_response",
                code="GITHUB_MCP_INVALID_RESPONSE",
            )
        try:
            value = json.loads(text) if text else {}
        except ValueError as exc:
            raise GitHubMCPTransportError(
                "Remote GitHub MCP returned invalid JSON.",
                error_type="invalid_mcp_response",
                code="GITHUB_MCP_INVALID_RESPONSE",
            ) from exc
        if not isinstance(value, dict):
            raise GitHubMCPTransportError(
                "Remote GitHub MCP returned a non-object response.",
                error_type="invalid_mcp_response",
                code="GITHUB_MCP_INVALID_RESPONSE",
            )
        return value

    def _rpc_remote(self, method: str, params: dict[str, Any], *, notification: bool = False) -> Any:
        request_id = None if notification else self._next_id()
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            message["id"] = request_id
        if params:
            message["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            "X-MCP-Tools": ",".join(READ_ONLY_GITHUB_TOOLS),
            "X-MCP-Readonly": "true",
        }
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = "2024-11-05"
        request = urllib.request.Request(
            self.remote_url, data=json.dumps(message).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with self._urlopen(request, timeout=self.timeout_seconds) as response:
                session_id = response.headers.get("MCP-Session-Id")
                if session_id:
                    self._session_id = session_id
                status = getattr(response, "status", 200)
                body = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            retry_after = exc.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else None
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = ""
            rate_limited = (
                status == 429
                or (status == 403 and (
                    "rate limit" in error_body.lower()
                    or exc.headers.get("X-RateLimit-Remaining") == "0"
                ))
            )
            if rate_limited:
                if wait is None:
                    reset = exc.headers.get("X-RateLimit-Reset")
                    if reset and reset.isdigit():
                        wait = max(1, int(reset) - int(time.time()))
                raise GitHubMCPToolError(
                    "GitHub MCP rate limit reached.",
                    error_type="rate_limited", code="GITHUB_RATE_LIMITED",
                    retryable=True, retry_after_seconds=wait or 60,
                ) from exc
            if status in {401, 403}:
                raise GitHubMCPConfigurationError(
                    "GitHub MCP authentication or repository permission was rejected."
                ) from exc
            if status == 404:
                raise GitHubMCPToolError(
                    "GitHub MCP resource was not found (HTTP 404).",
                    error_type="not_found", code="GITHUB_NOT_FOUND",
                    retryable=False,
                ) from exc
            if status >= 500 or status in {408, 425}:
                raise GitHubMCPTransportError(
                    f"GitHub MCP upstream returned HTTP {status}.",
                    error_type="upstream_5xx", code="GITHUB_UPSTREAM_5XX",
                ) from exc
            raise GitHubMCPToolError(
                f"GitHub MCP endpoint returned HTTP {status}.",
                error_type="http_error", code="GITHUB_MCP_HTTP_ERROR",
                retryable=False,
            ) from exc
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            raise GitHubMCPTransportError(
                f"Remote GitHub MCP connection failed: {type(exc).__name__}.",
                error_type="network_error", code="GITHUB_MCP_NETWORK_ERROR",
            ) from exc
        if notification and status in {202, 204}:
            return {}
        payload = self._decode_http_response(body, content_type)
        if payload.get("error"):
            error = payload["error"]
            raise GitHubMCPToolError(
                str(error.get("message") or error)[:1000],
                error_type="mcp_tool_error", code="GITHUB_MCP_TOOL_ERROR",
                retryable=False,
            )
        return payload.get("result", {})

    def _start_remote(self, *, restarting: bool = False) -> None:
        self._session_id = None
        try:
            self._rpc_remote("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "research-digital-employee", "version": "0.1.0"},
            })
            self._rpc_remote("notifications/initialized", {}, notification=True)
            listed = self._rpc_remote("tools/list", {})
            self._validate_server_tools(listed)
            self._ready = True
            self._ever_ready = True
        except GitHubMCPError as exc:
            if restarting:
                raise GitHubMCPTransportError(
                    f"Remote GitHub MCP restart failed: {exc}",
                    error_type="mcp_restart_failed",
                    code="GITHUB_MCP_RESTART_FAILED",
                ) from exc
            raise GitHubMCPConfigurationError(
                f"Remote GitHub MCP initialization failed: {exc}"
            ) from exc

    def _ensure_ready(self) -> None:
        self._ensure_configuration()
        if self._closed:
            raise GitHubMCPConfigurationError("GitHub MCP client has been closed.")
        if self._ready:
            if self.transport == "stdio" and (
                self._process is None or self._process.poll() is not None
            ):
                self._ready = False
                raise GitHubMCPTransportError(
                    "GitHub MCP server process has exited.",
                    error_type="mcp_crashed",
                    code="GITHUB_MCP_CRASHED",
                )
            return
        if self.transport == "stdio":
            self._start_stdio(restarting=self._ever_ready)
        else:
            self._start_remote(restarting=self._ever_ready)

    def _restart(self) -> None:
        self._ready = False
        self._close_process()
        self._session_id = None
        if self.transport == "stdio":
            self._start_stdio(restarting=True)
        else:
            self._start_remote(restarting=True)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in READ_ONLY_GITHUB_TOOLS:
            raise GitHubMCPConfigurationError(
                f"GitHub MCP tool '{name}' is not in the read-only allow-list."
            )
        with self._lock:
            while True:
                try:
                    self._ensure_ready()
                    if self.transport == "stdio":
                        result = self._rpc_stdio("tools/call", {
                            "name": name, "arguments": arguments,
                        })
                    else:
                        result = self._rpc_remote("tools/call", {
                            "name": name, "arguments": arguments,
                        })
                    if isinstance(result, dict) and result.get("isError"):
                        text_parts = [
                            item.get("text", "") for item in result.get("content", [])
                            if isinstance(item, dict) and item.get("type") == "text"
                        ]
                        raise GitHubMCPToolError(
                            "\n".join(text_parts)[:1200] or "GitHub MCP tool returned an error.",
                            error_type="mcp_tool_error",
                            code="GITHUB_MCP_TOOL_ERROR",
                            retryable=False,
                        )
                    return result
                except GitHubMCPTransportError:
                    if self._restart_count >= self.max_restarts:
                        raise
                    self._restart_count += 1
                    self._restart()

    def _close_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        self._ready = False

    def close(self) -> None:
        with self._lock:
            self._close_process()
            self._session_id = None
            self._ready = False
            self._closed = True


_CLIENTS: dict[tuple[str, str, str, str, float, int], GitHubMCPClient] = {}
_CLIENTS_LOCK = threading.Lock()


def get_github_mcp_client(settings) -> GitHubMCPClient:
    token = getattr(settings, "github_personal_access_token", "") or ""
    key = (
        str(getattr(settings, "github_mcp_transport", "stdio")),
        str(getattr(settings, "github_mcp_server_command", "github-mcp-server")),
        str(getattr(settings, "github_mcp_remote_url", "https://api.githubcopilot.com/mcp/")),
        __import__("hashlib").sha256(token.encode("utf-8")).hexdigest(),
        float(getattr(settings, "github_mcp_timeout_seconds", 20.0)),
        int(getattr(settings, "github_mcp_max_restarts", 1)),
    )
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None or client.closed:
            client = GitHubMCPClient(
                token=token,
                transport=key[0],
                command=key[1],
                remote_url=key[2],
                timeout_seconds=key[4],
                max_restarts=key[5],
            )
            _CLIENTS[key] = client
            from app.infrastructure.lifecycle import register_shutdown_callback
            register_shutdown_callback(client.close)
        return client

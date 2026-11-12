from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.control import TaskControlSignal
from app.tools.github_mcp import (
    GitHubMCPClient,
    GitHubMCPConfigurationError,
    GitHubMCPError,
    GitHubMCPToolError,
    get_github_mcp_client,
)

_BINARY_SUFFIXES = {
    ".7z", ".a", ".avi", ".bin", ".bmp", ".class", ".db", ".dll", ".doc",
    ".docx", ".dylib", ".eot", ".exe", ".gif", ".gz", ".ico", ".jar",
    ".jpeg", ".jpg", ".keystore", ".m4a", ".mkv", ".mov", ".mp3", ".mp4",
    ".npy", ".npz", ".o", ".pdf", ".parquet", ".p12", ".pfx", ".pickle",
    ".pkl", ".png", ".ppt", ".pptx", ".pyc", ".rar", ".so", ".sqlite",
    ".sqlite3", ".tar", ".tif", ".tiff", ".ttf", ".wasm", ".webp", ".woff",
    ".woff2", ".xls", ".xlsx", ".xz", ".zip",
}
_SECRET_PARTS = {
    "id_rsa", "id_ed25519", "id_ecdsa", "credentials", "credential",
    "secrets", "secret", "password", "passwd", "token", "private_key",
    ".ssh", ".aws", ".gnupg", ".kube", ".npmrc", ".pypirc", ".netrc",
    ".dockerconfigjson",
}
_OWNER_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class GitHubPolicyError(ValueError):
    pass


def validate_repository(repo: str) -> tuple[str, str]:
    value = str(repo or "").strip()
    if not _OWNER_REPO.fullmatch(value):
        raise GitHubPolicyError("repo must use the owner/repository format")
    owner, name = value.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise GitHubPolicyError("invalid repository identifier")
    return owner, name


def validate_github_path(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        raise GitHubPolicyError("file path cannot be empty")
    if (
        value.startswith(("/", "~")) or "\\" in value
        or re.match(r"^[A-Za-z]:[/\\]", value)
    ):
        raise GitHubPolicyError("absolute paths and backslash paths are not allowed")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"..", ".", "", "~"} for part in pure.parts):
        raise GitHubPolicyError("path traversal is not allowed")
    lowered = [part.lower() for part in pure.parts]
    if any(part == ".git" or part.startswith(".env") for part in lowered):
        raise GitHubPolicyError(".git and .env paths are not accessible")
    basename = lowered[-1]
    secret_name = re.search(r"(?:^|[._-])(secret|credentials?|password|passwd|token)(?:[._-]|$)", basename)
    if (
        basename in _SECRET_PARTS
        or any(part in _SECRET_PARTS for part in lowered)
        or secret_name
        or basename.endswith((".pem", ".key", ".p12", ".pfx", ".asc", ".gpg", ".tfstate"))
        or basename.startswith("id_rsa")
        or basename.startswith("id_ed25519")
    ):
        raise GitHubPolicyError("secret or private-key files are not accessible")
    if PurePosixPath(basename).suffix.lower() in _BINARY_SUFFIXES:
        raise GitHubPolicyError("binary files are not accessible through github_read")
    if any(ord(char) < 32 for char in value):
        raise GitHubPolicyError("control characters are not allowed in paths")
    return pure.as_posix()


def _decode_result(raw, *, parse_text_json: bool = False):
    if isinstance(raw, dict) and "structuredContent" in raw:
        return raw["structuredContent"]
    if isinstance(raw, dict) and "result" in raw:
        raw = raw["result"]
    if isinstance(raw, dict) and isinstance(raw.get("content"), list):
        text_parts = [
            str(item.get("text", ""))
            for item in raw["content"]
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        text = "\n".join(text_parts)
        if parse_text_json and text:
            try:
                return json.loads(text)
            except ValueError:
                pass
        return text
    if isinstance(raw, str) and parse_text_json:
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _extract_file_text(payload) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("content"), list):
            parts = []
            for item in payload["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, dict) and item.get("type") == "resource":
                    resource = item.get("resource") or {}
                    if resource.get("text"):
                        parts.append(str(resource["text"]))
            if parts:
                return "\n".join(parts)
        for key in ("text", "file_content", "contents"):
            if isinstance(payload.get(key), str):
                return payload[key]
    raise GitHubPolicyError("GitHub MCP returned no readable text content")


class GitHubSourceAdapter:
    """Research-role adapter: policy, evidence snapshots, events and error normalization."""

    def __init__(self, settings, db, task_id: str, artifacts, task_tools,
                 client: GitHubMCPClient | None = None):
        self.settings = settings
        self.db = db
        self.task_id = task_id
        self.artifacts = artifacts
        self.task_tools = task_tools
        self.client = client or get_github_mcp_client(settings)
        self.max_file_bytes = max(1024, int(getattr(settings, "github_mcp_max_file_bytes", 200_000)))
        self.snapshot_bytes = min(self.max_file_bytes, 120_000)
        self._tree_cache: dict[tuple[str, str, str], dict[str, dict]] = {}
        self._tree_commit_cache: dict[tuple[str, str, str], str] = {}

    def _record_error(self, operation: str, exc: Exception, *, context: dict | None = None) -> dict:
        if isinstance(exc, GitHubMCPConfigurationError):
            error_type, code, retryable, retry_after = exc.error_type, exc.code, False, None
        elif isinstance(exc, GitHubMCPToolError):
            message = str(exc).lower()
            if "404" in message or "not found" in message:
                error_type, code, retryable, retry_after = "not_found", "GITHUB_NOT_FOUND", False, None
            elif "401" in message or "403" in message or "permission" in message or "forbidden" in message:
                error_type, code, retryable, retry_after = "permission_denied", "GITHUB_PERMISSION_DENIED", False, None
            elif "429" in message or "rate limit" in message:
                error_type, code, retryable, retry_after = "rate_limited", "GITHUB_RATE_LIMITED", True, exc.retry_after_seconds or 60
            elif "timeout" in message:
                error_type, code, retryable, retry_after = "timeout", "GITHUB_TIMEOUT", True, None
            elif re.search(r"\b5\d\d\b", message):
                error_type, code, retryable, retry_after = "upstream_5xx", "GITHUB_UPSTREAM_5XX", True, None
            else:
                error_type, code, retryable, retry_after = exc.error_type, exc.code, bool(exc.retryable), exc.retry_after_seconds
        elif isinstance(exc, GitHubMCPError):
            error_type, code, retryable = exc.error_type, exc.code, bool(exc.retryable)
            retry_after = exc.retry_after_seconds
        else:
            message = str(exc).lower()
            if "404" in message or "not found" in message:
                error_type, code, retryable, retry_after = "not_found", "GITHUB_NOT_FOUND", False, None
            elif "401" in message or "403" in message or "permission" in message or "forbidden" in message:
                error_type, code, retryable, retry_after = "permission_denied", "GITHUB_PERMISSION_DENIED", False, None
            elif "429" in message or "rate limit" in message:
                error_type, code, retryable, retry_after = "rate_limited", "GITHUB_RATE_LIMITED", True, 60
            elif "timeout" in message:
                error_type, code, retryable, retry_after = "timeout", "GITHUB_TIMEOUT", True, None
            elif re.search(r"\b5\d\d\b", message):
                error_type, code, retryable, retry_after = "upstream_5xx", "GITHUB_UPSTREAM_5XX", True, None
            elif isinstance(exc, GitHubPolicyError):
                error_type, code, retryable, retry_after = "policy_rejected", "GITHUB_POLICY_REJECTED", False, None
            else:
                error_type, code, retryable, retry_after = "unknown", "UNKNOWN_GITHUB_ERROR", True, None
        message = str(exc)[:1200]
        task = self.db.get_task(self.task_id) or {}
        self.db.record_classified_error(
            self.task_id, message, "retryable" if retryable else "terminal",
            code, retryable, int(task.get("attempts", 0)),
        )
        payload = {
            "operation": operation, "error_type": error_type, "error_code": code,
            "retryable": retryable, "retry_after_seconds": retry_after,
            "error": message,
        }
        payload.update(context or {})
        self.db.add_event(self.task_id, f"github_{operation}_failed", payload)
        return {
            "success": False, "error_type": error_type, "error_code": code,
            "retryable": retryable, "retry_after_seconds": retry_after,
            "error": message, **(context or {}),
        }

    def _gate(self, tool_name: str) -> str | None:
        self.task_tools.check_control()
        return self.task_tools.budget_tool_gate(tool_name)

    def _call(self, name: str, arguments: dict, *, parse_text_json: bool = True):
        return _decode_result(
            self.client.call_tool(name, arguments), parse_text_json=parse_text_json
        )

    def _register_evidence(self, *, url: str, title: str, content: str,
                           source_type: str, metadata: dict) -> dict:
        return EvidenceRegistry(self.artifacts).register_source(
            url=url, title=title, content=content, source_type=source_type,
            verified=True, metadata=metadata,
        )

    def github_repo(self, repo: str) -> str:
        try:
            blocked = self._gate("github_repo")
            if blocked:
                return blocked
            owner, name = validate_repository(repo)
            self.db.add_event(self.task_id, "github_repo_lookup_started", {"repo": repo})
            match = None
            for owner_qualifier in (f"user:{owner}", f"org:{owner}"):
                payload = self._call("search_repositories", {
                    "query": f"{name} in:name {owner_qualifier}",
                    "perPage": 100,
                    "minimal_output": False,
                })
                self.task_tools.check_control()
                items = payload.get("items", []) if isinstance(payload, dict) else []
                match = next(
                    (item for item in items if str(item.get("full_name", "")).lower() == repo.lower()),
                    None,
                )
                if match:
                    break
            if not match:
                raise GitHubMCPToolError(
                    f"GitHub repository not found: {repo}", error_type="not_found",
                    code="GITHUB_NOT_FOUND", retryable=False,
                )
            overview = {
                "repo": match.get("full_name", repo),
                "description": match.get("description"),
                "default_branch": match.get("default_branch"),
                "language": match.get("language"),
                "license": (match.get("license") or {}).get("spdx_id") or (match.get("license") or {}).get("name"),
                "stars": match.get("stargazers_count"),
                "pushed_at": match.get("pushed_at"),
                "url": match.get("html_url") or f"https://github.com/{owner}/{name}",
            }
            evidence = self._register_evidence(
                url=overview["url"], title=f"GitHub repository {repo}",
                content=json.dumps(overview, ensure_ascii=False, indent=2),
                source_type="github_repo",
                metadata={"repo": repo, "ref": match.get("default_branch"), "sha": None, "kind": "repository_overview"},
            )
            overview["source_id"] = evidence["source_id"]
            self.db.add_event(self.task_id, "github_repo_lookup_completed", {
                "repo": repo, "source_id": evidence["source_id"],
            })
            return json.dumps({"success": True, **overview}, ensure_ascii=False, indent=2)
        except TaskControlSignal:
            raise
        except Exception as exc:
            return json.dumps(self._record_error("repo", exc, context={"repo": str(repo)}), ensure_ascii=False)

    def _directory_entries(self, owner: str, name: str, repo: str, path: str, ref: str | None):
        args = {
            "owner": owner, "repo": name, "path": path or "/",
            "fields": ["name", "path", "type", "size", "sha", "html_url"],
        }
        if ref:
            args["ref"] = ref
        payload = self._call("get_file_contents", args, parse_text_json=True)
        entries = payload if isinstance(payload, list) else payload.get("items", []) if isinstance(payload, dict) else []
        index: dict[str, dict] = {}
        filtered = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            item_path = str(item.get("path") or "")
            try:
                safe_path = validate_github_path(item_path)
            except GitHubPolicyError:
                continue
            if item.get("type") == "file":
                try:
                    validate_github_path(item_path)
                except GitHubPolicyError:
                    continue
            index[safe_path] = item
            if item.get("type") == "file" and int(item.get("size") or 0) > self.max_file_bytes:
                continue
            filtered.append({
                "name": item.get("name"), "path": safe_path,
                "type": item.get("type"), "size_bytes": item.get("size"),
                "sha": item.get("sha"), "url": item.get("html_url"),
            })
        self._tree_cache[(repo.lower(), ref or "", path or "")] = index
        return filtered, index

    def github_tree(self, repo: str, path: str = "", ref: str | None = None) -> str:
        try:
            blocked = self._gate("github_tree")
            if blocked:
                return blocked
            owner, name = validate_repository(repo)
            safe_dir = ""
            if str(path or "").strip():
                safe_dir = validate_github_path(path)
            self.task_tools.check_control()
            self.db.add_event(self.task_id, "github_tree_index_started", {
                "repo": repo, "path": safe_dir, "ref": ref,
            })
            commit_sha = self._resolve_commit(owner, name, ref)
            self.task_tools.check_control()
            entries, index = self._directory_entries(owner, name, repo, safe_dir, commit_sha)
            # Cache both the caller's ref alias and the immutable commit ref.
            self._tree_cache[(repo.lower(), ref or "", safe_dir)] = index
            self._tree_commit_cache[(repo.lower(), ref or "", safe_dir)] = commit_sha
            self._tree_commit_cache[(repo.lower(), commit_sha, safe_dir)] = commit_sha
            self.task_tools.check_control()
            self.db.add_event(self.task_id, "github_tree_indexed", {
                "repo": repo, "path": safe_dir, "ref": ref,
                "commit_sha": commit_sha, "entry_count": len(entries),
            })
            return json.dumps({
                "success": True, "repo": repo, "path": safe_dir,
                "ref": ref, "commit_sha": commit_sha, "entries": entries,
            }, ensure_ascii=False, indent=2)
        except TaskControlSignal:
            raise
        except Exception as exc:
            return json.dumps(self._record_error(
                "tree", exc, context={"repo": str(repo), "path": str(path), "ref": ref},
            ), ensure_ascii=False)

    def _resolve_commit(self, owner: str, name: str, ref: str | None) -> str:
        if ref and _COMMIT_RE.fullmatch(ref):
            return ref
        args = {"owner": owner, "repo": name, "perPage": 1, "fields": ["sha", "html_url", "commit"]}
        if ref:
            args["sha"] = ref
        payload = self._call("list_commits", args)
        commits = payload if isinstance(payload, list) else payload.get("items", []) if isinstance(payload, dict) else []
        if not commits or not commits[0].get("sha"):
            raise GitHubMCPToolError(
                "Unable to resolve the repository ref to a commit SHA.",
                error_type="not_found", code="GITHUB_REF_NOT_FOUND", retryable=False,
            )
        return str(commits[0]["sha"])

    def _cached_file_metadata(self, repo: str, ref: str | None, path: str):
        parent = str(PurePosixPath(path).parent)
        if parent == ".":
            parent = ""
        cache = self._tree_cache.get((repo.lower(), ref or "", parent))
        if cache and path in cache:
            return cache[path]
        return None

    def _preflight_file_size(self, repo: str, path: str, ref: str | None):
        metadata = self._cached_file_metadata(repo, ref, path)
        if metadata is None:
            raise GitHubPolicyError(
                f"file size metadata is not cached for {path}; call github_tree for its parent "
                "directory first. github_read performs no network request for uncached paths."
            )
        if metadata.get("type") not in {None, "file"}:
            raise GitHubPolicyError("only regular text files can be read")
        size = metadata.get("size")
        if size is None:
            raise GitHubPolicyError(
                f"GitHub directory metadata did not include a file size for {path}; refusing to fetch it"
            )
        if int(size) > self.max_file_bytes:
            raise GitHubPolicyError(
                f"file exceeds GITHUB_MCP_MAX_FILE_BYTES ({self.max_file_bytes} bytes)"
            )
        return metadata

    def github_read(self, repo: str, paths: list[str], ref: str | None = None) -> str:
        try:
            blocked = self._gate("github_read")
            if blocked:
                return blocked
            owner, name = validate_repository(repo)
            if not isinstance(paths, list) or not paths:
                raise GitHubPolicyError("paths must be a non-empty list")
            if len(paths) > 5:
                raise GitHubPolicyError("github_read accepts at most five paths")
            safe_paths = [validate_github_path(path) for path in paths]
            if len(set(safe_paths)) != len(safe_paths):
                raise GitHubPolicyError("duplicate paths are not allowed")
            self.task_tools.check_control()
            self.db.add_event(self.task_id, "github_files_read_started", {
                "repo": repo, "paths": safe_paths, "ref": ref,
            })
            metadata_by_path = {}
            commit_candidates = set()
            for path in safe_paths:
                metadata = self._preflight_file_size(repo, path, ref)
                metadata_by_path[path] = metadata
                parent = str(PurePosixPath(path).parent)
                if parent == ".":
                    parent = ""
                cache_key = (repo.lower(), ref or "", parent)
                cached_commit = self._tree_commit_cache.get(cache_key)
                if not cached_commit and ref and _COMMIT_RE.fullmatch(ref):
                    cached_commit = ref
                if not cached_commit:
                    raise GitHubPolicyError(
                        f"commit metadata is not cached for {path}; call github_tree first"
                    )
                commit_candidates.add(cached_commit)
            if len(commit_candidates) != 1:
                raise GitHubPolicyError(
                    "all github_read paths must be indexed at the same immutable commit SHA"
                )
            commit_sha = next(iter(commit_candidates))
            files = []
            for path in safe_paths:
                self.task_tools.check_control()
                metadata = metadata_by_path[path]
                args = {"owner": owner, "repo": name, "path": path, "sha": commit_sha}
                raw = self.client.call_tool("get_file_contents", args)
                payload = _decode_result(raw, parse_text_json=False)
                text = _extract_file_text(payload)
                encoded = text.encode("utf-8")
                if len(encoded) > self.max_file_bytes:
                    raise GitHubPolicyError(
                        f"file exceeds GITHUB_MCP_MAX_FILE_BYTES ({self.max_file_bytes} bytes): {path}"
                    )
                truncated = len(encoded) > self.snapshot_bytes
                if truncated:
                    text = encoded[:self.snapshot_bytes].decode("utf-8", errors="ignore")
                    text += "\n\n[SNAPSHOT TRUNCATED: source exceeds the configured snapshot limit.]\n"
                snapshot_path = f"sources/github/{commit_sha}/{path}.md"
                saved_path = self.artifacts.write_text(snapshot_path, text)
                url = f"https://github.com/{owner}/{name}/blob/{commit_sha}/{quote(path, safe='/')}"
                evidence = self._register_evidence(
                    url=url, title=f"{repo}/{path}", content=text,
                    source_type="github_file",
                    metadata={
                        "repo": repo, "ref": ref or commit_sha, "path": path,
                        "sha": commit_sha, "snapshot_path": saved_path,
                        "truncated": truncated, "original_bytes": len(encoded),
                    },
                )
                files.append({
                    "path": path, "sha": commit_sha, "url": url,
                    "source_id": evidence["source_id"], "saved_to": saved_path,
                    "bytes": len(encoded), "truncated": truncated,
                    "content": text[:self.snapshot_bytes],
                })
                self.db.add_event(self.task_id, "github_file_read_completed", {
                    "repo": repo, "path": path, "sha": commit_sha,
                    "source_id": evidence["source_id"], "saved_to": saved_path,
                    "truncated": truncated,
                })
            self.task_tools.check_control()
            self.db.add_event(self.task_id, "github_files_read_completed", {
                "repo": repo, "ref": ref, "commit_sha": commit_sha,
                "file_count": len(files), "source_ids": [item["source_id"] for item in files],
            })
            return json.dumps({
                "success": True, "repo": repo, "ref": ref,
                "commit_sha": commit_sha, "files": files,
            }, ensure_ascii=False, indent=2)
        except TaskControlSignal:
            raise
        except Exception as exc:
            return json.dumps(self._record_error(
                "files_read", exc, context={"repo": str(repo), "paths": paths, "ref": ref},
            ), ensure_ascii=False)

    def github_releases(self, repo: str, limit: int = 10) -> str:
        try:
            blocked = self._gate("github_releases")
            if blocked:
                return blocked
            owner, name = validate_repository(repo)
            limit = max(1, min(int(limit), 20))
            self.task_tools.check_control()
            payload = self._call("list_releases", {
                "owner": owner, "repo": name, "perPage": limit,
                "fields": ["tag_name", "name", "body", "html_url", "published_at", "prerelease", "draft"],
            })
            self.task_tools.check_control()
            releases = payload if isinstance(payload, list) else payload.get("items", []) if isinstance(payload, dict) else []
            releases = releases[:limit]
            evidence = self._register_evidence(
                url=f"https://github.com/{owner}/{name}/releases",
                title=f"GitHub releases for {repo}",
                content=json.dumps(releases, ensure_ascii=False, indent=2),
                source_type="github_repo",
                metadata={"repo": repo, "kind": "releases", "limit": limit},
            )
            self.db.add_event(self.task_id, "github_releases_listed", {
                "repo": repo, "release_count": len(releases), "source_id": evidence["source_id"],
            })
            return json.dumps({
                "success": True, "repo": repo, "releases": releases,
                "source_id": evidence["source_id"],
            }, ensure_ascii=False, indent=2)
        except TaskControlSignal:
            raise
        except Exception as exc:
            return json.dumps(self._record_error(
                "releases", exc, context={"repo": str(repo)},
            ), ensure_ascii=False)

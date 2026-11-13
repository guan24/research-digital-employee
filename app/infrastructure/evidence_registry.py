from __future__ import annotations

import hashlib
import json
import threading
from weakref import WeakValueDictionary
from datetime import datetime, timezone
from typing import Any

from app.infrastructure.artifact_store import ArtifactStore


class EvidenceRegistry:
    """Append-only task evidence ledger with content-addressed snapshots.

    Tool calls may run concurrently in LangGraph. Serialize each task's
    read-modify-write cycle so parallel findings or source registrations cannot
    overwrite one another. Weak values avoid retaining locks after a task ends.
    """

    REGISTRY_PATH = "evidence/registry.json"
    _lock_guard = threading.Lock()
    _workspace_locks = WeakValueDictionary()

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts
        key = str(artifacts.workspace.resolve())
        with self._lock_guard:
            lock = self._workspace_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._workspace_locks[key] = lock
            self._lock = lock

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def load(self) -> dict[str, Any]:
        if not self.artifacts.exists(self.REGISTRY_PATH):
            return {"schema_version": 1, "sources": [], "findings": []}
        try:
            registry = json.loads(self.artifacts.read_text(self.REGISTRY_PATH))
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("evidence registry is corrupted") from exc
        if not isinstance(registry, dict):
            raise RuntimeError("evidence registry must be a JSON object")
        registry.setdefault("sources", [])
        registry.setdefault("findings", [])
        return registry

    def _save(self, registry: dict[str, Any]) -> None:
        self.artifacts.write_text(
            self.REGISTRY_PATH, json.dumps(registry, ensure_ascii=False, indent=2)
        )

    def register_source(self, *, url: str, title: str, content: str,
                        source_type: str = "webpage", verified: bool = True,
                        metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            raise ValueError("source URL cannot be empty")
        body = str(content or "")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._lock:
            registry = self.load()
            for item in registry["sources"]:
                if item.get("url") == normalized_url and item.get("content_hash") == digest:
                    return item
            source_id = f"S{len(registry['sources']) + 1:04d}"
            snapshot_path = f"evidence/sources/{source_id}.md"
            retrieved_at = self._now()
            snapshot = (
                f"# {str(title or normalized_url).strip()}\\n\\n"
                f"- Source ID: {source_id}\\n- URL: {normalized_url}\\n"
                f"- Retrieved at: {retrieved_at}\\n- Source type: {source_type}\\n"
                f"- Content SHA-256: {digest}\\n\\n{body}\\n"
            )
            self.artifacts.write_text(snapshot_path, snapshot)
            source = {
                "source_id": source_id, "url": normalized_url,
                "title": str(title or normalized_url).strip()[:500],
                "source_type": source_type, "retrieved_at": retrieved_at,
                "content_hash": digest, "snapshot_path": snapshot_path,
                "verified": bool(verified), "metadata": metadata or {},
            }
            registry["sources"].append(source)
            self._save(registry)
            return source

    def read_snapshot(self, source_id: str) -> str:
        """Read a registered snapshot and verify the content-addressed body."""
        normalized_id = str(source_id or "").strip().upper()
        if not normalized_id:
            raise ValueError("source_id cannot be empty")
        registry = self.load()
        source = next(
            (item for item in registry["sources"]
             if str(item.get("source_id") or "").upper() == normalized_id),
            None,
        )
        if source is None:
            raise KeyError(normalized_id)
        path = str(source.get("snapshot_path") or "").strip()
        if not path or not path.startswith("evidence/sources/") or ".." in path.split("/"):
            raise RuntimeError("registered source has an invalid snapshot path")
        snapshot = self.artifacts.read_text(path)
        expected_hash = str(source.get("content_hash") or "").strip()
        source_marker = f"- Source ID: {normalized_id}\n"
        url_marker = f"- URL: {source.get('url', '')}\n"
        hash_marker = f"- Content SHA-256: {expected_hash}\n\n"
        if not expected_hash or source_marker not in snapshot or url_marker not in snapshot or hash_marker not in snapshot:
            raise RuntimeError("evidence snapshot metadata does not match the registry")
        body = snapshot.split(hash_marker, 1)[1]
        if body.endswith("\n"):
            body = body[:-1]
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != expected_hash:
            raise RuntimeError("evidence snapshot content hash mismatch")
        return snapshot

    def record_finding(self, *, claim: str, source_ids: list[str],
                       rationale: str = "", confidence: str = "medium",
                       dimension_id: str | None = None) -> dict[str, Any]:
        normalized_claim = str(claim or "").strip()
        ids = list(dict.fromkeys(str(value).strip().upper() for value in source_ids if str(value).strip()))
        if not normalized_claim:
            raise ValueError("finding claim cannot be empty")
        if not ids:
            raise ValueError("at least one source_id is required")
        normalized_dimension = str(dimension_id).strip().lower() if dimension_id else None
        with self._lock:
            registry = self.load()
            known = {item["source_id"] for item in registry["sources"]}
            missing = [value for value in ids if value not in known]
            if missing:
                raise ValueError("unknown source_id(s): " + ", ".join(missing))
            for finding in registry["findings"]:
                if (finding.get("claim") == normalized_claim and finding.get("source_ids") == ids
                        and finding.get("dimension_id") == normalized_dimension):
                    return finding
            finding = {
                "finding_id": f"F{len(registry['findings']) + 1:04d}",
                "claim": normalized_claim[:2000], "source_ids": ids,
                "dimension_id": normalized_dimension[:64] if normalized_dimension else None,
                "rationale": str(rationale or "")[:3000],
                "confidence": confidence if confidence in {"low", "medium", "high"} else "medium",
                "recorded_at": self._now(),
            }
            registry["findings"].append(finding)
            self._save(registry)
            return finding

    def summary(self) -> dict[str, Any]:
        registry = self.load()
        return {
            "registry_path": self.REGISTRY_PATH,
            "source_count": len(registry["sources"]),
            "verified_source_count": sum(bool(item.get("verified")) for item in registry["sources"]),
            "finding_count": len(registry["findings"]),
            "sources": registry["sources"],
            "findings": registry["findings"],
        }

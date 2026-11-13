from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.infrastructure.artifact_store import ArtifactStore


class EvidenceRegistry:
    """Append-only task evidence ledger with content-addressed snapshots."""

    REGISTRY_PATH = "evidence/registry.json"

    def __init__(self, artifacts: ArtifactStore):
        self.artifacts = artifacts

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
        registry = self.load()
        for item in registry["sources"]:
            if item.get("url") == normalized_url and item.get("content_hash") == digest:
                return item
        source_id = f"S{len(registry['sources']) + 1:04d}"
        snapshot_path = f"evidence/sources/{source_id}.md"
        retrieved_at = self._now()
        snapshot = (
            f"# {str(title or normalized_url).strip()}\n\n"
            f"- Source ID: {source_id}\n- URL: {normalized_url}\n"
            f"- Retrieved at: {retrieved_at}\n- Source type: {source_type}\n"
            f"- Content SHA-256: {digest}\n\n{body}\n"
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

    def record_finding(self, *, claim: str, source_ids: list[str],
                       rationale: str = "", confidence: str = "medium",
                       dimension_id: str | None = None) -> dict[str, Any]:
        normalized_claim = str(claim or "").strip()
        ids = list(dict.fromkeys(str(value).strip().upper() for value in source_ids if str(value).strip()))
        if not normalized_claim:
            raise ValueError("finding claim cannot be empty")
        if not ids:
            raise ValueError("at least one source_id is required")
        registry = self.load()
        known = {item["source_id"] for item in registry["sources"]}
        missing = [value for value in ids if value not in known]
        if missing:
            raise ValueError("unknown source_id(s): " + ", ".join(missing))
        for finding in registry["findings"]:
            if (finding.get("claim") == normalized_claim and finding.get("source_ids") == ids
                    and finding.get("dimension_id") == (str(dimension_id).strip().lower() if dimension_id else None)):
                return finding
        finding = {
            "finding_id": f"F{len(registry['findings']) + 1:04d}",
            "claim": normalized_claim[:2000], "source_ids": ids,
            "dimension_id": str(dimension_id).strip().lower()[:64] if dimension_id else None,
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

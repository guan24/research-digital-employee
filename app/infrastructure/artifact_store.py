from __future__ import annotations

import os
import tempfile
from pathlib import Path


class ArtifactError(ValueError):
    pass


class ArtifactStore:
    """Task-scoped artifact storage. All agent-visible paths are relative to one task workspace."""

    ALLOWED_SUFFIXES = {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}

    def __init__(self, root_dir: Path, task_id: str, max_file_bytes: int = 1_000_000):
        self.root_dir = root_dir.resolve()
        self.task_id = task_id
        self.workspace = (self.root_dir / task_id).resolve()
        self.max_file_bytes = max_file_bytes
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative_path: str) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or not relative_path.strip():
            raise ArtifactError("artifact path must be a non-empty relative path")
        if any(part in {"..", "~"} for part in raw.parts):
            raise ArtifactError("path traversal is not allowed")
        target = (self.workspace / raw).resolve()
        if target != self.workspace and self.workspace not in target.parents:
            raise ArtifactError("artifact path escapes task workspace")
        if target.suffix.lower() not in self.ALLOWED_SUFFIXES:
            raise ArtifactError(f"unsupported artifact extension: {target.suffix}")
        return target

    def write_text(self, relative_path: str, content: str) -> str:
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ArtifactError(f"artifact exceeds {self.max_file_bytes} bytes")
        target = self._resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return str(target.relative_to(self.root_dir))

    def read_text(self, relative_path: str) -> str:
        target = self._resolve(relative_path)
        if not target.is_file():
            raise FileNotFoundError(relative_path)
        if target.stat().st_size > self.max_file_bytes:
            raise ArtifactError("artifact is too large to read through the agent tool")
        return target.read_text(encoding="utf-8")

    def exists(self, relative_path: str) -> bool:
        try:
            return self._resolve(relative_path).is_file()
        except ArtifactError:
            return False

    def list_files(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for path in sorted(self.workspace.rglob("*")):
            if path.is_file() and path.suffix.lower() in self.ALLOWED_SUFFIXES:
                results.append(
                    {
                        "path": path.relative_to(self.workspace).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "modified_at": path.stat().st_mtime,
                    }
                )
        return results

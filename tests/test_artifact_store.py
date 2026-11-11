from __future__ import annotations

import pytest

from app.infrastructure.artifact_store import ArtifactError, ArtifactStore


def test_artifact_write_read_and_listing(tmp_path):
    store = ArtifactStore(tmp_path, "task-123")
    saved = store.write_text("notes/plan.md", "# Plan\nSearch and compare.")
    assert saved.endswith("task-123/notes/plan.md")
    assert store.read_text("notes/plan.md") == "# Plan\nSearch and compare."
    assert store.list_files()[0]["path"] == "notes/plan.md"


def test_artifact_store_rejects_path_traversal(tmp_path):
    store = ArtifactStore(tmp_path, "task-123")
    with pytest.raises(ArtifactError):
        store.write_text("../outside.md", "do not write")
    with pytest.raises(ArtifactError):
        store.write_text("/tmp/outside.md", "do not write")


def test_artifact_store_rejects_unknown_extension(tmp_path):
    store = ArtifactStore(tmp_path, "task-123")
    with pytest.raises(ArtifactError):
        store.write_text("payload.py", "print('bad')")

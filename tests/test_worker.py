from __future__ import annotations

from app.infrastructure.artifact_store import ArtifactStore
from app.runtime.worker import Worker


class FakeExecutor:
    def __init__(self, settings):
        self.settings = settings

    def run(self, task, worker_id=None):
        store = ArtifactStore(self.settings.workspace_dir, task["task_id"])
        store.write_text("report.md", "# Finished\n\nTest report.")
        return str(store.workspace / "report.md")


def test_worker_processes_one_task_and_releases_lease(settings, db):
    task = db.create_task("Research a topic with enough detail")
    worker = Worker(settings, db, executor=FakeExecutor(settings))
    assert worker.run_once() is True
    updated = db.get_task(task["task_id"])
    assert updated["status"] == "completed"
    assert updated["result_path"].endswith("report.md")
    assert db.acquire_worker_lease("another-worker", 10) is True
    db.release_worker_lease("another-worker")


def test_worker_run_once_returns_false_when_queue_empty(settings, db):
    worker = Worker(settings, db, executor=FakeExecutor(settings))
    assert worker.run_once() is False

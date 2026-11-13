from __future__ import annotations

from app.infrastructure.artifact_store import ArtifactStore
from app.runtime.worker import Worker
from app.runtime.control import TaskWaitingForHuman


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


def test_worker_does_not_retry_task_parked_for_human_decision(settings, db):
    class WaitingExecutor:
        def run(self, task, worker_id=None):
            intervention_id = db.set_waiting_for_human(
                task["task_id"], "quality_not_satisfied",
                {"message": "需要人工决策。"}, worker_id=worker_id,
            )
            assert intervention_id
            raise TaskWaitingForHuman("waiting for operator")

    task = db.create_task("Research requiring human approval")
    worker = Worker(settings, db, executor=WaitingExecutor())
    assert worker.run_once() is True
    updated = db.get_task(task["task_id"])
    assert updated["status"] == "waiting_for_human"
    assert db.get_pending_human_intervention(task["task_id"])["status"] == "pending"


def test_worker_run_once_returns_false_when_queue_empty(settings, db):
    worker = Worker(settings, db, executor=FakeExecutor(settings))
    assert worker.run_once() is False


def test_worker_waits_for_expired_single_worker_lease(settings, db):
    task = db.create_task("Research a topic with enough detail")
    assert db.acquire_worker_lease("stale-worker", 0.05) is True
    worker = Worker(settings, db, executor=FakeExecutor(settings))
    assert worker.run_once() is True
    assert db.get_task(task["task_id"])["status"] == "completed"

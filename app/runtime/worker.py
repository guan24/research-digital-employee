from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
import uuid
from pathlib import Path

from app.config import Settings
from app.infrastructure.database import Database
from app.runtime.executor import ResearchExecutor
from app.tools.research_tools import TaskCancelled, TaskDeadlineExceeded, TaskPaused, WorkerLeaseLost

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("research-worker")


class Worker:
    def __init__(self, settings: Settings, db: Database, executor: ResearchExecutor | None = None):
        self.settings = settings
        self.db = db
        self.executor = executor or ResearchExecutor(settings, db)
        self.worker_id = f"worker-{uuid.uuid4().hex[:10]}"
        self._stop = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._current_task_id: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.settings.worker_heartbeat_seconds):
            if not self.db.renew_worker_lease(self.worker_id, self.settings.worker_lease_seconds):
                logger.error("worker lease was lost")
                self._stop.set()
                return
            if self._current_task_id:
                self.db.heartbeat(self._current_task_id, self.worker_id)

    def _execute_claimed(self, task: dict) -> None:
        self._current_task_id = task["task_id"]
        try:
            report_path = self.executor.run(task, worker_id=self.worker_id)
            if not self.db.owns_worker_lease(self.worker_id):
                self.db.add_event(task["task_id"], "worker_lease_lost", {"phase": "before_completion"})
                return
            current = self.db.get_task(task["task_id"])
            if current and current["cancel_requested"]:
                self.db.mark_cancelled(task["task_id"], self.worker_id)
            elif current and current["pause_requested"]:
                self.db.mark_paused(task["task_id"], self.worker_id)
            else:
                relative = str(Path(report_path).resolve().relative_to(self.settings.app_home)) if Path(report_path).resolve().is_relative_to(self.settings.app_home) else report_path
                self.db.mark_completed(task["task_id"], relative, self.worker_id)
        except TaskPaused:
            self.db.mark_paused(task["task_id"], self.worker_id)
        except TaskCancelled:
            self.db.mark_cancelled(task["task_id"], self.worker_id)
        except WorkerLeaseLost as exc:
            self.db.add_event(task["task_id"], "worker_lease_lost", {"error": str(exc)})
        except TaskDeadlineExceeded as exc:
            self.db.schedule_retry(task, f"TaskDeadlineExceeded: {exc}", self._retry_delay(task), self.worker_id)
        except Exception as exc:
            logger.exception("task %s failed", task["task_id"])
            self.db.add_event(task["task_id"], "execution_exception", {"error_type": type(exc).__name__, "error": str(exc)[:3000]})
            self.db.schedule_retry(task, f"{type(exc).__name__}: {exc}", self._retry_delay(task), self.worker_id)
        finally:
            self._current_task_id = None

    def _retry_delay(self, task: dict) -> int:
        attempt = max(1, int(task["attempts"]))
        return min(self.settings.retry_base_seconds * (2 ** (attempt - 1)), self.settings.retry_max_seconds)

    def run_once(self) -> bool:
        if not self.db.acquire_worker_lease(self.worker_id, self.settings.worker_lease_seconds):
            raise RuntimeError("another worker owns the single-worker lease")
        self._heartbeat_stop.clear()
        heartbeat = threading.Thread(target=self._heartbeat_loop, name="worker-heartbeat", daemon=True)
        heartbeat.start()
        try:
            self.db.recover_stale_tasks(self.settings.worker_lease_seconds)
            task = self.db.claim_next_task(self.worker_id)
            if task is None:
                return False
            self._execute_claimed(task)
            return True
        finally:
            self._heartbeat_stop.set()
            heartbeat.join(timeout=2)
            self.db.release_worker_lease(self.worker_id)

    def run_forever(self) -> None:
        if not self.db.acquire_worker_lease(self.worker_id, self.settings.worker_lease_seconds):
            raise RuntimeError("another worker owns the single-worker lease")
        heartbeat = threading.Thread(target=self._heartbeat_loop, name="worker-heartbeat", daemon=True)
        heartbeat.start()
        logger.info("worker started: %s", self.worker_id)
        try:
            while not self._stop.is_set():
                self.db.recover_stale_tasks(self.settings.worker_lease_seconds)
                task = self.db.claim_next_task(self.worker_id)
                if task is None:
                    self._stop.wait(self.settings.worker_poll_seconds)
                    continue
                self._execute_claimed(task)
        finally:
            self._heartbeat_stop.set()
            heartbeat.join(timeout=2)
            self.db.release_worker_lease(self.worker_id)
            logger.info("worker stopped: %s", self.worker_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Digital Employee single worker")
    parser.add_argument("--once", action="store_true", help="process at most one queued task and exit")
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.ensure_directories()
    db = Database(settings.database_path)
    db.init()
    worker = Worker(settings, db)
    signal.signal(signal.SIGINT, lambda *_: worker.stop())
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    if args.once:
        processed = worker.run_once()
        logger.info("processed_task=%s", processed)
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path
from typing import Any, Iterator
from datetime import datetime, timezone


TASK_STATUSES = {"queued", "running", "retry_wait", "paused", "completed", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    max_steps INTEGER NOT NULL DEFAULT 12,
                    max_runtime_seconds INTEGER NOT NULL DEFAULT 3600,
                    next_run_at REAL NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    heartbeat_at REAL,
                    resume_mode INTEGER NOT NULL DEFAULT 0,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    result_path TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(status, next_run_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_heartbeat ON tasks(status, heartbeat_at);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, event_id);
                CREATE TABLE IF NOT EXISTS worker_lease (
                    lock_id INTEGER PRIMARY KEY CHECK(lock_id = 1),
                    owner TEXT,
                    lease_until REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO worker_lease(lock_id, owner, lease_until) VALUES (1, NULL, 0);
                """
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_task(
        self,
        goal: str,
        max_attempts: int = 3,
        max_steps: int = 12,
        max_runtime_seconds: int = 3600,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        now = utc_now()
        with closing(self.connect()) as conn:
            conn.execute(
                """INSERT INTO tasks(
                    task_id, goal, status, created_at, updated_at, max_attempts,
                    max_steps, max_runtime_seconds, metadata_json
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
                (task_id, goal, now, now, max_attempts, max_steps, max_runtime_seconds,
                 json.dumps(metadata or {}, ensure_ascii=False)),
            )
        self.add_event(task_id, "task_created", {"goal": goal, "max_steps": max_steps})
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def add_event(self, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        safe_payload = json.dumps(payload or {}, ensure_ascii=False, default=str)
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO events(task_id, timestamp, event_type, payload_json) VALUES (?, ?, ?, ?)",
                (task_id, utc_now(), event_type, safe_payload),
            )

    def get_events(self, task_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY event_id ASC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def claim_next_task(self, worker_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT * FROM tasks
                   WHERE status IN ('queued', 'retry_wait') AND next_run_at <= ?
                   ORDER BY created_at ASC LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            task_id = row["task_id"]
            conn.execute(
                """UPDATE tasks SET status='running', attempts=attempts+1,
                   worker_id=?, heartbeat_at=?, updated_at=?, started_at=COALESCE(started_at, ?)
                   WHERE task_id=?""",
                (worker_id, now, utc_now(), utc_now(), task_id),
            )
            claimed = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        self.add_event(task_id, "task_started", {"worker_id": worker_id, "attempt": claimed["attempts"]})
        return dict(claimed)

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                "UPDATE tasks SET heartbeat_at=?, updated_at=? WHERE task_id=? AND worker_id=? AND status='running'",
                (time.time(), utc_now(), task_id, worker_id),
            )
        return cur.rowcount == 1

    def recover_stale_tasks(self, lease_seconds: int) -> int:
        cutoff = time.time() - lease_seconds
        now = utc_now()
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE status='running' AND COALESCE(heartbeat_at, 0) < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE tasks SET status='queued', worker_id=NULL, heartbeat_at=NULL,
                       resume_mode=1, next_run_at=0, updated_at=?,
                       last_error=COALESCE(last_error, 'worker heartbeat expired; queued for checkpoint recovery')
                       WHERE task_id=?""",
                    (now, row["task_id"]),
                )
        for row in rows:
            self.add_event(row["task_id"], "worker_recovery_scheduled", {"reason": "stale heartbeat"})
        return len(rows)

    def mark_completed(self, task_id: str, result_path: str, worker_id: str | None = None) -> bool:
        with closing(self.connect()) as conn:
            if worker_id is None:
                cur = conn.execute(
                    """UPDATE tasks SET status='completed', completed_at=?, updated_at=?,
                       worker_id=NULL, heartbeat_at=NULL, result_path=?, last_error=NULL
                       WHERE task_id=?""",
                    (utc_now(), utc_now(), result_path, task_id),
                )
            else:
                cur = conn.execute(
                    """UPDATE tasks SET status='completed', completed_at=?, updated_at=?,
                       worker_id=NULL, heartbeat_at=NULL, result_path=?, last_error=NULL
                       WHERE task_id=? AND worker_id=? AND status='running'""",
                    (utc_now(), utc_now(), result_path, task_id, worker_id),
                )
        if cur.rowcount:
            self.add_event(task_id, "task_completed", {"result_path": result_path})
        else:
            self.add_event(task_id, "stale_worker_update_ignored", {"attempted_status": "completed"})
        return cur.rowcount == 1

    def mark_paused(self, task_id: str, worker_id: str | None = None) -> bool:
        with closing(self.connect()) as conn:
            if worker_id is None:
                cur = conn.execute(
                    "UPDATE tasks SET status='paused', pause_requested=0, worker_id=NULL, heartbeat_at=NULL, updated_at=? WHERE task_id=?",
                    (utc_now(), task_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE tasks SET status='paused', pause_requested=0, worker_id=NULL, heartbeat_at=NULL, updated_at=? WHERE task_id=? AND worker_id=? AND status='running'",
                    (utc_now(), task_id, worker_id),
                )
        if cur.rowcount:
            self.add_event(task_id, "task_paused", {})
        return cur.rowcount == 1

    def mark_cancelled(self, task_id: str, worker_id: str | None = None) -> bool:
        with closing(self.connect()) as conn:
            if worker_id is None:
                cur = conn.execute(
                    "UPDATE tasks SET status='cancelled', cancel_requested=0, worker_id=NULL, heartbeat_at=NULL, updated_at=? WHERE task_id=?",
                    (utc_now(), task_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE tasks SET status='cancelled', cancel_requested=0, worker_id=NULL, heartbeat_at=NULL, updated_at=? WHERE task_id=? AND worker_id=? AND status='running'",
                    (utc_now(), task_id, worker_id),
                )
        if cur.rowcount:
            self.add_event(task_id, "task_cancelled", {})
        return cur.rowcount == 1

    def schedule_retry(self, task: dict[str, Any], error: str, delay_seconds: int, worker_id: str | None = None) -> str:
        terminal = int(task["attempts"]) >= int(task["max_attempts"])
        status = "failed" if terminal else "retry_wait"
        with closing(self.connect()) as conn:
            if worker_id is None:
                cur = conn.execute(
                    """UPDATE tasks SET status=?, next_run_at=?, worker_id=NULL, heartbeat_at=NULL,
                       updated_at=?, last_error=? WHERE task_id=?""",
                    (status, time.time() + delay_seconds, utc_now(), error[:4000], task["task_id"]),
                )
            else:
                cur = conn.execute(
                    """UPDATE tasks SET status=?, next_run_at=?, worker_id=NULL, heartbeat_at=NULL,
                       updated_at=?, last_error=? WHERE task_id=? AND worker_id=? AND status='running'""",
                    (status, time.time() + delay_seconds, utc_now(), error[:4000], task["task_id"], worker_id),
                )
        if not cur.rowcount:
            self.add_event(task["task_id"], "stale_worker_update_ignored", {"attempted_status": status})
            return "lease_lost"
        self.add_event(
            task["task_id"],
            "task_failed" if terminal else "retry_scheduled",
            {"error": error[:2000], "next_status": status, "delay_seconds": delay_seconds,
             "attempt": task["attempts"], "max_attempts": task["max_attempts"]},
        )
        return status

    def request_pause(self, task_id: str) -> bool:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """UPDATE tasks SET pause_requested=1, updated_at=?
                   WHERE task_id=? AND status='running'""",
                (utc_now(), task_id),
            )
            if cur.rowcount == 0:
                cur = conn.execute(
                    "UPDATE tasks SET status='paused', updated_at=? WHERE task_id=? AND status IN ('queued','retry_wait')",
                    (utc_now(), task_id),
                )
        if cur.rowcount:
            self.add_event(task_id, "pause_requested", {})
        return cur.rowcount == 1

    def request_cancel(self, task_id: str) -> bool:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """UPDATE tasks SET cancel_requested=1, updated_at=?
                   WHERE task_id=? AND status='running'""",
                (utc_now(), task_id),
            )
            if cur.rowcount == 0:
                cur = conn.execute(
                    "UPDATE tasks SET status='cancelled', updated_at=? WHERE task_id=? AND status IN ('queued','retry_wait','paused')",
                    (utc_now(), task_id),
                )
        if cur.rowcount:
            self.add_event(task_id, "cancel_requested", {})
        return cur.rowcount == 1

    def resume_task(self, task_id: str) -> bool:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                """UPDATE tasks SET status='queued', resume_mode=1, pause_requested=0,
                   cancel_requested=0, next_run_at=0, updated_at=?
                   WHERE task_id=? AND status='paused'""",
                (utc_now(), task_id),
            )
        if cur.rowcount:
            self.add_event(task_id, "task_resume_requested", {})
        return cur.rowcount == 1

    def get_control_flags(self, task_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT status, pause_requested, cancel_requested FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def acquire_worker_lease(self, worker_id: str, lease_seconds: int) -> bool:
        now = time.time()
        with self.transaction() as conn:
            cur = conn.execute(
                """UPDATE worker_lease SET owner=?, lease_until=?
                   WHERE lock_id=1 AND (owner IS NULL OR owner=? OR lease_until < ?)""",
                (worker_id, now + lease_seconds, worker_id, now),
            )
        return cur.rowcount == 1

    def owns_worker_lease(self, worker_id: str) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT owner, lease_until FROM worker_lease WHERE lock_id=1"
            ).fetchone()
        return bool(row and row["owner"] == worker_id and float(row["lease_until"]) > time.time())

    def renew_worker_lease(self, worker_id: str, lease_seconds: int) -> bool:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                "UPDATE worker_lease SET lease_until=? WHERE lock_id=1 AND owner=?",
                (time.time() + lease_seconds, worker_id),
            )
        return cur.rowcount == 1

    def release_worker_lease(self, worker_id: str) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                "UPDATE worker_lease SET owner=NULL, lease_until=0 WHERE lock_id=1 AND owner=?",
                (worker_id,),
            )

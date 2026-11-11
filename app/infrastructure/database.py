from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path
from typing import Any, Iterator
from datetime import datetime, timezone


TASK_STATUSES = {"queued", "running", "retry_wait", "paused", "completed", "failed", "cancelled", "deleting"}


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
                    agent_type TEXT NOT NULL DEFAULT 'research',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    max_steps INTEGER NOT NULL DEFAULT 12,
                    max_runtime_seconds INTEGER NOT NULL DEFAULT 3600,
                    runtime_seconds_used REAL NOT NULL DEFAULT 0,
                    runtime_last_accounted_at REAL,
                    execution_steps INTEGER NOT NULL DEFAULT 0,
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
            # Additive migrations keep existing local SQLite databases compatible.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            migrations = {
                "agent_type": "ALTER TABLE tasks ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'research'",
                "runtime_seconds_used": "ALTER TABLE tasks ADD COLUMN runtime_seconds_used REAL NOT NULL DEFAULT 0",
                "runtime_last_accounted_at": "ALTER TABLE tasks ADD COLUMN runtime_last_accounted_at REAL",
                "execution_steps": "ALTER TABLE tasks ADD COLUMN execution_steps INTEGER NOT NULL DEFAULT 0",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(statement)

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
        agent_type: str = "research",
    ) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        now = utc_now()
        with closing(self.connect()) as conn:
            conn.execute(
                """INSERT INTO tasks(
                    task_id, goal, agent_type, status, created_at, updated_at, max_attempts,
                    max_steps, max_runtime_seconds, metadata_json
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
                (task_id, goal, agent_type, now, now, max_attempts, max_steps, max_runtime_seconds,
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

    def get_runtime_overview(self) -> dict[str, Any]:
        """Return task counts and the current single-worker lease for the console."""
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM tasks GROUP BY status"
            ).fetchall()
            lease = conn.execute(
                "SELECT owner, lease_until FROM worker_lease WHERE lock_id=1"
            ).fetchone()

        counts = {status: 0 for status in TASK_STATUSES}
        for row in rows:
            counts[str(row["status"])] = int(row["total"])

        now = time.time()
        owner = lease["owner"] if lease else None
        lease_until = float(lease["lease_until"]) if lease else 0.0
        if owner and lease_until > now:
            worker_status = "active"
        elif owner:
            worker_status = "lease_expired"
        else:
            worker_status = "offline"

        return {
            "task_counts": counts,
            "worker": {
                "status": worker_status,
                "worker_id": owner if worker_status == "active" else None,
                "lease_until": lease_until if worker_status == "active" else None,
                "lease_remaining_seconds": (
                    max(0, int(lease_until - now)) if worker_status == "active" else 0
                ),
            },
        }

    def add_event(self, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        from app.infrastructure.redaction import redact_value
        safe_payload = json.dumps(redact_value(payload or {}), ensure_ascii=False, default=str)
        with closing(self.connect()) as conn:
            conn.execute(
                "INSERT INTO events(task_id, timestamp, event_type, payload_json) VALUES (?, ?, ?, ?)",
                (task_id, utc_now(), event_type, safe_payload),
            )

    def get_events_after(self, task_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id=? AND event_id>? ORDER BY event_id ASC LIMIT ?",
                (task_id, after_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

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

    def begin_delete(self, task_id: str) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if row["status"] != "deleting":
                conn.execute(
                    """UPDATE tasks SET status='deleting', cancel_requested=?,
                       worker_id=CASE WHEN status='running' THEN worker_id ELSE NULL END,
                       heartbeat_at=CASE WHEN status='running' THEN heartbeat_at ELSE NULL END,
                       updated_at=? WHERE task_id=?""",
                    (1 if row["status"] == "running" else 0, utc_now(), task_id),
                )
            return dict(conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())

    def abort_delete(self, task_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE tasks SET status=CASE WHEN worker_id IS NULL THEN 'cancelled' ELSE 'running' END,
                   cancel_requested=0, updated_at=? WHERE task_id=? AND status='deleting'""",
                (utc_now(), task_id),
            )

    def delete_task_record(self, task_id: str) -> bool:
        with self.transaction() as conn:
            row = conn.execute("SELECT status, worker_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return False
            if row["status"] != "deleting" or row["worker_id"] is not None:
                raise RuntimeError("worker has not stopped; refusing deletion")
            conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            return True

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
                   worker_id=?, heartbeat_at=?, runtime_last_accounted_at=?, updated_at=?, started_at=COALESCE(started_at, ?)
                   WHERE task_id=?""",
                (worker_id, now, now, utc_now(), utc_now(), task_id),
            )
            claimed = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        self.add_event(task_id, "task_started", {"worker_id": worker_id, "attempt": claimed["attempts"]})
        return dict(claimed)

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT runtime_last_accounted_at, heartbeat_at FROM tasks WHERE task_id=? AND worker_id=? AND status='running'",
                (task_id, worker_id),
            ).fetchone()
            if row is None:
                return False
            accounted_at = row["runtime_last_accounted_at"] or row["heartbeat_at"] or now
            elapsed = max(0.0, now - float(accounted_at))
            cur = conn.execute(
                """UPDATE tasks SET runtime_seconds_used=runtime_seconds_used+?,
                   runtime_last_accounted_at=?, heartbeat_at=?, updated_at=?
                   WHERE task_id=? AND worker_id=? AND status='running'""",
                (elapsed, now, now, utc_now(), task_id, worker_id),
            )
        return cur.rowcount == 1

    def recover_stale_tasks(self, lease_seconds: int) -> int:
        cutoff = time.time() - lease_seconds
        now_epoch = time.time()
        cutoff = now_epoch - lease_seconds
        now = utc_now()
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE status='running' AND COALESCE(heartbeat_at, 0) < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE tasks SET status='queued', worker_id=NULL, heartbeat_at=NULL,
                       runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, resume_mode=1, next_run_at=0, updated_at=?,
                       last_error=COALESCE(last_error, 'worker heartbeat expired; queued for checkpoint recovery')
                       WHERE task_id=?""",
                    (now_epoch, now_epoch, now, row["task_id"]),
                )
        for row in rows:
            self.add_event(row["task_id"], "worker_recovery_scheduled", {"reason": "stale heartbeat"})
        return len(rows)

    def mark_completed(self, task_id: str, result_path: str, worker_id: str | None = None) -> bool:
        now_ts = time.time()
        now_iso = utc_now()
        with closing(self.connect()) as conn:
            if worker_id is None:
                cur = conn.execute(
                    """UPDATE tasks SET status='completed', completed_at=?, updated_at=?,
                       runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, worker_id=NULL, heartbeat_at=NULL,
                       result_path=?, last_error=NULL WHERE task_id=?""",
                    (now_iso, now_iso, now_ts, now_ts, result_path, task_id),
                )
            else:
                cur = conn.execute(
                    """UPDATE tasks SET status='completed', completed_at=?, updated_at=?,
                       runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, worker_id=NULL, heartbeat_at=NULL,
                       result_path=?, last_error=NULL WHERE task_id=? AND worker_id=? AND status='running'""",
                    (now_iso, now_iso, now_ts, now_ts, result_path, task_id, worker_id),
                )
        if cur.rowcount:
            self.add_event(task_id, "task_completed", {"result_path": result_path})
        else:
            self.add_event(task_id, "stale_worker_update_ignored", {"attempted_status": "completed"})
        return cur.rowcount == 1

    def mark_paused(self, task_id: str, worker_id: str | None = None) -> bool:
        now_ts = time.time()
        now_iso = utc_now()
        with closing(self.connect()) as conn:
            if worker_id is None:
                cur = conn.execute(
                    """UPDATE tasks SET status='paused', pause_requested=0,
                       runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, worker_id=NULL, heartbeat_at=NULL, updated_at=? WHERE task_id=?""",
                    (now_ts, now_ts, now_iso, task_id),
                )
            else:
                cur = conn.execute(
                    """UPDATE tasks SET status='paused', pause_requested=0,
                       runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, worker_id=NULL, heartbeat_at=NULL, updated_at=? WHERE task_id=? AND worker_id=? AND status='running'""",
                    (now_ts, now_ts, now_iso, task_id, worker_id),
                )
        if cur.rowcount:
            self.add_event(task_id, "task_paused", {})
        return cur.rowcount == 1

    def mark_cancelled(self, task_id: str, worker_id: str | None = None) -> bool:
        now_ts = time.time()
        now_iso = utc_now()
        with self.transaction() as conn:
            if worker_id is None:
                cur = conn.execute(
                    """UPDATE tasks SET status=CASE WHEN status='deleting' THEN 'deleting' ELSE 'cancelled' END,
                       cancel_requested=0, runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, worker_id=NULL, heartbeat_at=NULL, updated_at=? WHERE task_id=?""",
                    (now_ts, now_ts, now_iso, task_id),
                )
            else:
                cur = conn.execute(
                    """UPDATE tasks SET status=CASE WHEN status='deleting' THEN 'deleting' ELSE 'cancelled' END,
                       cancel_requested=0, runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, worker_id=NULL, heartbeat_at=NULL, updated_at=? WHERE task_id=? AND worker_id=? AND status IN ('running','deleting')""",
                    (now_ts, now_ts, now_iso, task_id, worker_id),
                )
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",
                    (task_id, now_iso, "task_cancelled", "{}"),
                )
        return cur.rowcount == 1

    def schedule_retry(self, task: dict[str, Any], error: str, delay_seconds: int, worker_id: str | None = None) -> str:
        terminal = int(task["attempts"]) >= int(task["max_attempts"])
        status = "failed" if terminal else "retry_wait"
        now_ts = time.time()
        now_iso = utc_now()
        with closing(self.connect()) as conn:
            runtime_sql = "runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)), runtime_last_accounted_at=NULL"
            if worker_id is None:
                cur = conn.execute(
                    f"""UPDATE tasks SET status=?, next_run_at=?, worker_id=NULL, heartbeat_at=NULL,
                       {runtime_sql}, updated_at=?, last_error=? WHERE task_id=?""",
                    (status, now_ts + delay_seconds, now_ts, now_ts, now_iso, error[:4000], task["task_id"]),
                )
            else:
                cur = conn.execute(
                    f"""UPDATE tasks SET status=?, next_run_at=?, worker_id=NULL, heartbeat_at=NULL,
                       {runtime_sql}, updated_at=?, last_error=? WHERE task_id=? AND worker_id=? AND status='running'""",
                    (status, now_ts + delay_seconds, now_ts, now_ts, now_iso, error[:4000], task["task_id"], worker_id),
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

    def consume_execution_step(self, task_id: str, worker_id: str | None = None) -> int | None:
        """Atomically consume one task-wide tool-call step; None means exhausted or ownership changed."""
        with self.transaction() as conn:
            if worker_id is None:
                cur = conn.execute(
                    "UPDATE tasks SET execution_steps=execution_steps+1 WHERE task_id=? AND status='running' AND execution_steps < max_steps",
                    (task_id,),
                )
            else:
                cur = conn.execute(
                    "UPDATE tasks SET execution_steps=execution_steps+1 WHERE task_id=? AND worker_id=? AND status='running' AND execution_steps < max_steps",
                    (task_id, worker_id),
                )
            if cur.rowcount != 1:
                return None
            row = conn.execute("SELECT execution_steps FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return int(row["execution_steps"]) if row else None

    def mark_failed(self, task_id: str, error: str, worker_id: str | None = None) -> bool:
        now_ts = time.time()
        now_iso = utc_now()
        with self.transaction() as conn:
            if worker_id is None:
                cur = conn.execute(
                    """UPDATE tasks SET status='failed', completed_at=?, updated_at=?,
                       runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, worker_id=NULL, heartbeat_at=NULL,
                       last_error=? WHERE task_id=? AND status NOT IN ('completed','cancelled','deleting')""",
                    (now_iso, now_iso, now_ts, now_ts, error[:4000], task_id),
                )
            else:
                cur = conn.execute(
                    """UPDATE tasks SET status='failed', completed_at=?, updated_at=?,
                       runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, worker_id=NULL, heartbeat_at=NULL,
                       last_error=? WHERE task_id=? AND worker_id=? AND status='running'""",
                    (now_iso, now_iso, now_ts, now_ts, error[:4000], task_id, worker_id),
                )
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",
                    (task_id, now_iso, "task_failed", json.dumps({"error": error[:2000], "terminal": True}, ensure_ascii=False)),
                )
        return cur.rowcount == 1

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

    def requeue_failed_task(self, task_id: str, reason: str | None = None) -> dict[str, Any] | None:
        """Atomically requeue a failed task and append its audit event.

        The task identifier, LangGraph checkpoint thread, and task workspace are
        intentionally retained so the executor can resume the existing run.
        """
        normalized_reason = reason.strip() if reason and reason.strip() else None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, attempts FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "failed":
                return {"requeued": False, "current_status": row["status"]}

            previous_attempts = int(row["attempts"])
            now = utc_now()
            cur = conn.execute(
                """UPDATE tasks
                   SET status='queued',
                       attempts=0,
                       next_run_at=0,
                       worker_id=NULL,
                       heartbeat_at=NULL,
                       resume_mode=1,
                       pause_requested=0,
                       cancel_requested=0,
                       updated_at=?,
                       completed_at=NULL,
                       last_error=NULL,
                       result_path=NULL
                   WHERE task_id=? AND status='failed'""",
                (now, task_id),
            )
            if cur.rowcount != 1:
                current = conn.execute(
                    "SELECT status FROM tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                return {
                    "requeued": False,
                    "current_status": current["status"] if current else "missing",
                }

            payload = {
                "reason": normalized_reason,
                "previous_attempts": previous_attempts,
                "attempts_reset_to": 0,
                "checkpoint_reuse_requested": True,
                "workspace_preserved": True,
            }
            conn.execute(
                """INSERT INTO events(task_id, timestamp, event_type, payload_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    task_id,
                    now,
                    "task_manually_requeued",
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()

        return {
            "requeued": True,
            "task": dict(updated),
            "previous_attempts": previous_attempts,
        }

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

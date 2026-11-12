from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.infrastructure.budget_policy import convergence_reserve_tokens


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
                    budget_enabled INTEGER NOT NULL DEFAULT 1,
                    max_total_tokens INTEGER NOT NULL DEFAULT 100000,
                    total_tokens_used INTEGER NOT NULL DEFAULT 0,
                    worker_generation INTEGER NOT NULL DEFAULT 0,
                    error_category TEXT,
                    error_code TEXT,
                    first_error_json TEXT,
                    error_history_json TEXT NOT NULL DEFAULT '[]',
                    reserved_tokens INTEGER NOT NULL DEFAULT 0,
                    budget_state TEXT NOT NULL DEFAULT 'normal',
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
                CREATE TABLE IF NOT EXISTS token_reservations (task_id TEXT NOT NULL, reservation_id TEXT NOT NULL, reserved_tokens INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0, settled INTEGER NOT NULL DEFAULT 0, actual_tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(task_id,reservation_id), FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE);
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
                "budget_enabled": "ALTER TABLE tasks ADD COLUMN budget_enabled INTEGER NOT NULL DEFAULT 1",
                "max_total_tokens": "ALTER TABLE tasks ADD COLUMN max_total_tokens INTEGER NOT NULL DEFAULT 100000",
                "total_tokens_used": "ALTER TABLE tasks ADD COLUMN total_tokens_used INTEGER NOT NULL DEFAULT 0",
                "worker_generation": "ALTER TABLE tasks ADD COLUMN worker_generation INTEGER NOT NULL DEFAULT 0",
                "error_category": "ALTER TABLE tasks ADD COLUMN error_category TEXT",
                "error_code": "ALTER TABLE tasks ADD COLUMN error_code TEXT",
                "first_error_json": "ALTER TABLE tasks ADD COLUMN first_error_json TEXT",
                "error_history_json": "ALTER TABLE tasks ADD COLUMN error_history_json TEXT NOT NULL DEFAULT '[]'",
                "reserved_tokens": "ALTER TABLE tasks ADD COLUMN reserved_tokens INTEGER NOT NULL DEFAULT 0",
                "budget_state": "ALTER TABLE tasks ADD COLUMN budget_state TEXT NOT NULL DEFAULT 'normal'",
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
        budget_enabled: bool = True,
        max_total_tokens: int = 100_000,
    ) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        now = utc_now()
        with closing(self.connect()) as conn:
            conn.execute(
                """INSERT INTO tasks(
                    task_id, goal, agent_type, status, created_at, updated_at, max_attempts,
                    max_steps, max_runtime_seconds, budget_enabled, max_total_tokens, metadata_json
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, goal, agent_type, now, now, max_attempts, max_steps, max_runtime_seconds,
                 int(budget_enabled), max_total_tokens, json.dumps(metadata or {}, ensure_ascii=False)),
            )
        self.add_event(task_id, "task_created", {"goal": goal, "max_steps": max_steps, "budget_enabled": budget_enabled, "max_total_tokens": max_total_tokens})
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
            generation = int(row["worker_generation"] or 0) + 1
            conn.execute(
                """UPDATE tasks SET status='running', attempts=attempts+1,
                   worker_id=?, worker_generation=?, heartbeat_at=?, runtime_last_accounted_at=?, updated_at=?, started_at=COALESCE(started_at, ?)
                   WHERE task_id=?""",
                (worker_id, generation, now, now, utc_now(), utc_now(), task_id),
            )
            claimed = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        self.add_event(task_id, "task_started", {"worker_id": worker_id, "attempt": claimed["attempts"], "generation": claimed["worker_generation"]})
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
                pending = conn.execute(
                    "SELECT COALESCE(SUM(reserved_tokens),0) AS amount FROM token_reservations WHERE task_id=? AND settled=0",
                    (row["task_id"],),
                ).fetchone()["amount"]
                if pending:
                    conn.execute(
                        "UPDATE token_reservations SET settled=1 WHERE task_id=? AND settled=0",
                        (row["task_id"],),
                    )
                conn.execute(
                    """UPDATE tasks SET status='queued', worker_id=NULL, heartbeat_at=NULL,
                       runtime_seconds_used=runtime_seconds_used+MAX(0.0, ?-COALESCE(runtime_last_accounted_at, heartbeat_at, ?)),
                       runtime_last_accounted_at=NULL, reserved_tokens=MAX(0,reserved_tokens-?),
                       resume_mode=1, next_run_at=0, updated_at=?,
                       last_error=COALESCE(last_error, 'worker heartbeat expired; queued for checkpoint recovery')
                       WHERE task_id=?""",
                    (now_epoch, now_epoch, int(pending or 0), now, row["task_id"]),
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
                       result_path=?, last_error=NULL, error_category=NULL, error_code=NULL WHERE task_id=?""",
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
        self.release_pending_token_reservations(task_id)
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
        self.release_pending_token_reservations(task_id)
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
        self.release_pending_token_reservations(task["task_id"])
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
        current = self.get_task(task["task_id"]) or {}
        self.add_event(
            task["task_id"],
            "task_failed" if terminal else "retry_scheduled",
            {"error": error[:2000], "next_status": status, "delay_seconds": delay_seconds,
             "attempt": task["attempts"], "max_attempts": task["max_attempts"],
             "category": current.get("error_category") or "unknown",
             "error_code": current.get("error_code") or "UNKNOWN_EXCEPTION"},
        )
        return status

    def consume_execution_step(self, task_id: str, worker_id: str | None = None) -> int | None:
        """Consume hard tool-call quota atomically before invoking the tool."""
        with self.transaction() as conn:
            sql = "UPDATE tasks SET execution_steps=execution_steps+1 WHERE task_id=? AND status='running' AND (budget_enabled=0 OR execution_steps < max_steps)"
            args = [task_id]
            if worker_id:
                sql += " AND worker_id=?"
                args.append(worker_id)
            cur = conn.execute(sql, tuple(args))
            if cur.rowcount != 1:
                row = conn.execute("SELECT execution_steps,max_steps,worker_generation FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if row:
                    payload = {"dimension":"tool_calls","gate":"before_tool","used":row["execution_steps"],"limit":row["max_steps"],"generation":row["worker_generation"]}
                    conn.execute("INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",(task_id,utc_now(),"budget_call_rejected",json.dumps(payload)))
                return None
            row = conn.execute("SELECT execution_steps FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return int(row["execution_steps"]) if row else None

    def record_token_usage(self, task_id: str, usage: dict[str, Any], worker_id: str | None = None, generation: int | None = None) -> int:
        total = usage.get("total_tokens") or usage.get("totalTokens")
        if total is None:
            total = (usage.get("prompt_tokens") or usage.get("input_tokens") or 0) + (usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        try:
            total = max(0, int(total))
        except (TypeError, ValueError):
            return 0
        if not total:
            return 0
        with self.transaction() as conn:
            sql = "UPDATE tasks SET total_tokens_used=total_tokens_used+? WHERE task_id=? AND status='running'"
            args = [total, task_id]
            if worker_id:
                sql += " AND worker_id=?"
                args.append(worker_id)
            if generation is not None:
                sql += " AND worker_generation=?"
                args.append(generation)
            cur = conn.execute(sql, tuple(args))
        return total if cur.rowcount == 1 else 0

    def record_classified_error(self, task_id: str, error: str, category: str, code: str, retryable: bool, attempt: int) -> dict[str, Any]:
        entry={"category":category,"error_code":code,"retryable":bool(retryable),"attempt":attempt,"timestamp":utc_now(),"summary":str(error)[:2000]}
        with self.transaction() as conn:
            row=conn.execute("SELECT first_error_json,error_history_json FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            history=json.loads(row["error_history_json"] or "[]"); history.append(entry)
            first=row["first_error_json"] or json.dumps(entry,ensure_ascii=False)
            conn.execute("UPDATE tasks SET error_category=?,error_code=?,first_error_json=?,error_history_json=?,last_error=? WHERE task_id=?",(category,code,first,json.dumps(history,ensure_ascii=False),str(error)[:4000],task_id))
        return {"first_error":json.loads(first),"error_history":history}

    def reserve_tokens(
        self,
        task_id: str,
        reservation_id: str,
        estimate: int,
        worker_id: str | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        """Reserve model-call tokens atomically while protecting the convergence pool."""
        amount = max(1, int(estimate))
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return {"allowed": False, "reason": "task_missing"}
            if worker_id and (
                row["worker_id"] != worker_id
                or (generation is not None and row["worker_generation"] != generation)
            ):
                return {"allowed": False, "reason": "stale_generation"}

            existing = conn.execute(
                "SELECT reserved_tokens,settled,generation FROM token_reservations WHERE task_id=? AND reservation_id=?",
                (task_id, reservation_id),
            ).fetchone()
            if existing and generation is not None and int(existing["generation"]) != int(generation):
                return {"allowed": False, "reason": "stale_reservation_generation"}

            limit = max(0, int(row["max_total_tokens"] or 0))
            used = int(row["total_tokens_used"] or 0)
            reserved = int(row["reserved_tokens"] or 0)
            remaining = max(0, limit - used - reserved)
            enabled = bool(row["budget_enabled"])
            reserve = convergence_reserve_tokens(limit) if enabled else 0
            exploration_limit = max(0, limit - reserve)
            projected = used + reserved + amount
            phase = (
                "convergence" if enabled and limit and projected > exploration_limit
                else ("research" if enabled else "unbounded")
            )

            if existing:
                return {
                    "allowed": True,
                    "already_reserved": not bool(existing["settled"]),
                    "already_settled": bool(existing["settled"]),
                    "reserved": 0 if existing["settled"] else int(existing["reserved_tokens"]),
                    "remaining": remaining,
                    "phase": "convergence" if enabled and used + reserved >= exploration_limit else phase,
                    "convergence_reserve_tokens": reserve,
                    "exploration_remaining": max(0, exploration_limit - used - reserved),
                }

            if enabled and amount > remaining:
                payload = {
                    "dimension": "tokens",
                    "gate": "before_model_call",
                    "requested": amount,
                    "used": used,
                    "reserved": reserved,
                    "limit": limit,
                    "remaining": remaining,
                    "convergence_reserve_tokens": reserve,
                    "exploration_limit": exploration_limit,
                    "phase": phase,
                    "generation": int(row["worker_generation"] or 0),
                }
                conn.execute(
                    "INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",
                    (task_id, utc_now(), "budget_call_rejected", json.dumps(payload, ensure_ascii=False)),
                )
                return {"allowed": False, "reason": "token_budget_exhausted", **payload}

            if phase == "convergence":
                exists = conn.execute(
                    "SELECT 1 FROM events WHERE task_id=? AND event_type='budget_convergence_started' LIMIT 1",
                    (task_id,),
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",
                        (
                            task_id, utc_now(), "budget_convergence_started",
                            json.dumps({
                                "dimension": "tokens",
                                "used": used,
                                "reserved": reserved,
                                "requested": amount,
                                "limit": limit,
                                "exploration_limit": exploration_limit,
                                "convergence_reserve_tokens": reserve,
                                "convergence_remaining_before_call": remaining,
                                "policy": "stop exploration; reserve tokens for synthesis, persistence and repair",
                            }, ensure_ascii=False),
                        ),
                    )

            conn.execute(
                "INSERT INTO token_reservations(task_id,reservation_id,reserved_tokens,generation) VALUES(?,?,?,?)",
                (task_id, reservation_id, amount, int(generation or 0)),
            )
            conn.execute(
                "UPDATE tasks SET reserved_tokens=reserved_tokens+? WHERE task_id=?",
                (amount, task_id),
            )
            return {
                "allowed": True,
                "reserved": amount,
                "remaining": max(0, remaining - amount),
                "phase": phase,
                "convergence_reserve_tokens": reserve,
                "exploration_remaining": max(0, exploration_limit - used - reserved - amount),
            }

    def release_pending_token_reservations(self, task_id: str) -> int:
        """Release reservations left by an interrupted attempt; settled calls are untouched."""
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT reservation_id,reserved_tokens FROM token_reservations WHERE task_id=? AND settled=0",
                (task_id,),
            ).fetchall()
            amount = sum(int(item["reserved_tokens"]) for item in rows)
            if not rows:
                return 0
            conn.execute(
                "UPDATE token_reservations SET settled=1 WHERE task_id=? AND settled=0",
                (task_id,),
            )
            conn.execute(
                "UPDATE tasks SET reserved_tokens=MAX(0,reserved_tokens-?) WHERE task_id=?",
                (amount, task_id),
            )
            conn.execute(
                "INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",
                (task_id, utc_now(), "token_reservations_released", json.dumps({
                    "released_tokens": amount, "reservation_count": len(rows),
                    "reason": "attempt_ended_or_worker_recovered",
                }, ensure_ascii=False)),
            )
            return amount

    def check_runtime_budget(self, task_id: str, worker_id: str | None = None,
                             generation: int | None = None) -> dict[str, Any]:
        """Enforce cumulative runtime at every controlled execution boundary."""
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row or row["status"] != "running" or not bool(row["budget_enabled"]):
                return {"exhausted": False}
            if worker_id and row["worker_id"] != worker_id:
                return {"exhausted": False, "stale_worker": True}
            if generation is not None and int(row["worker_generation"] or 0) != int(generation):
                return {"exhausted": False, "stale_generation": True}
            accounted_at = row["runtime_last_accounted_at"] or row["heartbeat_at"] or now
            used = float(row["runtime_seconds_used"] or 0) + max(0.0, now - float(accounted_at))
            limit = int(row["max_runtime_seconds"] or 0)
            if not limit or used < limit:
                return {"exhausted": False, "used": used, "limit": limit}
            payload = {
                "key": "runtime_seconds:hard", "dimension": "runtime_seconds",
                "gate": "controlled_execution_boundary", "used": round(used, 3),
                "limit": limit, "remaining": max(0.0, limit - used),
                "generation": int(row["worker_generation"] or 0),
            }
            exists = conn.execute(
                "SELECT 1 FROM events WHERE task_id=? AND event_type='budget_call_rejected' AND payload_json LIKE ?",
                (task_id, "%runtime_seconds:hard%"),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",
                    (task_id, utc_now(), "budget_call_rejected", json.dumps(payload, ensure_ascii=False)),
                )
            conn.execute(
                "UPDATE tasks SET runtime_seconds_used=?,runtime_last_accounted_at=? WHERE task_id=? AND status='running'",
                (used, now, task_id),
            )
            return {"exhausted": True, "used": used, "limit": limit, "remaining": 0.0, "generation": payload["generation"]}

    def settle_token_reservation(self, task_id: str, reservation_id: str, actual: int, worker_id: str | None = None, generation: int | None = None) -> int:
        """Settle one reservation exactly once using provider-reported usage."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM token_reservations WHERE task_id=? AND reservation_id=?",
                (task_id, reservation_id),
            ).fetchone()
            task = conn.execute(
                "SELECT worker_id,worker_generation FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not row or row["settled"] or not task:
                return 0
            if worker_id and task["worker_id"] != worker_id:
                return 0
            if generation is not None and (
                task["worker_generation"] != generation
                or int(row["generation"] or 0) != int(generation)
            ):
                return 0
            actual = max(0, int(actual))
            reserved = int(row["reserved_tokens"])
            conn.execute(
                "UPDATE token_reservations SET settled=1,actual_tokens=? WHERE task_id=? AND reservation_id=? AND settled=0",
                (actual, task_id, reservation_id),
            )
            conn.execute(
                "UPDATE tasks SET reserved_tokens=MAX(0,reserved_tokens-?),total_tokens_used=total_tokens_used+? WHERE task_id=?",
                (reserved, actual, task_id),
            )
            return actual

    def release_token_reservation(self, task_id: str, reservation_id: str,
                                  worker_id: str | None = None, generation: int | None = None) -> int:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT reserved_tokens,settled,generation FROM token_reservations WHERE task_id=? AND reservation_id=?",
                (task_id, reservation_id),
            ).fetchone()
            task = conn.execute(
                "SELECT worker_id,worker_generation FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not row or row["settled"] or not task:
                return 0
            if worker_id and task["worker_id"] != worker_id:
                return 0
            if generation is not None and (
                int(task["worker_generation"] or 0) != int(generation)
                or int(row["generation"] or 0) != int(generation)
            ):
                return 0
            amount = int(row["reserved_tokens"])
            conn.execute(
                "UPDATE token_reservations SET settled=1 WHERE task_id=? AND reservation_id=? AND settled=0",
                (task_id, reservation_id),
            )
            conn.execute(
                "UPDATE tasks SET reserved_tokens=MAX(0,reserved_tokens-?) WHERE task_id=?",
                (amount, task_id),
            )
            return amount

    def get_budget_status(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            return {"enabled": False, "state": "missing", "phase": "unbounded", "dimensions": {}}

        now = time.time()
        accounted = task.get("runtime_last_accounted_at") or task.get("heartbeat_at") or now
        runtime = float(task.get("runtime_seconds_used") or 0)
        if task.get("status") == "running":
            runtime += max(0.0, now - float(accounted))

        dimensions = {
            "tool_calls": {"used": int(task.get("execution_steps") or 0), "limit": int(task.get("max_steps") or 0)},
            "runtime_seconds": {"used": round(runtime, 2), "limit": int(task.get("max_runtime_seconds") or 0)},
            "tokens": {
                "used": int(task.get("total_tokens_used") or 0),
                "reserved": int(task.get("reserved_tokens") or 0),
                "limit": int(task.get("max_total_tokens") or 0),
            },
        }
        for item in dimensions.values():
            item["remaining"] = max(0, item["limit"] - item["used"] - int(item.get("reserved", 0)))
            item["ratio"] = (
                (item["used"] + int(item.get("reserved", 0))) / item["limit"]
                if item["limit"] else 0
            )

        enabled = bool(task.get("budget_enabled", 1))
        token = dimensions["tokens"]
        token_limit = int(token["limit"])
        token_consumed = int(token["used"]) + int(token["reserved"])
        reserve = convergence_reserve_tokens(token_limit) if enabled else 0
        exploration_limit = max(0, token_limit - reserve)
        convergence_active = bool(enabled and token_limit > 0 and token_consumed >= exploration_limit)
        token["convergence_reserve"] = reserve
        token["exploration_limit"] = exploration_limit
        token["exploration_remaining"] = max(0, exploration_limit - token_consumed)
        token["convergence_remaining"] = max(0, token_limit - token_consumed)

        ratios = {name: item["ratio"] for name, item in dimensions.items()}
        trigger = "tokens" if convergence_active else max(ratios, key=ratios.get)
        peak = max(ratios.values(), default=0)
        if not enabled:
            state, phase = "disabled", "unbounded"
        else:
            phase = "convergence" if convergence_active else "research"
            state = (
                "soft_limit" if peak >= 1
                else ("restricted" if peak >= 0.95
                      else ("warning" if peak >= 0.8 else "normal"))
            )

        instruction = (
            "Convergence reserve active: stop new searches and source reads. Use registered evidence and saved notes; write, read back, validate and deliver the report."
            if phase == "convergence"
            else (
                "Budget warning: stop low-value exploration and prepare synthesis."
                if state == "warning"
                else ("Budget gates are disabled for this task." if not enabled else "Budget normal.")
            )
        )
        return {
            "enabled": enabled,
            "state": state,
            "phase": phase,
            "trigger_dimension": trigger,
            "dimensions": dimensions,
            "convergence": {
                "reserve_tokens": reserve,
                "exploration_limit": exploration_limit,
                "active": convergence_active,
                "remaining_tokens": token["convergence_remaining"],
            },
            "policy": "hard tool/runtime gates; model calls reserve before provider invocation; 30% token pool protected for convergence",
            "instruction": instruction,
        }

    def approve_budget_extension(self, task_id: str, *, reason: str,
                                 additional_steps: int = 0,
                                 additional_runtime_seconds: int = 0,
                                 additional_tokens: int = 0) -> dict[str, Any] | None:
        additions = {
            "steps": max(0, min(int(additional_steps), 100)),
            "runtime_seconds": max(0, min(int(additional_runtime_seconds), 86_400)),
            "tokens": max(0, min(int(additional_tokens), 5_000_000)),
        }
        if not any(additions.values()):
            raise ValueError("at least one positive budget extension is required")
        if not str(reason or "").strip():
            raise ValueError("approval reason is required")
        with self.transaction() as conn:
            row = conn.execute("SELECT status,max_steps,max_runtime_seconds,max_total_tokens FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None
            conn.execute("""UPDATE tasks SET max_steps=max_steps+?, max_runtime_seconds=max_runtime_seconds+?,
                max_total_tokens=max_total_tokens+?, updated_at=? WHERE task_id=?""",
                (additions["steps"], additions["runtime_seconds"], additions["tokens"], utc_now(), task_id))
            updated = conn.execute("SELECT max_steps,max_runtime_seconds,max_total_tokens FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            conn.execute("INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",
                (task_id,utc_now(),"budget_extension_approved",json.dumps({
                    "reason":reason.strip()[:1200],"additions":additions,
                    "new_limits":{"tool_calls":updated["max_steps"],"runtime_seconds":updated["max_runtime_seconds"],"tokens":updated["max_total_tokens"]}
                },ensure_ascii=False)))
        return {"task_id":task_id,"additions":additions,"new_limits":{
            "tool_calls":updated["max_steps"],"runtime_seconds":updated["max_runtime_seconds"],"tokens":updated["max_total_tokens"]}}

    def emit_budget_alerts(self, task_id: str) -> None:
        budget = self.get_budget_status(task_id)
        if not budget.get("enabled"):
            return
        for name, item in budget["dimensions"].items():
            if not item["limit"]:
                continue
            for threshold in (80, 95, 100):
                if item["ratio"] < threshold / 100:
                    continue
                event_type = "budget_warning" if threshold == 80 else ("budget_restricted" if threshold == 95 else "budget_soft_limit_reached")
                key = "budget:" + name + ":" + str(threshold)
                with self.transaction() as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM events WHERE task_id=? AND event_type=? AND payload_json LIKE ?",
                        (task_id, event_type, "%" + key + "%"),
                    ).fetchone()
                    if exists:
                        continue
                    payload = json.dumps({"key": key, "dimension": name, "threshold_percent": threshold,
                        "used": item["used"], "limit": item["limit"], "policy": "soft_limit_agent_autonomy"})
                    conn.execute("INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",
                        (task_id, utc_now(), event_type, payload))

    def mark_failed(self, task_id: str, error: str, worker_id: str | None = None) -> bool:
        self.release_pending_token_reservations(task_id)
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
                detail = conn.execute("SELECT error_category,error_code FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                payload = {"error": error[:2000], "terminal": True, "category": detail["error_category"] if detail else "unknown", "error_code": detail["error_code"] if detail else "UNKNOWN_EXCEPTION"}
                conn.execute("INSERT INTO events(task_id,timestamp,event_type,payload_json) VALUES(?,?,?,?)",(task_id,now_iso,"task_failed",json.dumps(payload,ensure_ascii=False)))
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
                       error_category=NULL,
                       error_code=NULL,
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

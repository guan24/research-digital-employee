from __future__ import annotations

import sqlite3
import time


def test_create_claim_and_event_journal(db):
    task = db.create_task("Research a topic with enough detail", max_steps=8)
    assert task["status"] == "queued"
    claimed = db.claim_next_task("worker-a")
    assert claimed["task_id"] == task["task_id"]
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    events = db.get_events(task["task_id"])
    assert [event["event_type"] for event in events] == ["task_created", "task_started"]


def test_stale_running_task_is_requeued_for_recovery(db):
    task = db.create_task("Research a topic with enough detail")
    claimed = db.claim_next_task("worker-a")
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE tasks SET heartbeat_at=? WHERE task_id=?", (time.time() - 120, task["task_id"]))
    recovered = db.recover_stale_tasks(30)
    assert recovered == 1
    current = db.get_task(task["task_id"])
    assert current["status"] == "queued"
    assert current["resume_mode"] == 1
    assert current["worker_id"] is None
    assert any(event["event_type"] == "worker_recovery_scheduled" for event in db.get_events(task["task_id"]))


def test_single_worker_lease_blocks_second_worker(db):
    assert db.acquire_worker_lease("worker-a", 60) is True
    assert db.acquire_worker_lease("worker-b", 60) is False
    assert db.renew_worker_lease("worker-a", 60) is True
    db.release_worker_lease("worker-a")
    assert db.acquire_worker_lease("worker-b", 60) is True


def test_pause_resume_and_cancel_transitions(db):
    task = db.create_task("Research a topic with enough detail")
    assert db.request_pause(task["task_id"]) is True
    assert db.get_task(task["task_id"])["status"] == "paused"
    assert db.resume_task(task["task_id"]) is True
    assert db.get_task(task["task_id"])["status"] == "queued"
    assert db.request_cancel(task["task_id"]) is True
    assert db.get_task(task["task_id"])["status"] == "cancelled"

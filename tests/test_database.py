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



def test_manual_retry_requeues_failed_task_and_preserves_recovery_state(db):
    task = db.create_task("Research a topic with enough detail", max_attempts=1)
    claimed = db.claim_next_task("worker-a")
    assert db.schedule_retry(
        claimed, "OpenAI API quota exhausted", delay_seconds=0, worker_id="worker-a"
    ) == "failed"

    result = db.requeue_failed_task(task["task_id"], reason="OpenAI API quota replenished")

    assert result is not None
    assert result["requeued"] is True
    assert result["previous_attempts"] == 1
    updated = db.get_task(task["task_id"])
    assert updated["task_id"] == task["task_id"]
    assert updated["status"] == "queued"
    assert updated["attempts"] == 0
    assert updated["max_attempts"] == 1
    assert updated["resume_mode"] == 1
    assert updated["worker_id"] is None
    assert updated["heartbeat_at"] is None
    assert updated["last_error"] is None
    assert updated["result_path"] is None

    retry_event = db.get_events(task["task_id"])[-1]
    assert retry_event["event_type"] == "task_manually_requeued"
    assert retry_event["payload"] == {
        "reason": "OpenAI API quota replenished",
        "previous_attempts": 1,
        "attempts_reset_to": 0,
        "checkpoint_reuse_requested": True,
        "workspace_preserved": True,
    }


def test_manual_retry_rejects_non_failed_task_without_mutation(db):
    task = db.create_task("Research a topic with enough detail")
    before_events = db.get_events(task["task_id"])
    result = db.requeue_failed_task(task["task_id"], reason="not failed yet")
    assert result == {"requeued": False, "current_status": "queued"}
    assert db.get_task(task["task_id"])["status"] == "queued"
    assert db.get_events(task["task_id"]) == before_events


def test_manual_retry_returns_none_for_missing_task(db):
    assert db.requeue_failed_task("missing-task-id", reason="retry") is None


def test_manual_retry_is_idempotently_rejected_after_first_requeue(db):
    task = db.create_task("Research a topic with enough detail", max_attempts=1)
    claimed = db.claim_next_task("worker-a")
    assert db.schedule_retry(claimed, "quota exhausted", 0, "worker-a") == "failed"
    first = db.requeue_failed_task(task["task_id"], reason="quota replenished")
    second = db.requeue_failed_task(task["task_id"], reason="duplicate request")
    assert first is not None and first["requeued"] is True
    assert second == {"requeued": False, "current_status": "queued"}
    assert sum(
        event["event_type"] == "task_manually_requeued"
        for event in db.get_events(task["task_id"])
    ) == 1


def test_runtime_budget_is_accounted_across_heartbeats_and_retries(db):
    task = db.create_task("Research a topic with enough detail", max_runtime_seconds=60)
    claimed = db.claim_next_task("worker-a")
    with sqlite3.connect(db.path) as conn:
        conn.execute(
            "UPDATE tasks SET runtime_last_accounted_at=?, heartbeat_at=? WHERE task_id=?",
            (time.time() - 7, time.time() - 7, task["task_id"]),
        )
    assert db.heartbeat(task["task_id"], "worker-a") is True
    current = db.get_task(task["task_id"])
    assert 6.0 <= current["runtime_seconds_used"] < 10.0
    assert db.schedule_retry(current, "temporary model error", 0, "worker-a") == "retry_wait"
    after_retry = db.get_task(task["task_id"])
    assert after_retry["runtime_seconds_used"] >= current["runtime_seconds_used"]
    assert after_retry["runtime_last_accounted_at"] is None


def test_tool_call_budget_is_a_soft_convergence_signal(db):
    task = db.create_task("Research a topic with enough detail", max_steps=2)
    db.claim_next_task("worker-a")
    assert db.consume_execution_step(task["task_id"], "worker-a") == 1
    assert db.consume_execution_step(task["task_id"], "worker-a") == 2
    assert db.consume_execution_step(task["task_id"], "worker-a") == 3
    assert db.get_task(task["task_id"])["execution_steps"] == 3
    assert db.get_budget_status(task["task_id"])["state"] == "exhausted"


def test_agent_type_and_step_counter_are_persisted(db):
    task = db.create_task("Research a topic with enough detail", agent_type="research", max_steps=1)
    assert task["agent_type"] == "research"
    claimed = db.claim_next_task("worker-a")
    assert db.consume_execution_step(task["task_id"], "worker-a") == 1
    assert db.consume_execution_step(task["task_id"], "worker-a") == 2
    assert db.get_task(task["task_id"])["execution_steps"] == 2


def test_token_budget_and_warning_alerts_are_persisted(db):
    task = db.create_task("Research a sufficiently detailed topic", max_total_tokens=1000)
    claimed = db.claim_next_task("worker-a")
    assert db.record_token_usage(task["task_id"], {"input_tokens": 700, "output_tokens": 150}, "worker-a", claimed["worker_generation"]) == 850
    status = db.get_budget_status(task["task_id"])
    assert status["dimensions"]["tokens"]["used"] == 850
    assert status["state"] == "warning"
    db.emit_budget_alerts(task["task_id"])
    db.emit_budget_alerts(task["task_id"])
    alerts = [e for e in db.get_events(task["task_id"]) if e["event_type"] == "budget_warning"]
    assert len(alerts) == 1


def test_worker_generation_increments_on_reclaim(db):
    task = db.create_task("Research a sufficiently detailed topic")
    first = db.claim_next_task("worker-a")
    assert first["worker_generation"] == 1
    db.schedule_retry(first, "transient", 0, "worker-a")
    second = db.claim_next_task("worker-b")
    assert second["worker_generation"] == 2

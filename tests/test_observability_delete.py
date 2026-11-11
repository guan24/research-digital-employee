from __future__ import annotations

import json
import sqlite3

from app.infrastructure.redaction import redact_value


def test_chinese_goal_round_trips_through_sqlite(db):
    goal = "调研当前主流 Coding Agent 的技术架构，比较任务规划、工具使用和上下文管理，并生成 Markdown 报告。"
    task = db.create_task(goal)
    assert db.get_task(task["task_id"])["goal"] == goal
    assert db.list_tasks()[0]["goal"] == goal


def test_delete_fence_blocks_worker_claim_and_cascades_events(db):
    task = db.create_task("Research a sufficiently detailed topic")
    task_id = task["task_id"]
    result = db.begin_delete(task_id)
    assert result["status"] == "deleting"
    assert result["worker_id"] is None
    assert db.claim_next_task("worker-after-delete") is None
    assert db.delete_task_record(task_id) is True
    assert db.get_task(task_id) is None
    assert db.get_events(task_id) == []


def test_running_delete_waits_for_worker_confirmation_and_can_be_aborted(db):
    task = db.create_task("Research a sufficiently detailed topic")
    claimed = db.claim_next_task("worker-active")
    result = db.begin_delete(task["task_id"])
    assert result["status"] == "deleting"
    assert result["worker_id"] == "worker-active"
    try:
        db.delete_task_record(task["task_id"])
        assert False, "deletion must be fenced while worker is active"
    except RuntimeError:
        pass
    db.abort_delete(task["task_id"])
    restored = db.get_task(task["task_id"])
    assert restored["status"] == "running"
    assert restored["worker_id"] == "worker-active"


def test_event_journal_replays_after_event_id(db):
    task = db.create_task("Research a sufficiently detailed topic")
    db.add_event(task["task_id"], "agent_plan_started", {"plan": ["search", "read", "synthesize"]})
    db.add_event(task["task_id"], "tool_call_completed", {"tool": "web_search", "output": "evidence"})
    all_events = db.get_events(task["task_id"])
    cursor = all_events[0]["event_id"]
    replay = db.get_events_after(task["task_id"], cursor)
    assert [e["event_id"] for e in replay] == [e["event_id"] for e in all_events[1:]]


def test_credentials_are_redacted_but_research_text_is_preserved():
    value = {
        "goal": "研究 LangGraph 的 checkpoint 设计",
        "api_key": "sk-test-credential-value",
        "authorization": "Bearer very-secret-token",
        "output": "The report compares agents and their planning approaches.",
    }
    cleaned = redact_value(value)
    assert cleaned["goal"] == value["goal"]
    assert cleaned["output"] == value["output"]
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["authorization"] == "[REDACTED]"

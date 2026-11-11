from __future__ import annotations

import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from app.api.main import app


def test_task_api_create_status_and_events(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            json={"goal": "Research the architecture of autonomous coding agents", "max_steps": 8},
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        status = client.get(f"/tasks/{task_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "queued"
        events = client.get(f"/tasks/{task_id}/events")
        assert events.status_code == 200
        assert events.json()[0]["event_type"] == "task_created"


def test_task_detail_exposes_execution_instance_runtime_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={"goal": "Research a sufficiently detailed task instance"})
        task_id = created.json()["task_id"]
        db = client.app.state.db
        with db.transaction() as conn:
            conn.execute(
                """UPDATE tasks SET worker_id=?, heartbeat_at=?, next_run_at=?,
                   resume_mode=1, pause_requested=1, cancel_requested=0 WHERE task_id=?""",
                ("worker-instance-test", 1790000000.0, 1790000100.0, task_id),
            )
        payload = client.get(f"/tasks/{task_id}").json()
        assert payload["worker_id"] == "worker-instance-test"
        assert payload["heartbeat_at"] == 1790000000.0
        assert payload["next_run_at"] == 1790000100.0
        assert payload["resume_mode"] is True
        assert payload["pause_requested"] is True
        assert payload["cancel_requested"] is False


def test_task_api_rejects_short_goal(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        response = client.post("/tasks", json={"goal": "short"})
        assert response.status_code == 422



def test_task_api_manually_retries_failed_task_and_records_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))

    with TestClient(app) as client:
        created = client.post(
            "/tasks",
            json={
                "goal": "Research the architecture of autonomous coding agents",
                "max_attempts": 1,
            },
        )
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        db = client.app.state.db
        claimed = db.claim_next_task("test-worker")
        assert db.schedule_retry(
            claimed, "OpenAI API quota exhausted", 0, "test-worker"
        ) == "failed"

        retried = client.post(
            f"/tasks/{task_id}/retry",
            json={"reason": "OpenAI API quota replenished"},
        )
        assert retried.status_code == 202
        assert retried.json() == {
            "task_id": task_id,
            "status": "queued",
            "checkpoint_reuse_requested": True,
            "workspace_preserved": True,
            "previous_attempts": 1,
            "message": (
                "Task requeued. The Worker will reuse the existing LangGraph checkpoint "
                "when one is available."
            ),
        }

        current = client.get(f"/tasks/{task_id}")
        assert current.json()["status"] == "queued"
        events = client.get(f"/tasks/{task_id}/events").json()
        assert events[-1]["event_type"] == "task_manually_requeued"
        assert events[-1]["payload"]["reason"] == "OpenAI API quota replenished"

        duplicate = client.post(
            f"/tasks/{task_id}/retry",
            json={"reason": "duplicate request"},
        )
        assert duplicate.status_code == 409


def test_task_api_retry_returns_404_for_missing_task(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        response = client.post(
            "/tasks/does-not-exist/retry",
            json={"reason": "retry"},
        )
        assert response.status_code == 404


def test_task_api_retry_rejects_non_failed_task(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post(
            "/tasks",
            json={"goal": "Research the architecture of autonomous coding agents"},
        )
        task_id = created.json()["task_id"]
        response = client.post(
            f"/tasks/{task_id}/retry",
            json={"reason": "task is still queued"},
        )
        assert response.status_code == 409
        assert "current status is 'queued'" in response.json()["detail"]


def test_console_root_serves_frontend_and_static_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "研究任务总览" in page.text
        script = client.get("/static/app.js")
        stylesheet = client.get("/static/style.css")
        assert script.status_code == 200
        assert "checkpoint_resume" in script.text
        assert stylesheet.status_code == 200


def test_delete_running_task_waits_for_worker_stop_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={"goal": "Research a sufficiently detailed topic"})
        task_id = created.json()["task_id"]
        db = client.app.state.db
        claimed = db.claim_next_task("worker-delete-test")
        assert claimed["task_id"] == task_id

        def stop_worker():
            time.sleep(0.15)
            assert db.mark_cancelled(task_id, "worker-delete-test")

        thread = threading.Thread(target=stop_worker)
        thread.start()
        response = client.delete(f"/tasks/{task_id}")
        thread.join(timeout=2)
        assert response.status_code == 200
        assert response.json()["deleted"] is True
        assert db.get_task(task_id) is None


def test_task_api_rejects_unregistered_agent_type(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "goal": "Research the architecture of autonomous coding agents",
            "agent_type": "unknown-role",
        })
        assert response.status_code == 422


def test_api_exposes_agents_budget_evidence_and_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        agents = client.get("/agents")
        assert agents.status_code == 200
        assert "research" in [item["agent_type"] for item in agents.json()["agents"]]
        created = client.post("/tasks", json={
            "goal": "Research a sufficiently detailed autonomous system",
            "budget_enabled": True, "max_total_tokens": 20000
        })
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        budget = client.get(f"/tasks/{task_id}/budget")
        evidence = client.get(f"/tasks/{task_id}/evidence")
        memory = client.get(f"/tasks/{task_id}/memory")
        assert budget.status_code == evidence.status_code == memory.status_code == 200
        assert budget.json()["dimensions"]["tokens"]["limit"] == 20000
        assert evidence.json()["source_count"] == 0
        assert memory.json()["content"] == ""


def test_budget_extension_requires_reason_and_records_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={
            "goal": "Research a sufficiently detailed autonomous system",
            "max_total_tokens": 10000, "max_steps": 10, "max_runtime_seconds": 600
        })
        task_id = created.json()["task_id"]
        response = client.post(f"/tasks/{task_id}/budget/approve", json={
            "reason": "A primary-source validation phase is still necessary",
            "additional_steps": 5, "additional_runtime_seconds": 300,
            "additional_tokens": 20000
        })
        assert response.status_code == 200
        assert response.json()["new_limits"] == {
            "tool_calls": 15, "runtime_seconds": 900, "tokens": 30000
        }
        events = client.get(f"/tasks/{task_id}/events").json()
        assert events[-1]["event_type"] == "budget_extension_approved"


def test_task_creation_uses_one_role_budget_source_and_explicit_values_override(tmp_path, monkeypatch):
    from app.agents.registry import get_agent_registration, load_research_profile

    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    profile = load_research_profile()
    registration = get_agent_registration("research")
    assert registration.budget_defaults.max_steps == profile.max_steps
    assert registration.budget_defaults.max_runtime_seconds == profile.max_runtime_seconds
    assert registration.budget_defaults.max_total_tokens == profile.max_total_tokens

    with TestClient(app) as client:
        defaults = client.post("/tasks", json={
            "goal": "Research the default role budget configuration",
        })
        assert defaults.status_code == 202
        task = defaults.json()
        assert task["max_steps"] == profile.max_steps
        assert task["max_runtime_seconds"] == profile.max_runtime_seconds
        assert task["max_total_tokens"] == profile.max_total_tokens

        overridden = client.post("/tasks", json={
            "goal": "Research the explicitly overridden role budget",
            "max_steps": 7,
            "max_runtime_seconds": 900,
            "max_total_tokens": 12000,
        })
        assert overridden.status_code == 202
        task = overridden.json()
        assert (task["max_steps"], task["max_runtime_seconds"], task["max_total_tokens"]) == (7, 900, 12000)

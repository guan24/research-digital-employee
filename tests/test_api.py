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

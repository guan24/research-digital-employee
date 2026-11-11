from __future__ import annotations

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

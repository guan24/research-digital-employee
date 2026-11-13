from __future__ import annotations

import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from app.api.main import app
from app.infrastructure.artifact_store import ArtifactStore


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


def test_runtime_budget_extension_adds_quota_and_requeues_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={
            "goal": "Research a runtime-budget extension recovery scenario",
            "max_runtime_seconds": 60,
        })
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        db = client.app.state.db
        claimed = db.claim_next_task("runtime-extension-worker")
        db.record_classified_error(
            task_id, "cumulative runtime budget exhausted",
            "control", "RUNTIME_BUDGET_EXHAUSTED", False, claimed["attempts"],
        )
        assert db.mark_failed(task_id, "RUNTIME_BUDGET_EXHAUSTED", "runtime-extension-worker")

        approved = client.post(f"/tasks/{task_id}/budget/approve", json={
            "reason": "Resume after operator-approved runtime extension",
            "additional_runtime_seconds": 600,
        })
        assert approved.status_code == 200
        assert approved.json()["new_limits"]["runtime_seconds"] == 660
        assert approved.json()["resumed_from_checkpoint"] is True
        current = db.get_task(task_id)
        assert current["status"] == "queued"
        assert current["resume_mode"] == 1
        assert current["runtime_seconds_used"] >= 0
        assert current["max_runtime_seconds"] == 660
        events = db.get_events(task_id)
        assert any(event["event_type"] == "budget_checkpoint_resume_requested" for event in events)
        assert any(event["event_type"] == "task_manually_requeued" for event in events)


def test_research_api_exposes_human_choices_and_rejects_unsafe_recovery_action(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={
            "goal": "Research a task whose checkpoint recovery is ambiguous",
        })
        task_id = created.json()["task_id"]
        db = client.app.state.db
        db.claim_next_task("recovery-choice-worker")
        intervention_id = db.set_waiting_for_human(
            task_id, "recovery_conflict",
            {"summary": "Checkpoint state is ambiguous.", "choices": ["terminate"]},
            worker_id="recovery-choice-worker",
        )
        state = client.get(f"/tasks/{task_id}/research")
        assert state.status_code == 200
        assert state.json()["pending_intervention"]["allowed_actions"] == ["terminate"]

        rejected = client.post(f"/tasks/{task_id}/human-intervention/resolve", json={
            "intervention_id": intervention_id, "action": "continue_research",
        })
        assert rejected.status_code == 409
        assert db.get_task(task_id)["status"] == "waiting_for_human"

        terminated = client.post(f"/tasks/{task_id}/human-intervention/resolve", json={
            "intervention_id": intervention_id, "action": "terminate",
            "reason": "Checkpoint 状态无法核验，按保守恢复策略终止。",
        })
        assert terminated.status_code == 200
        current = db.get_task(task_id)
        assert current["status"] == "cancelled"
        assert current["resume_mode"] == 0
        decision = db.list_human_decisions(task_id)
        assert decision[-1]["payload"]["action"] == "terminate"


def test_human_requirements_decision_merges_and_persists_before_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={
            "goal": "Research the architecture and security boundary of a system",
        })
        task_id = created.json()["task_id"]
        db = client.app.state.db
        db.claim_next_task("human-requirements-worker")
        from app.runtime.research_controller import ResearchController
        ResearchController(db, task_id).define_requirements({
            "dimensions": [{
                "id": "architecture", "title": "架构机制",
                "questions": ["系统如何组织执行？"], "expected_evidence": ["官方源码"],
                "priority": "critical", "min_sources": 1,
            }]
        })
        intervention_id = db.set_waiting_for_human(
            task_id, "research_control_requires_human",
            {"message": "请补充研究要求。",
             "choices": ["continue_research", "change_research_direction",
                         "add_research_requirements", "terminate"]},
            worker_id="human-requirements-worker",
        )
        response = client.post(f"/tasks/{task_id}/human-intervention/resolve", json={
            "intervention_id": intervention_id,
            "action": "add_research_requirements",
            "requirements": {"dimensions": [{
                "id": "security", "title": "安全边界",
                "questions": ["系统的安全边界是什么？"],
                "expected_evidence": ["官方安全文档"],
                "priority": "critical", "min_sources": 2,
            }]},
        })
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        state = db.get_research_control_state(task_id)
        assert [item["id"] for item in state["requirements"]["dimensions"]] == ["architecture", "security"]
        decision = db.list_human_decisions(task_id)[-1]
        assert decision["payload"]["action"] == "add_research_requirements"
        assert decision["payload"]["request"]["message"] == "请补充研究要求。"


def test_degraded_delivery_requires_confirmation_and_readback_verified_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={
            "goal": "Research a report that needs explicit degraded-delivery approval",
        })
        task_id = created.json()["task_id"]
        db = client.app.state.db
        db.claim_next_task("degraded-approval-worker")
        store = ArtifactStore(client.app.state.settings.workspace_dir, task_id)
        store.write_text("reports/final.md", "# Draft report\n\nEvidence and limitations.")
        intervention_id = db.set_waiting_for_human(
            task_id, "quality_not_satisfied",
            {"message": "报告未达到质量门槛。", "artifact_path": "reports/final.md",
             "choices": ["continue_repair", "approve_degraded_delivery", "terminate"]},
            worker_id="degraded-approval-worker",
        )
        missing_confirmation = client.post(f"/tasks/{task_id}/human-intervention/resolve", json={
            "intervention_id": intervention_id, "action": "approve_degraded_delivery",
            "reason": "人工已审阅并批准。",
        })
        assert missing_confirmation.status_code == 422
        assert db.get_task(task_id)["status"] == "waiting_for_human"

        approved = client.post(f"/tasks/{task_id}/human-intervention/resolve", json={
            "intervention_id": intervention_id, "action": "approve_degraded_delivery",
            "reason": "人工已审阅主要缺陷及局限，同意降级交付。",
            "confirm": True,
        })
        assert approved.status_code == 200
        current = db.get_task(task_id)
        assert current["status"] == "completed"
        assert current["result_path"]
        persisted = store.read_text("reports/final.md")
        assert "人工批准的降级交付说明" in persisted
        delivered = [event for event in db.get_events(task_id) if event["event_type"] == "report_delivered"]
        assert delivered[-1]["payload"]["manual_approval"] is True
        assert len(delivered[-1]["payload"]["sha256"]) == 64




def test_task_plan_snapshot_api_returns_current_plan_progress_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={"goal": "Research a sufficiently detailed plan snapshot scenario"})
        task_id = created.json()["task_id"]
        db = client.app.state.db
        plan = {
            "title": "研究计划", "objective": "验证快照 API",
            "steps": [{"id": "read", "title": "阅读官方文档", "phase": "reading"}],
        }
        db.add_event(task_id, "research_plan_updated", {
            "version": 1, "change_type": "initial", "plan": plan,
            "original_plan_text": "initial plan",
        })
        db.add_event(task_id, "research_plan_step_status", {
            "step_id": "read", "step_title": "阅读官方文档",
            "status": "in_progress", "previous_status": "pending",
            "plan_version": 1, "source": "agent", "phase": "reading",
            "note": "正在阅读",
        })
        response = client.get(f"/tasks/{task_id}/plan")
        assert response.status_code == 200
        payload = response.json()
        assert payload["current_version"] == 1
        assert payload["current_plan"]["steps"][0]["status"] == "in_progress"
        assert payload["progress"]["read"]["note"] == "正在阅读"
        assert payload["history"][0]["original_plan_text"] == "initial plan"
        assert payload["progress_counts"]["in_progress"] == 1


def test_evidence_snapshot_api_reads_registered_snapshot_and_rejects_tampering(tmp_path, monkeypatch):
    from app.infrastructure.evidence_registry import EvidenceRegistry

    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={"goal": "Research a sufficiently detailed evidence snapshot scenario"})
        task_id = created.json()["task_id"]
        store = ArtifactStore(tmp_path / "workspaces", task_id)
        source = EvidenceRegistry(store).register_source(
            url="https://example.org/primary",
            title="Primary source",
            content="Snapshot body that must remain content-addressed.",
        )
        response = client.get(f"/tasks/{task_id}/evidence/sources/{source['source_id']}/snapshot")
        assert response.status_code == 200
        assert "Snapshot body that must remain content-addressed." in response.text
        store.write_text(source["snapshot_path"], "# Tampered snapshot\\n")
        tampered = client.get(f"/tasks/{task_id}/evidence/sources/{source['source_id']}/snapshot")
        assert tampered.status_code == 409


def test_evidence_snapshot_api_returns_404_for_unknown_source(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as client:
        created = client.post("/tasks", json={"goal": "Research a sufficiently detailed missing snapshot scenario"})
        task_id = created.json()["task_id"]
        response = client.get(f"/tasks/{task_id}/evidence/sources/S9999/snapshot")
        assert response.status_code == 404

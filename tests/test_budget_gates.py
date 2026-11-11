from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.registry import register_agent
from app.infrastructure.artifact_store import ArtifactStore
from app.runtime.control import TaskDeadlineExceeded, TaskStepLimitExceeded
from app.runtime.errors import BudgetExhausted, ClassifiedTaskError
from app.runtime.executor import ResearchExecutor, TaskEventCallback
from app.runtime.worker import Worker
from app.tools.common import TaskToolService
from app.tools.research_tools import ResearchToolService


def test_model_token_overrun_is_rejected_before_provider_invocation(settings, db):
    task = db.create_task("Exercise the pre-provider token gate", max_total_tokens=1000)
    db.claim_next_task("model-worker")
    callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60)

    class FakeProvider:
        invoked = False

        def invoke(self):
            callback.on_chat_model_start(
                {"name": "fake-chat-model"},
                [[SimpleNamespace(type="human", content="large-input " * 1000)]],
                run_id="token-overrun-call",
            )
            self.invoked = True

    provider = FakeProvider()
    with pytest.raises(BudgetExhausted):
        provider.invoke()

    current = db.get_task(task["task_id"])
    rejected = [
        event for event in db.get_events(task["task_id"])
        if event["event_type"] == "budget_call_rejected"
        and event["payload"].get("dimension") == "tokens"
    ]
    assert provider.invoked is False
    assert current["total_tokens_used"] == 0
    assert current["reserved_tokens"] == 0
    assert len(rejected) == 1
    assert rejected[0]["payload"]["gate"] == "before_model_call"
    assert rejected[0]["payload"]["used"] == 0
    assert rejected[0]["payload"]["limit"] == 1000
    assert rejected[0]["payload"]["remaining"] == 1000


def test_token_reservations_reduce_remaining_and_settle_or_release_once(db):
    task = db.create_task("Exercise token reservations", max_total_tokens=6000)
    db.claim_next_task("reservation-worker")

    first = db.reserve_tokens(task["task_id"], "call-1", 3000)
    second = db.reserve_tokens(task["task_id"], "call-2", 2000)
    budget = db.get_budget_status(task["task_id"])
    assert first["allowed"] is True and second["allowed"] is True
    assert budget["dimensions"]["tokens"]["reserved"] == 5000
    assert budget["dimensions"]["tokens"]["remaining"] == 1000

    assert db.settle_token_reservation(task["task_id"], "call-1", 1200) == 1200
    after_settlement = db.get_budget_status(task["task_id"])
    assert after_settlement["dimensions"]["tokens"]["used"] == 1200
    assert after_settlement["dimensions"]["tokens"]["reserved"] == 2000
    assert after_settlement["dimensions"]["tokens"]["remaining"] == 2800
    assert db.settle_token_reservation(task["task_id"], "call-1", 1200) == 0
    duplicate = db.get_budget_status(task["task_id"])
    assert duplicate["dimensions"]["tokens"]["used"] == 1200
    assert duplicate["dimensions"]["tokens"]["reserved"] == 2000

    assert db.release_token_reservation(task["task_id"], "call-2") == 2000
    assert db.get_task(task["task_id"])["reserved_tokens"] == 0
    assert db.reserve_tokens(task["task_id"], "failed-call", 2500)["allowed"] is True
    assert db.release_token_reservation(task["task_id"], "failed-call") == 2500
    final = db.get_budget_status(task["task_id"])
    assert final["dimensions"]["tokens"]["used"] == 1200
    assert final["dimensions"]["tokens"]["reserved"] == 0
    assert final["dimensions"]["tokens"]["remaining"] == 4800


def test_runtime_budget_terminates_worker_without_retry(settings, db):
    task = db.create_task("Exercise cumulative runtime hard gate", max_runtime_seconds=60)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET runtime_seconds_used=600 WHERE task_id=?",
            (task["task_id"],),
        )

    class BoundaryExecutor:
        def run(self, claimed, worker_id=None):
            callback = TaskEventCallback(
                db, claimed["task_id"], time.monotonic(),
                claimed["max_runtime_seconds"], worker_id,
                claimed.get("worker_generation"),
            )
            callback._check_control()
            raise AssertionError("runtime budget boundary unexpectedly allowed execution")

    worker = Worker(settings, db, executor=BoundaryExecutor())
    assert worker.run_once() is True

    current = db.get_task(task["task_id"])
    events = db.get_events(task["task_id"])
    assert current["status"] == "failed"
    assert current["error_code"] == "RUNTIME_BUDGET_EXHAUSTED"
    assert current["error_category"] == "control"
    assert current["first_error_json"] is not None
    assert json.loads(current["error_history_json"])[0]["error_code"] == "RUNTIME_BUDGET_EXHAUSTED"
    assert any(
        event["event_type"] == "budget_call_rejected"
        and event["payload"].get("dimension") == "runtime_seconds"
        for event in events
    )
    assert not any(event["event_type"] == "retry_scheduled" for event in events)


def test_restricted_state_blocks_exploration_but_keeps_artifact_tools(settings, db):
    task = db.create_task("Exercise restricted convergence tools", max_total_tokens=1000)
    db.claim_next_task("restricted-worker")
    db.record_token_usage(task["task_id"], {"total_tokens": 960}, "restricted-worker", 1)
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(
        settings, db, task["task_id"], artifacts, time.monotonic(),
    )

    blocked = json.loads(service.web_search("must not reach Tavily"))
    assert blocked["error_type"] == "budget_intercept"
    assert blocked["dimension"] == "tokens"
    assert blocked["tool"] == "web_search"
    assert any(event["event_type"] == "budget_tool_rejected" for event in db.get_events(task["task_id"]))

    saved = service.save_artifact("notes/converged.md", "Saved evidence is still writable.")
    assert "notes/converged.md" in saved
    assert artifacts.read_text("notes/converged.md") == "Saved evidence is still writable."


@pytest.mark.parametrize(
    ("category", "code", "retryable", "expected_status", "expected_event"),
    [
        ("terminal", "AUTH_FAILED", False, "failed", "task_failed"),
        ("retryable", "UPSTREAM_TIMEOUT", True, "retry_wait", "retry_scheduled"),
    ],
)
def test_worker_retries_only_retryable_errors(
    settings, db, category, code, retryable, expected_status, expected_event
):
    task = db.create_task("Exercise Worker error policy", max_attempts=3)

    class FailingExecutor:
        def run(self, claimed, worker_id=None):
            raise ClassifiedTaskError("simulated classified failure", category, code, retryable)

    worker = Worker(settings, db, executor=FailingExecutor())
    assert worker.run_once() is True

    current = db.get_task(task["task_id"])
    events = db.get_events(task["task_id"])
    assert current["status"] == expected_status
    assert current["attempts"] == 1
    assert current["error_code"] == code
    assert any(event["event_type"] == expected_event for event in events)
    if retryable:
        assert sum(event["event_type"] == "retry_scheduled" for event in events) == 1
    else:
        assert not any(event["event_type"] == "retry_scheduled" for event in events)


def test_second_role_custom_artifact_validator_and_finalizer_run_end_to_end(settings, db):
    role = "probe_role_" + uuid.uuid4().hex[:8]
    validator_calls = []
    finalizer_calls = []

    class FakeAgent:
        def get_state(self, config):
            return None

        def invoke(self, payload, config):
            return {"messages": [{"role": "assistant", "content": "Custom role output"}]}

    def validator(content, task, artifacts):
        validator_calls.append((content, task["agent_type"], artifacts.exists("deliverables/custom.txt")))
        return False

    def finalizer(_db, task_id, artifacts, outcome, task):
        finalizer_calls.append((task_id, outcome, artifacts.exists("deliverables/custom.txt")))

    register_agent(
        role, lambda *_args: FakeAgent(),
        output_artifact="deliverables/custom.txt",
        validator=validator, max_revisions=0, finalizer=finalizer,
    )
    task = db.create_task("Exercise role-specific delivery contract", agent_type=role)
    executor = ResearchExecutor(
        settings, db, checkpointer_factory=lambda _path: _fake_checkpointer(),
    )
    worker = Worker(settings, db, executor=executor)
    assert worker.run_once() is True

    current = db.get_task(task["task_id"])
    events = db.get_events(task["task_id"])
    assert current["status"] == "failed"
    assert current["error_code"] == "DELIVERY_VALIDATION_EXHAUSTED"
    assert len(validator_calls) == 1
    assert validator_calls[0][1:] == (role, True)
    assert finalizer_calls == [(task["task_id"], "failed", True)]
    assert any(
        event["event_type"] == "delivery_validation_completed"
        and event["payload"]["valid"] is False
        and event["payload"]["artifact_path"] == "deliverables/custom.txt"
        for event in events
    )
    assert not any(event["event_type"] == "retry_scheduled" for event in events)


def _fake_checkpointer():
    from contextlib import nullcontext
    return nullcontext(object())


def test_runtime_has_no_research_package_imports_and_imports_without_fetch_dependencies():
    runtime_dir = Path(__file__).resolve().parents[1] / "app" / "runtime"
    import_pattern = re.compile(r"^\s*(?:from|import)\s+(?:app\.tools\.research_tools|app\.agents\.research)(?:\.|\s|$)")
    hits = []
    for path in runtime_dir.rglob("*.py"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if import_pattern.search(line):
                hits.append(f"{path}:{line_number}:{line.strip()}")
    assert hits == []

    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; sys.modules['httpx']=None; sys.modules['bs4']=None; "
        "from app.runtime.executor import ResearchExecutor, TaskEventCallback; "
        "from app.runtime.worker import Worker; print('minimal-runtime-import-ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "minimal-runtime-import-ok" in completed.stdout

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


def test_callback_settlement_and_provider_error_release_reservations(db):
    success_task = db.create_task("Model callback settlement", max_total_tokens=10000)
    db.claim_next_task("callback-worker")
    success_callback = TaskEventCallback(db, success_task["task_id"], time.monotonic(), 60)
    success_callback.on_chat_model_start(
        {"name": "fake-model"},
        [[SimpleNamespace(type="human", content="short prompt")]],
        run_id="callback-settlement",
    )
    reserved_before = db.get_task(success_task["task_id"])["reserved_tokens"]
    assert reserved_before > 0
    response = SimpleNamespace(
        llm_output={"token_usage": {"total_tokens": 1200}},
        generations=[],
    )
    success_callback.on_llm_end(response, run_id="callback-settlement")
    success_callback.on_llm_end(response, run_id="callback-settlement")
    settled = db.get_task(success_task["task_id"])
    assert settled["total_tokens_used"] == 1200
    assert settled["reserved_tokens"] == 0

    failed_task = db.create_task("Model callback failure release", max_total_tokens=10000)
    db.claim_next_task("callback-error-worker")
    failed_callback = TaskEventCallback(db, failed_task["task_id"], time.monotonic(), 60)
    failed_callback.on_chat_model_start(
        {"name": "fake-model"},
        [[SimpleNamespace(type="human", content="short prompt")]],
        run_id="callback-failure",
    )
    assert db.get_task(failed_task["task_id"])["reserved_tokens"] > 0
    failed_callback.on_llm_error(RuntimeError("simulated provider failure"), run_id="callback-failure")
    released = db.get_task(failed_task["task_id"])
    assert released["reserved_tokens"] == 0
    assert released["total_tokens_used"] == 0


def test_token_budget_exhaustion_fails_worker_without_provider_call_or_retry(settings, db):
    task = db.create_task("Worker token exhaustion", max_total_tokens=1000)
    provider_calls = []

    class ProviderExecutor:
        def run(self, claimed, worker_id=None):
            callback = TaskEventCallback(
                db, claimed["task_id"], time.monotonic(),
                claimed["max_runtime_seconds"], worker_id, claimed["worker_generation"],
            )
            callback.on_chat_model_start(
                {"name": "fake-model"},
                [[SimpleNamespace(type="human", content="large prompt " * 1000)]],
                run_id="worker-token-exhaustion",
            )
            provider_calls.append("called")

    worker = Worker(settings, db, executor=ProviderExecutor())
    assert worker.run_once() is True
    current = db.get_task(task["task_id"])
    events = db.get_events(task["task_id"])
    assert current["status"] == "failed"
    assert current["error_code"] == "TOKEN_BUDGET_EXHAUSTED"
    assert current["total_tokens_used"] == 0
    assert current["reserved_tokens"] == 0
    assert provider_calls == []
    assert not any(event["event_type"] == "retry_scheduled" for event in events)


def test_delivery_validation_two_repairs_exhausts_terminally_without_retry(settings, db):
    role = "two_repair_probe_" + uuid.uuid4().hex[:8]
    validation_calls = []
    repair_calls = []
    finalizer_calls = []

    class FakeAgent:
        def get_state(self, config):
            return None

        def invoke(self, payload, config):
            return {"messages": [{"role": "assistant", "content": "Invalid but materialized report"}]}

    def validator(content, task, artifacts):
        validation_calls.append(content)
        return False

    def postprocessor(agent, result, config, task, artifacts, validation):
        repair_calls.append(len(repair_calls) + 1)
        agent.invoke({"messages": [{"role": "user", "content": "repair based on validation"}]}, config=config)

    def finalizer(_db, task_id, artifacts, outcome, task):
        finalizer_calls.append((task_id, outcome, artifacts.exists("reports/custom.md")))

    register_agent(
        role, lambda *_args: FakeAgent(),
        output_artifact="reports/custom.md",
        validator=validator, postprocessor=postprocessor, max_revisions=2,
        finalizer=finalizer,
    )
    task = db.create_task("Exercise two bounded delivery repairs", agent_type=role)
    worker = Worker(
        settings, db,
        executor=ResearchExecutor(settings, db, checkpointer_factory=lambda _path: _fake_checkpointer()),
    )
    assert worker.run_once() is True
    current = db.get_task(task["task_id"])
    events = db.get_events(task["task_id"])
    assert current["status"] == "failed"
    assert current["error_code"] == "DELIVERY_VALIDATION_EXHAUSTED"
    assert len(validation_calls) == 3
    assert repair_calls == [1, 2]
    assert finalizer_calls == [(task["task_id"], "failed", True)]
    assert sum(event["event_type"] == "delivery_validation_completed" for event in events) == 3
    assert not any(event["event_type"] == "retry_scheduled" for event in events)


def test_stale_generation_cannot_settle_or_release_a_token_reservation(db):
    task = db.create_task("Stale generation reservation guard", max_total_tokens=5000)
    first = db.claim_next_task("old-worker")
    reserved = db.reserve_tokens(
        task["task_id"], "old-generation-call", 2500,
        "old-worker", first["worker_generation"],
    )
    assert reserved["allowed"] is True
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET worker_id=?,worker_generation=? WHERE task_id=?",
            ("new-worker", first["worker_generation"] + 1, task["task_id"]),
        )

    assert db.settle_token_reservation(
        task["task_id"], "old-generation-call", 1200,
        "old-worker", first["worker_generation"],
    ) == 0
    assert db.release_token_reservation(
        task["task_id"], "old-generation-call",
        "old-worker", first["worker_generation"],
    ) == 0
    current = db.get_task(task["task_id"])
    assert current["total_tokens_used"] == 0
    assert current["reserved_tokens"] == 2500

def test_tool_call_budget_enters_convergence_before_hard_step_limit(db, settings):
    task = db.create_task("Protect tool calls for final report delivery", max_steps=30)
    db.claim_next_task("worker-step-reserve")
    for _ in range(23):
        assert db.consume_execution_step(task["task_id"]) is not None

    status = db.get_budget_status(task["task_id"])
    assert status["phase"] == "convergence"
    assert status["trigger_dimension"] == "tool_calls"
    assert status["dimensions"]["tool_calls"]["convergence_reserve"] == 7
    assert status["dimensions"]["tool_calls"]["exploration_remaining"] == 0
    assert status["dimensions"]["tool_calls"]["convergence_remaining"] == 7

    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())
    blocked = json.loads(service.web_search("must be blocked during convergence"))
    assert blocked["error_type"] == "budget_intercept"
    assert any(
        event["event_type"] == "budget_convergence_started"
        for event in db.get_events(task["task_id"])
    )


def test_runtime_budget_enters_convergence_before_hard_timeout(db):
    task = db.create_task("Protect runtime for report validation", max_runtime_seconds=3_600)
    db.claim_next_task("worker-runtime-reserve")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET runtime_seconds_used=2880,runtime_last_accounted_at=? WHERE task_id=?",
            (time.time(), task["task_id"]),
        )

    status = db.get_budget_status(task["task_id"])
    assert status["phase"] == "convergence"
    assert status["trigger_dimension"] == "runtime_seconds"
    assert status["dimensions"]["runtime_seconds"]["convergence_reserve"] == 720
    assert status["dimensions"]["runtime_seconds"]["exploration_remaining"] == 0
    assert any(
        event["event_type"] == "budget_convergence_started"
        for event in db.get_events(task["task_id"])
    )


def test_token_budget_protects_a_convergence_reserve_before_exploration_stops(db, settings):
    task = db.create_task("Exercise the protected report convergence pool", max_total_tokens=100_000, max_steps=30, max_runtime_seconds=3_600)
    db.claim_next_task("convergence-worker")

    research_call = db.reserve_tokens(task["task_id"], "research-call", 69_000)
    convergence_call = db.reserve_tokens(task["task_id"], "convergence-call", 2_000)
    status = db.get_budget_status(task["task_id"])

    assert research_call["allowed"] is True
    assert research_call["phase"] == "research"
    assert convergence_call["allowed"] is True
    assert convergence_call["phase"] == "convergence"
    assert status["phase"] == "convergence"
    assert status["state"] == "warning"
    assert status["phase"] == "convergence"
    assert status["convergence"]["reserve_tokens"] == 30_000
    assert status["convergence"]["reserve_tool_calls"] == 7
    assert status["convergence"]["reserve_runtime_seconds"] == 720
    assert status["dimensions"]["tokens"]["exploration_remaining"] == 0
    assert status["dimensions"]["tokens"]["convergence_remaining"] == 29_000
    assert any(
        event["event_type"] == "budget_convergence_started"
        for event in db.get_events(task["task_id"])
    )

    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    service = ResearchToolService(settings, db, task["task_id"], artifacts, time.monotonic())
    blocked = json.loads(service.web_search("exploration must stop"))
    assert blocked["error_type"] == "budget_intercept"
    assert blocked["phase"] == "convergence"
    assert "report.md" in service.save_artifact("report.md", "# Converged report")

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.runtime.control import TaskDeadlineExceeded
from app.runtime.errors import BudgetExhausted
from app.runtime.executor import ResearchExecutor, TaskEventCallback
from app.runtime.worker import Worker
from app.agents.registry import register_agent


def build_settings(root: Path) -> Settings:
    return Settings(
        app_home=root, database_path=root / "data" / "tasks.sqlite",
        checkpoint_path=root / "data" / "checkpoints.sqlite",
        workspace_dir=root / "workspaces", openai_api_key="probe-key",
        openai_base_url=None, openai_model="probe-model", model_timeout_seconds=5,
        tavily_api_key="", search_timeout_seconds=5, fetch_timeout_seconds=5,
        max_fetch_bytes=100_000, worker_poll_seconds=0.01,
        worker_heartbeat_seconds=0.01, worker_lease_seconds=2,
        retry_base_seconds=1, retry_max_seconds=2, default_max_attempts=3,
        default_max_steps=12, default_max_runtime_seconds=60,
    )


@contextmanager
def fake_checkpointer(_path):
    yield object()


def main():
    with tempfile.TemporaryDirectory(prefix="research-budget-probe-") as tmp:
        root = Path(tmp)
        settings = build_settings(root)
        settings.ensure_directories()
        db = Database(settings.database_path)
        db.init()

        # 1. Token gate: callback must reject before a fake provider is invoked.
        task = db.create_task("Token pre-provider gate probe", max_total_tokens=1000)
        db.claim_next_task("probe-token-worker")
        callback = TaskEventCallback(db, task["task_id"], time.monotonic(), 60)
        provider_invoked = False
        try:
            callback.on_chat_model_start(
                {"name": "probe-model"},
                [[SimpleNamespace(type="human", content="large prompt " * 1000)]],
                run_id="probe-token-overrun",
            )
            provider_invoked = True
        except BudgetExhausted:
            pass
        current = db.get_task(task["task_id"])
        rejects = [
            event for event in db.get_events(task["task_id"])
            if event["event_type"] == "budget_call_rejected"
            and event["payload"].get("dimension") == "tokens"
        ]
        print("TOKEN_PROBE: used=%s reserved=%s rejected=%s provider_invoked=%s" % (
            current["total_tokens_used"], current["reserved_tokens"], bool(rejects), provider_invoked))

        # 2. Runtime gate: 600 seconds already used against a 60-second ceiling.
        runtime_task = db.create_task("Runtime hard gate probe", max_runtime_seconds=60)
        with db.transaction() as conn:
            conn.execute("UPDATE tasks SET runtime_seconds_used=600 WHERE task_id=?", (runtime_task["task_id"],))

        class RuntimeBoundaryExecutor:
            def run(self, claimed, worker_id=None):
                TaskEventCallback(
                    db, claimed["task_id"], time.monotonic(),
                    claimed["max_runtime_seconds"], worker_id, claimed["worker_generation"],
                )._check_control()
                raise AssertionError("runtime gate unexpectedly allowed execution")

        runtime_worker = Worker(settings, db, executor=RuntimeBoundaryExecutor())
        runtime_worker.run_once()
        current = db.get_task(runtime_task["task_id"])
        runtime_events = db.get_events(runtime_task["task_id"])
        print("RUNTIME_PROBE: used=600 limit=60 status=%s code=%s retry_events=%s history=%s" % (
            current["status"], current["error_code"],
            sum(e["event_type"] == "retry_scheduled" for e in runtime_events),
            len(json.loads(current["error_history_json"]))))

        # 3. Settlement: 3000 reserved, 1200 actual, repeated settlement is idempotent.
        settle_task = db.create_task("Token settlement probe", max_total_tokens=5000)
        db.claim_next_task("probe-settle-worker")
        db.reserve_tokens(settle_task["task_id"], "probe-settle-call", 3000)
        first = db.settle_token_reservation(settle_task["task_id"], "probe-settle-call", 1200)
        second = db.settle_token_reservation(settle_task["task_id"], "probe-settle-call", 1200)
        current = db.get_task(settle_task["task_id"])
        print("SETTLEMENT_PROBE: first=%s duplicate=%s used=%s reserved=%s" % (
            first, second, current["total_tokens_used"], current["reserved_tokens"]))

        # 4. Failed-call release.
        release_task = db.create_task("Token failed-call release probe", max_total_tokens=5000)
        db.claim_next_task("probe-release-worker")
        db.reserve_tokens(release_task["task_id"], "probe-failed-call", 2500)
        db.release_token_reservation(release_task["task_id"], "probe-failed-call")
        current = db.get_task(release_task["task_id"])
        print("FAILURE_RELEASE_PROBE: used=%s reserved=%s" % (
            current["total_tokens_used"], current["reserved_tokens"]))

        # 5. Role-owned artifact, validator and finalizer, executed by Worker/Executor.
        role = "probe_role_" + uuid.uuid4().hex[:8]
        validator_called = []
        finalizer_called = []

        class FakeAgent:
            def get_state(self, config):
                return None
            def invoke(self, payload, config):
                return {"messages": [{"role": "assistant", "content": "Custom role deliverable"}]}

        def validator(content, task, artifacts):
            validator_called.append((content, task["agent_type"], artifacts.exists("deliverables/probe.txt")))
            return False

        def finalizer(_db, task_id, artifacts, outcome, task):
            finalizer_called.append((task_id, outcome, artifacts.exists("deliverables/probe.txt")))

        register_agent(
            role, lambda *_args: FakeAgent(),
            output_artifact="deliverables/probe.txt",
            validator=validator, max_revisions=0, finalizer=finalizer,
        )
        role_task = db.create_task("Role contract probe", agent_type=role)
        role_executor = ResearchExecutor(settings, db, checkpointer_factory=fake_checkpointer)
        Worker(settings, db, executor=role_executor).run_once()
        current = db.get_task(role_task["task_id"])
        print("ROLE_PROBE: status=%s code=%s validator_called=%s finalizer_called=%s artifact=%s" % (
            current["status"], current["error_code"], bool(validator_called),
            bool(finalizer_called), ArtifactStore(settings.workspace_dir, role_task["task_id"]).exists("deliverables/probe.txt")))

    with tempfile.TemporaryDirectory(prefix="research-regression-probes-") as regression_tmp:
        root = Path(regression_tmp)
        settings = build_settings(root)
        settings.ensure_directories()
        db = Database(settings.database_path)
        db.init()
        # 6. Regression smoke: hard step gate, restricted exploration and writable artifacts.
        step_task = db.create_task("Tool step hard gate probe", max_steps=2)
        db.claim_next_task("probe-step-worker")
        step_callback = TaskEventCallback(db, step_task["task_id"], time.monotonic(), 3600)
        step_callback.on_tool_start({"name": "probe_tool"}, "{}", run_id="step-1")
        step_callback.on_tool_start({"name": "probe_tool"}, "{}", run_id="step-2")
        step_blocked = False
        try:
            step_callback.on_tool_start({"name": "probe_tool"}, "{}", run_id="step-3")
        except Exception as exc:
            step_blocked = type(exc).__name__ == "TaskStepLimitExceeded"
        step_current = db.get_task(step_task["task_id"])
        print("STEP_REGRESSION_PROBE: used=%s third_call_blocked=%s" % (
            step_current["execution_steps"], step_blocked))
    
        from app.tools.research_tools import ResearchToolService
        restricted_task = db.create_task("Restricted exploration probe", max_total_tokens=1000)
        db.claim_next_task("probe-restricted-worker")
        db.record_token_usage(restricted_task["task_id"], {"total_tokens": 960}, "probe-restricted-worker", 1)
        restricted_store = ArtifactStore(settings.workspace_dir, restricted_task["task_id"])
        restricted_service = ResearchToolService(settings, db, restricted_task["task_id"], restricted_store, time.monotonic())
        search_result = json.loads(restricted_service.web_search("must be blocked"))
        restricted_service.save_artifact("notes/allowed.md", "artifact writes remain allowed")
        print("RESTRICTED_REGRESSION_PROBE: search_blocked=%s artifact_write=%s" % (
            search_result.get("error_type") == "budget_intercept",
            restricted_store.exists("notes/allowed.md")))
    
        # 7. Two bounded delivery repairs still terminate without Worker retry.
        repair_role = "probe_two_repairs_" + uuid.uuid4().hex[:8]
        repair_calls = []
        validator_calls = []
        repair_finalizers = []
    
        class RepairAgent:
            def get_state(self, config):
                return None
            def invoke(self, payload, config):
                return {"messages": [{"role": "assistant", "content": "invalid deliverable"}]}
    
        def always_invalid(content, task, artifacts):
            validator_calls.append(content)
            return False
    
        def repair_once(agent, result, config, task, artifacts, validation):
            repair_calls.append(len(repair_calls) + 1)
            agent.invoke({"messages": [{"role": "user", "content": "repair"}]}, config=config)
    
        def repair_finalizer(_db, task_id, artifacts, outcome, task):
            repair_finalizers.append((task_id, outcome))
    
        register_agent(
            repair_role, lambda *_args: RepairAgent(),
            output_artifact="reports/repair-probe.md", validator=always_invalid,
            postprocessor=repair_once, max_revisions=2, finalizer=repair_finalizer,
        )
        repair_task = db.create_task("Two revision terminal probe", agent_type=repair_role)
        repair_executor = ResearchExecutor(settings, db, checkpointer_factory=fake_checkpointer)
        Worker(settings, db, executor=repair_executor).run_once()
        repair_current = db.get_task(repair_task["task_id"])
        repair_events = db.get_events(repair_task["task_id"])
        print("VALIDATION_REGRESSION_PROBE: status=%s validations=%s repairs=%s retry_events=%s finalizer=%s" % (
            repair_current["status"], len(validator_calls), len(repair_calls),
            sum(e["event_type"] == "retry_scheduled" for e in repair_events),
            bool(repair_finalizers)))
    
        # 8. First root cause remains the first entry after later errors.
        history_task = db.create_task("Root cause history probe")
        db.record_classified_error(history_task["task_id"], "initial auth failure", "terminal", "AUTH_FAILED", False, 1)
        db.record_classified_error(history_task["task_id"], "later timeout", "retryable", "UPSTREAM_TIMEOUT", True, 2)
        history_current = db.get_task(history_task["task_id"])
        history = json.loads(history_current["error_history_json"])
        first = json.loads(history_current["first_error_json"])
        print("ROOT_CAUSE_REGRESSION_PROBE: first=%s history_entries=%s" % (
            first["error_code"], len(history)))
    
    # 9. Static import boundary and minimal-dependency import probe.
    runtime_dir = ROOT / "app" / "runtime"
    pattern = re.compile(r"^\s*(?:from|import)\s+(?:app\.tools\.research_tools|app\.agents\.research)(?:\.|\s|$)")
    hits = [
        f"{path.relative_to(ROOT)}:{line_no}:{line.strip()}"
        for path in runtime_dir.rglob("*.py")
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    print("STATIC_DECOUPLING_PROBE: import_hits=%s" % len(hits))
    code = (
        "import sys; sys.modules['httpx']=None; sys.modules['bs4']=None; "
        "from app.runtime.executor import ResearchExecutor, TaskEventCallback; "
        "from app.runtime.worker import Worker; print('minimal-runtime-import-ok')"
    )
    imported = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    print("MINIMAL_DEPENDENCY_PROBE: success=%s output=%s error=%s" % (
        imported.returncode == 0, imported.stdout.strip(), imported.stderr.strip().splitlines()[-1] if imported.stderr else ""))

    # 10. Documentation claims must match the enforced runtime behavior.
    readme_en = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    docs_ok = (
        "Before each model request" in readme_en
        and "模型调用前" in readme_zh
        and "before provider invocation" in design
        and "soft convergence signals" not in design
    )
    print("DOC_CONSISTENCY_PROBE: consistent=%s" % docs_ok)
    if not docs_ok:
        raise SystemExit("budget documentation contradicts the implemented gates")



if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.infrastructure.database import Database
from app.runtime.executor import TaskEventCallback
from app.agents.registry import get_agent_registration, load_research_profile


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = Settings(app_home=root, database_path=root/"data/tasks.sqlite",
            checkpoint_path=root/"data/checkpoints.sqlite", workspace_dir=root/"workspaces",
            openai_api_key="test", openai_base_url=None, openai_model="test",
            model_timeout_seconds=5, tavily_api_key="", search_timeout_seconds=5,
            fetch_timeout_seconds=5, max_fetch_bytes=100000, worker_poll_seconds=.01,
            worker_heartbeat_seconds=.01, worker_lease_seconds=5, retry_base_seconds=1,
            retry_max_seconds=2, default_max_attempts=3, default_max_steps=12,
            default_max_runtime_seconds=60)
        settings.ensure_directories()
        db = Database(settings.database_path); db.init()
        task = db.create_task("Baseline token gate probe task", max_total_tokens=1000)
        claimed = db.claim_next_task("probe-worker")
        callback = TaskEventCallback(db, task["task_id"], 0, 60)
        callback.on_chat_model_start({}, [[SimpleNamespace(type="human", content="x"*12000)]], run_id="baseline-token-call")
        print("TOKEN_BEFORE_PROVIDER_CALL: allowed=True reserved=%s used=%s" % (
            db.get_task(task["task_id"])["reserved_tokens"], db.get_task(task["task_id"])["total_tokens_used"]))
        callback.on_llm_end(SimpleNamespace(llm_output={"token_usage":{"total_tokens":4000}}, generations=[]), run_id="baseline-token-call")
        t = db.get_task(task["task_id"])
        print("TOKEN_AFTER_PROVIDER_CALL: used=%s reserved=%s budget_events=%s" % (
            t["total_tokens_used"], t["reserved_tokens"], sum(e["event_type"]=="budget_call_rejected" for e in db.get_events(task["task_id"]))))
        runtime_task = db.create_task("Baseline runtime gate probe task", max_runtime_seconds=60)
        db.claim_next_task("probe-worker-2")
        with db.transaction() as conn:
            conn.execute("UPDATE tasks SET runtime_seconds_used=600 WHERE task_id=?", (runtime_task["task_id"],))
        runtime_callback = TaskEventCallback(db, runtime_task["task_id"], 0, 60)
        try:
            runtime_callback._check_control()
            print("RUNTIME_BOUNDARY: allowed=True status=%s error_code=%s" % (
                db.get_task(runtime_task["task_id"])["status"], db.get_task(runtime_task["task_id"])["error_code"]))
        except Exception as exc:
            print("RUNTIME_BOUNDARY: raised=%s" % type(exc).__name__)
        token_task = db.create_task("Baseline settlement probe task", max_total_tokens=5000)
        db.claim_next_task("probe-worker-3")
        db.reserve_tokens(token_task["task_id"], "settle-call", 3000, "probe-worker-3", 1)
        db.settle_token_reservation(token_task["task_id"], "settle-call", 1200, "probe-worker-3", 1)
        db.settle_token_reservation(token_task["task_id"], "settle-call", 1200, "probe-worker-3", 1)
        t=db.get_task(token_task["task_id"])
        print("EXISTING_DB_SETTLEMENT: used=%s reserved=%s (callback path was not connected)" % (t["total_tokens_used"],t["reserved_tokens"]))
    runtime = Path("app/runtime")
    hits=[]
    for path in runtime.rglob("*.py"):
        source=path.read_text(encoding="utf-8")
        for line_no,line in enumerate(source.splitlines(),1):
            if "app.tools.research_tools" in line or "app.agents.research" in line:
                hits.append(f"{path}:{line_no}:{line.strip()}")
    print("STATIC_RUNTIME_RESEARCH_IMPORT_HITS:",len(hits),hits)
    code="import sys; sys.modules['httpx']=None; sys.modules['bs4']=None; import app.runtime.executor"
    probe=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True)
    print("MINIMAL_IMPORT: success=%s stderr=%s" % (probe.returncode==0,probe.stderr.strip().splitlines()[-1] if probe.stderr else ""))
    profile=load_research_profile(); defaults=get_agent_registration("research").budget_defaults
    print("RESEARCH_DEFAULTS: profile.max_steps=%s effective.max_steps=%s runtime=%s tokens=%s" % (
        profile.max_steps,defaults.max_steps,defaults.max_runtime_seconds,defaults.max_total_tokens))


if __name__=="__main__":
    main()

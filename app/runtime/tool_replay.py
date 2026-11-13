"""Skip graph tool replays only when the durable call is already successful."""
import json


def tool_replay_guard(db, task_id):
    from hashlib import sha256
    from langchain.agents.middleware import wrap_tool_call
    from langchain_core.messages import ToolMessage
    from langgraph.config import get_config
    from app.runtime.recovery_manager import RecoveryManager, READ_ONLY_TOOLS
    from app.runtime.control import TaskWaitingForHuman
    owner = db.get_task(task_id)

    @wrap_tool_call
    def guard(request, handler):
        call = request.tool_call
        config = get_config()
        metadata = config.get("metadata") or {}
        key = RecoveryManager.call_key(task_id, call["name"], json.dumps(call["args"]), metadata, call["id"])
        with db.transaction() as conn:
            db._require_owner(conn, task_id, owner["worker_id"], owner["worker_generation"])
            record = conn.execute("SELECT * FROM tool_calls WHERE task_id=? AND idempotency_key=?", (task_id, key)).fetchone()
        if record and record["status"] == "succeeded":
            saved = json.loads(record["result_json"])["summary"]
            if sha256(saved.encode("utf-8")).hexdigest() == record["result_hash"]:
                return ToolMessage(content=saved, tool_call_id=call["id"], name=call["name"])
        if record and call["name"] not in READ_ONLY_TOOLS:
            # Uncertain writes must first pass RecoveryManager's read-back checks.
            db.set_waiting_for_human(task_id, "tool_replay_conflict",
                {"summary": "Uncertain write replay", "call_id": record["call_id"], "choices": ["terminate"]},
                owner["worker_id"], owner["worker_generation"])
            raise TaskWaitingForHuman("write replay requires persistence reconciliation")
        return handler(request)

    return guard

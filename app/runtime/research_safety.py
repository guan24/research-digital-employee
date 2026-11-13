"""Task-scoped, bounded correction of model output and tool arguments."""
import json

from pydantic import ValidationError

from app.runtime.errors import ClassifiedTaskError


def correction_failure(db, task_id, tool_name, error, worker_id=None, generation=None):
    errors = error.errors(include_input=False, include_url=False) if isinstance(error, ValidationError) else [
        {"type": type(error).__name__, "loc": [], "msg": str(error)}
    ]
    details = [{"type": e["type"], "path": ".".join(map(str, e["loc"])),
                "expected": e["msg"], "suggestion": "Match the declared tool schema and business constraints."}
               for e in errors]
    with db.transaction() as conn:
        db._require_owner(conn, task_id, worker_id, generation)
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='research_tool_validation_failed' "
            "AND json_extract(payload_json,'$.tool')=?", (task_id, tool_name)
        ).fetchone()[0] + 1
        payload = {"success": False, "tool": tool_name, "attempt": count, "errors": details,
                   "retryable": count == 1, "reason": "Correct the arguments once." if count == 1 else "Tool argument correction exhausted."}
        db._insert_event(conn, task_id, "research_tool_validation_failed", payload)
    if count > 1:
        raise ClassifiedTaskError(json.dumps(payload), "terminal", "TOOL_ARGUMENT_CORRECTION_EXHAUSTED", False)
    return json.dumps(payload, ensure_ascii=False)


def model_output_guard(db, task_id):
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import HumanMessage

    owner = db.get_task(task_id)

    class OutputGuard(AgentMiddleware):
        def check_response(self, response):
            truncated = [m for m in response.result if (m.response_metadata or {}).get("finish_reason") == "length"]
            if not truncated:
                return False
            with db.transaction() as conn:
                db._require_owner(conn, task_id, owner["worker_id"], owner["worker_generation"])
                count = conn.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='model_output_truncated'", (task_id,)).fetchone()[0] + 1
                db._insert_event(conn, task_id, "model_output_truncated", {
                    "attempt": count, "retryable": count == 1,
                    "responses": [m.model_dump(mode="json") for m in truncated],
                })
            if count > 1:
                raise ClassifiedTaskError("Model output remained truncated after one correction; no tools dispatched.",
                                          "terminal", "MODEL_OUTPUT_TRUNCATED", False)
            return True

        def corrected_request(self, request):
            return request.override(messages=[*request.messages, HumanMessage(content=
                "The previous response was truncated (finish_reason=length) and discarded; no tools were executed. "
                "Retry once with concise complete JSON. Use 3 short complete plan steps if planning. "
                "Keep the current output limit; do not repeat unnecessary prose or tools.")])

        def wrap_model_call(self, request, handler):
            response = handler(request)
            if self.check_response(response):
                response = handler(self.corrected_request(request))
                self.check_response(response)
            return response

        async def awrap_model_call(self, request, handler):
            response = await handler(request)
            if self.check_response(response):
                response = await handler(self.corrected_request(request))
                self.check_response(response)
            return response

    return OutputGuard()

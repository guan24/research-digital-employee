import json
import pytest

from fastapi.testclient import TestClient
from langchain.agents import create_agent

from app.agents import registry
from app.api.main import app
from app.infrastructure.artifact_store import ArtifactStore
from app.runtime.executor import ResearchExecutor, sqlite_checkpointer
from app.runtime.worker import Worker
from app.tools.common import TaskToolService
from test_autonomous_research_e2e import ToolCallingModel, call
from langchain_core.messages import AIMessage


def test_analysis_role_uses_api_worker_checkpoint_and_own_lifecycle(settings, monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    monkeypatch.setenv("APP_HOME", str(settings.app_home))
    monkeypatch.setenv("DATA_DIR", str(settings.database_path.parent))
    monkeypatch.setenv("WORKSPACE_DIR", str(settings.workspace_dir))
    finalized = []
    model = ToolCallingModel(responses=[
        call("save_artifact", path="plan.json", content=json.dumps({"steps": ["analyze", "deliver"]})),
        call("read_artifact", path="plan.json"),
        call("save_artifact", path="analysis.json", content=json.dumps({"value": 42})),
        AIMessage(content="Analysis complete"),
    ])
    def builder(settings, db, task, saver, started):
        service = TaskToolService(settings, db, task["task_id"], ArtifactStore(settings.workspace_dir, task["task_id"]), started, task.get("worker_id"), task.get("worker_generation"))
        return create_agent(model, service.as_common_tools(), checkpointer=saver)

    def validate(content, task, store):
        return json.loads(content) == {"value": 42} and store.exists("plan.json")

    registry.register_agent("test_analysis", builder, output_artifact="analysis.json", validator=validate,
        budget_defaults=registry.BudgetDefaults(10, 60, 10000),
        finalizer=lambda db, task_id, store, status, task: finalized.append((task_id, status, store.read_text("analysis.json"))))
    with TestClient(app) as client:
        response = client.post("/tasks", json={"goal": "Analyze the offline test dataset", "agent_type": "test_analysis"})
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        database = client.app.state.db
        assert Worker(settings, database, ResearchExecutor(settings, database)).run_once()
        task = client.get(f"/tasks/{task_id}").json()
        assert task["status"] == "completed"
        assert finalized == [(task_id, "completed", '{"value": 42}')]
        assert database.list_quality_assessments(task_id) == []
        events = database.get_events(task_id)
        assert not any(e["event_type"].startswith("research_") for e in events)
        assert sum(e["event_type"] == "tool_call_completed" for e in events) == 3
        assert database.get_task(task_id)["max_steps"] == 10
        assert registry.get_agent_registration("research").budget_defaults.max_steps != 10
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        assert saver.get_tuple({"configurable": {"thread_id": task_id}}) is not None


@pytest.mark.parametrize('action', ['pause', 'cancel', 'retry'])
def test_second_role_control_and_retry_use_public_worker(settings, db, monkeypatch, action):
    monkeypatch.setattr(registry, '_REGISTRY', dict(registry._REGISTRY))
    seen = []
    from langgraph.graph import START, END, StateGraph
    from typing import TypedDict

    class State(TypedDict):
        messages: list

    def builder(settings, database, task, saver, started):
        service = TaskToolService(settings, database, task['task_id'], ArtifactStore(settings.workspace_dir, task['task_id']), started, task.get('worker_id'), task.get('worker_generation'))
        def analyze(state):
            seen.append(task['worker_generation'])
            if len(seen) == 1:
                if action == 'pause':
                    database.request_pause(task['task_id'])
                elif action == 'cancel':
                    database.request_cancel(task['task_id'])
                else:
                    raise TimeoutError('offline transient failure')
            service.check_control()
            return {'messages': [AIMessage(content='{"value": 42}')]}
        graph = StateGraph(State)
        graph.add_node('analyze', analyze)
        graph.add_edge(START, 'analyze')
        graph.add_edge('analyze', END)
        return graph.compile(checkpointer=saver)

    registry.register_agent('control_analysis', builder, output_artifact='analysis.json', validator=lambda text, *_: json.loads(text) == {'value': 42})
    task = db.create_task('Analyze data with controlled interruption', agent_type='control_analysis')
    worker = Worker(settings, db, ResearchExecutor(settings, db))
    assert worker.run_once()
    current = db.get_task(task['task_id'])
    if action == 'cancel':
        assert current['status'] == 'cancelled'
        assert len(seen) == 1
        return
    assert current['status'] == ('paused' if action == 'pause' else 'retry_wait')
    if action == 'pause':
        assert db.resume_task(task['task_id'])
    else:
        with db.transaction() as conn:
            conn.execute('UPDATE tasks SET next_run_at=0 WHERE task_id=?', (task['task_id'],))
    assert worker.run_once()
    assert db.get_task(task['task_id'])['status'] == 'completed'
    assert seen == [1, 2]
    assert any(e['event_type'] == 'checkpoint_resume' for e in db.get_events(task['task_id']))

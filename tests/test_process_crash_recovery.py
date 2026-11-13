import multiprocessing
from pathlib import Path
from typing import TypedDict

import pytest

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.runtime.executor import sqlite_checkpointer
from app.runtime.recovery_manager import RecoveryManager


class CrashState(TypedDict):
    messages: list[str]
    completed: int


def graph_for_crash(store, saver, pipe=None):
    from langgraph.graph import START, END, StateGraph
    def saved(state):
        previous = store.read_text('count.txt') if store.exists('count.txt') else '0'
        store.write_text('count.txt', str(int(previous) + 1))
        return {'messages': ['saved tool result'], 'completed': 1}
    def interrupted(state):
        if pipe is not None:
            pipe.send('checkpoint-ready')
            multiprocessing.Event().wait(60)
        return {'messages': [*state['messages'], 'resumed'], 'completed': state['completed']}
    graph = StateGraph(CrashState)
    graph.add_node('saved', saved)
    graph.add_node('interrupted', interrupted)
    graph.add_edge(START, 'saved')
    graph.add_edge('saved', 'interrupted')
    graph.add_edge('interrupted', END)
    return graph.compile(checkpointer=saver)


def crash_child(database_path, workspace, checkpoint_path, mode, pipe, settings):
    # Spawned processes do not inherit pytest monkeypatches. This worker has no
    # model/provider or HTTP client; its only operations are local SQLite/files.
    db = Database(Path(database_path))
    db.init()
    task = db.create_task('Offline child process interruption')
    claimed = db.claim_next_task('child-worker')
    assert db.acquire_worker_lease('child-worker', 60)
    store = ArtifactStore(Path(workspace), task['task_id'])
    pipe.send(task['task_id'])
    manager = RecoveryManager(db, Path(workspace), Path(checkpoint_path))
    if mode == 'checkpoint':
        with sqlite_checkpointer(Path(checkpoint_path)) as saver:
            graph_for_crash(store, saver, pipe).invoke({'messages': [], 'completed': 0},
                {'configurable': {'thread_id': task['task_id']}}, durability='sync')
    else:
        import json
        manager.begin_tool_call(task['task_id'], 'interrupted', 'save_artifact' if mode == 'artifact' else 'web_search',
            json.dumps({'path': 'result.json', 'content': '{"value": 42}'}) if mode == 'artifact' else '{}',
            claimed['worker_generation'], worker_id='child-worker')
        if mode == 'artifact':
            import time
            from app.tools.common import TaskToolService
            service = TaskToolService(settings, db, task['task_id'], store, time.monotonic(),
                'child-worker', claimed['worker_generation'])
            save = next(tool for tool in service.as_common_tools() if tool.name == 'save_artifact')
            save.invoke({'path': 'result.json', 'content': '{"value": 42}'})
        pipe.send('side-effect-ready' if mode == 'artifact' else 'start-ready')
        multiprocessing.Event().wait(60)


@pytest.mark.parametrize('mode', ['artifact', 'started', 'checkpoint'])
def test_real_process_kill_and_recovery(settings, db, mode):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=crash_child, args=(str(settings.database_path), str(settings.workspace_dir), str(settings.checkpoint_path), mode, child, settings))
    process.start()
    child.close()
    try:
        assert parent.poll(30), 'child did not create a task'
        task_id = parent.recv()
        assert parent.poll(30), 'child did not reach the crash barrier'
        assert parent.recv() == {'artifact': 'side-effect-ready', 'started': 'start-ready', 'checkpoint': 'checkpoint-ready'}[mode]
        store = ArtifactStore(settings.workspace_dir, task_id)
        if mode == 'checkpoint':
            with sqlite_checkpointer(settings.checkpoint_path) as saver:
                assert graph_for_crash(store, saver).get_state({'configurable': {'thread_id': task_id}}).next == ('interrupted',)
        before = (store.workspace / 'result.json').stat().st_mtime_ns if mode == 'artifact' else None
        process.terminate()
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode != 0
        with db.transaction() as conn:
            conn.execute('UPDATE tasks SET heartbeat_at=0 WHERE task_id=?', (task_id,))
            conn.execute('UPDATE worker_lease SET lease_until=0')
        from app.runtime.worker import Worker
        class ResumeExecutor:
            def run(self, task, worker_id=None):
                assert task['worker_generation'] == 2
                if mode == 'checkpoint':
                    with sqlite_checkpointer(settings.checkpoint_path) as saver:
                        graph = graph_for_crash(store, saver)
                        config = {'configurable': {'thread_id': task_id}}
                        snapshot = graph.get_state(config)
                        assert snapshot.next == ('interrupted',)
                        assert snapshot.values == {'messages': ['saved tool result'], 'completed': 1}
                        assert graph.invoke(None, config)['messages'] == ['saved tool result', 'resumed']
                        assert graph.get_state(config).next == ()
                    store.write_text('result.json', '{"resumed": true}')
                elif mode == 'started':
                    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
                    call_id = manager.begin_tool_call(task_id, 'interrupted', 'web_search', '{}', 2, worker_id=worker_id)
                    db.finish_tool_call(task_id, call_id, 'offline read result', worker_id, 2)
                    store.write_text('result.json', '{"read": true}')
                return str(store.workspace / 'result.json')
        assert Worker(settings, db, ResumeExecutor()).run_once()
        successor = db.get_task(task_id)
        assert successor['status'] == 'completed' and successor['worker_generation'] == 2
        decisions = db.list_recovery_decisions(task_id)
        if mode == 'artifact':
            assert any(d['action'] == 'mark_succeeded' for d in decisions)
            assert store.read_text('result.json') == '{"value": 42}'
            assert (store.workspace / 'result.json').stat().st_mtime_ns == before
            with db.connect() as conn:
                assert conn.execute('SELECT status FROM tool_calls WHERE task_id=?', (task_id,)).fetchone()[0] == 'succeeded'
            assert len([e for e in db.get_events(task_id) if e['event_type'] == 'tool_call_completed']) == 1
            assert db.get_open_tool_calls(task_id) == []
        elif mode == 'started':
            assert any(d['action'] == 'retry_allowed' for d in decisions)
            with db.connect() as conn:
                row = conn.execute('SELECT status,attempts,worker_generation FROM tool_calls WHERE task_id=?', (task_id,)).fetchone()
            assert tuple(row) == ('succeeded', 2, 2)
        else:
            assert store.read_text('count.txt') == '1'
        assert db.get_task(task_id)['worker_generation'] == successor['worker_generation']
        from runtime_assertions import assert_tool_event_consistency
        assert_tool_event_consistency(db, task_id)
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()

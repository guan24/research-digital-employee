import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.runtime.control import WorkerLeaseLost
from app.runtime.executor import TaskEventCallback
from app.runtime.recovery_manager import RecoveryManager
import time


def setup_call(db, settings):
    task = db.create_task('Race independent SQLite connections')
    owner = db.claim_next_task('worker-a')
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    manager.begin_tool_call(task['task_id'], 'call', 'web_search', '{}', owner['worker_generation'], worker_id='worker-a')
    return owner


def takeover(db, task):
    with db.transaction() as conn:
        conn.execute("UPDATE tasks SET status='queued',worker_id=NULL WHERE task_id=?", (task['task_id'],))
    successor = db.claim_next_task('worker-b')
    assert successor['worker_generation'] == task['worker_generation'] + 1
    return successor


def terminals(db, task):
    return [e for e in db.get_events(task['task_id']) if e['event_type'] in {'tool_call_completed', 'tool_call_failed'}]


@pytest.mark.parametrize('operation', ['success', 'failure', 'start', 'completed', 'failed', 'paused', 'cancelled', 'retry'])
def test_takeover_after_python_check_rejects_old_write(db, settings, operation):
    task = setup_call(db, settings)
    ready = threading.Event()
    proceed = threading.Event()

    def old_worker():
        assert db.get_task(task['task_id'])['worker_generation'] == task['worker_generation']
        ready.set()
        assert proceed.wait(10)
        args = (task['task_id'], 'worker-a', task['worker_generation'])
        if operation == 'success':
            return db.finish_tool_call(task['task_id'], 'call', 'late', args[1], args[2])
        if operation == 'failure':
            return db.fail_tool_call(task['task_id'], 'call', 'late', worker_id=args[1], generation=args[2])
        if operation == 'start':
            return db.begin_tool_call(task['task_id'], 'late', 'late', 'web_search', '{}', 'hash', args[2], args[1])
        if operation == 'retry':
            return db.schedule_retry(task, 'late', 0, args[1])
        if operation == 'completed':
            return db.mark_completed(task['task_id'], 'late.md', args[1], args[2])
        if operation == 'failed':
            return db.mark_failed(task['task_id'], 'late', args[1], args[2])
        return getattr(db, 'mark_' + operation)(*args)

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(old_worker)
        try:
            assert ready.wait(10)
            successor = takeover(db, task)
            assert db.reserve_tokens(task['task_id'], 'new-reservation', 1000, 'worker-b', successor['worker_generation'])['allowed']
            successor = db.get_task(task['task_id'])
            before = db.get_events(task['task_id'])
        finally:
            proceed.set()
        with pytest.raises(WorkerLeaseLost):
            future.result(timeout=10)
    assert db.get_task(task['task_id']) == successor
    assert db.get_events(task['task_id']) == before
    assert terminals(db, task) == []


@pytest.mark.parametrize('fail', [False, True])
def test_atomic_terminal_and_duplicate_callback(db, settings, fail):
    task = setup_call(db, settings)
    def complete():
        if fail:
            return db.fail_tool_call(task['task_id'], 'call', 'error', worker_id='worker-a', generation=1)
        return db.finish_tool_call(task['task_id'], 'call', 'result', 'worker-a', 1)
    assert complete() is True
    assert complete() is False
    assert len(terminals(db, task)) == 1
    with db.connect() as conn:
        assert conn.execute('SELECT status FROM tool_calls').fetchone()[0] == ('failed' if fail else 'succeeded')


def test_competing_terminals_have_one_consistent_winner(db, settings):
    task = setup_call(db, settings)
    barrier = threading.Barrier(2)
    def finish(fail):
        barrier.wait(timeout=10)
        try:
            return db.fail_tool_call(task['task_id'], 'call', 'error', worker_id='worker-a', generation=1) if fail else db.finish_tool_call(task['task_id'], 'call', 'ok', 'worker-a', 1)
        except ValueError:
            return False
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(finish, fail) for fail in [True, False]]
        assert sum(f.result(timeout=10) for f in futures) == 1
    events = terminals(db, task)
    assert len(events) == 1
    with db.connect() as conn:
        status = conn.execute('SELECT status FROM tool_calls').fetchone()[0]
    assert events[0]['event_type'] == ('tool_call_completed' if status == 'succeeded' else 'tool_call_failed')


@pytest.mark.parametrize('fail', [False, True])
def test_event_insert_failure_rolls_back_tool_update(db, settings, fail):
    import sqlite3
    task = setup_call(db, settings)
    with db.transaction() as conn:
        event = 'tool_call_failed' if fail else 'tool_call_completed'
        conn.execute(f"CREATE TRIGGER reject_terminal BEFORE INSERT ON events WHEN NEW.event_type='{event}' BEGIN SELECT RAISE(ABORT,'injected event failure'); END")
    def complete():
        return db.fail_tool_call(task['task_id'], 'call', 'error', worker_id='worker-a', generation=1) if fail else db.finish_tool_call(task['task_id'], 'call', 'result', 'worker-a', 1)
    with pytest.raises(sqlite3.IntegrityError):
        complete()
    with db.connect() as conn:
        assert conn.execute('SELECT status FROM tool_calls').fetchone()[0] == 'running'
    assert terminals(db, task) == []
    with db.transaction() as conn:
        conn.execute('DROP TRIGGER reject_terminal')
    assert complete()


def test_successful_tool_replay_does_not_repeat_side_effect(db, settings):
    from langchain.agents import create_agent
    from langchain.agents.middleware import wrap_tool_call
    from langchain_core.tools import tool
    from langchain_core.messages import AIMessage
    from app.runtime.executor import sqlite_checkpointer
    from app.runtime.tool_replay import tool_replay_guard
    from test_autonomous_research_e2e import ToolCallingModel, call

    task = db.create_task('Verify successful graph tool replay')
    owner = db.claim_next_task('worker-a')
    assert db.acquire_worker_lease('worker-a', 60)
    writes = []
    @tool
    def write_once(value: str) -> str:
        """Persist a test side effect."""
        writes.append(value)
        return 'saved'
    interrupted = []
    @wrap_tool_call
    def crash_after_terminal(request, handler):
        result = handler(request)
        if not interrupted:
            interrupted.append(True)
            raise RuntimeError('after terminal before graph checkpoint')
        return result

    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    callback = TaskEventCallback(db, task['task_id'], time.monotonic(), 60, worker_id='worker-a',
        worker_generation=owner['worker_generation'], recovery_manager=manager)
    model = ToolCallingModel(responses=[call('write_once', value='one effect'), AIMessage(content='done')])
    config = {'configurable': {'thread_id': task['task_id']}, 'callbacks': [callback]}
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        graph = create_agent(model, [write_once], middleware=[crash_after_terminal, tool_replay_guard(db, task['task_id'])], checkpointer=saver)
        with pytest.raises(RuntimeError, match='after terminal'):
            graph.invoke({'messages': [{'role': 'user', 'content': task['goal']}]}, config)
        assert graph.get_state(config).next
        graph.invoke(None, config)
        assert graph.get_state(config).next == ()
    assert writes == ['one effect']
    assert len(terminals(db, task)) == 1


@pytest.mark.parametrize('mode', ['success', 'failure', 'start'])
def test_callback_check_before_takeover_is_not_sufficient(db, settings, monkeypatch, mode):
    task = setup_call(db, settings)
    assert db.acquire_worker_lease('worker-a', 60)
    callback = TaskEventCallback(db, task['task_id'], time.monotonic(), 60, worker_id='worker-a',
        worker_generation=1, recovery_manager=RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path))
    callback.on_tool_start({'name': 'web_search'}, '{}', run_id='call')
    ready, proceed = threading.Event(), threading.Event()
    method = '_check_control' if mode == 'start' else '_check_tool_ownership'
    original = getattr(callback, method)
    def checked():
        original()
        ready.set()
        assert proceed.wait(10)
    monkeypatch.setattr(callback, method, checked)
    def old_callback():
        if mode == 'start':
            callback.on_tool_start({'name': 'web_search'}, '{}', run_id='late')
        elif mode == 'success':
            callback.on_tool_end('late', run_id='call')
        else:
            callback.on_tool_error(RuntimeError('late'), run_id='call')
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(old_callback)
        try:
            assert ready.wait(10)
            successor = takeover(db, task)
            before = db.get_events(task['task_id'])
        finally:
            proceed.set()
        with pytest.raises(WorkerLeaseLost):
            future.result(timeout=10)
    assert db.get_task(task['task_id']) == successor
    assert db.get_events(task['task_id']) == before


def test_duplicate_outcome_with_different_result_is_rejected(db, settings):
    task = setup_call(db, settings)
    assert db.finish_tool_call(task['task_id'], 'call', 'first', 'worker-a', 1)
    with pytest.raises(ValueError, match='conflicting duplicate tool result'):
        db.finish_tool_call(task['task_id'], 'call', 'different', 'worker-a', 1)
    assert len(terminals(db, task)) == 1


@pytest.mark.parametrize('status', ['completed', 'failed', 'paused', 'cancelled', 'retry_wait'])
def test_task_outcome_and_reservations_rollback_together(db, settings, status):
    import sqlite3
    task = setup_call(db, settings)
    assert db.reserve_tokens(task['task_id'], 'reservation', 1000, 'worker-a', 1)['allowed']
    before = db.get_task(task['task_id'])
    with db.transaction() as conn:
        event = 'retry_scheduled' if status == 'retry_wait' else 'task_' + status
        conn.execute(f"CREATE TRIGGER reject_task_event BEFORE INSERT ON events WHEN NEW.event_type='{event}' BEGIN SELECT RAISE(ABORT,'injected task event failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db._transition_task(task['task_id'], status, 'worker-a', 1)
    assert db.get_task(task['task_id']) == before
    with db.connect() as conn:
        assert conn.execute('SELECT settled FROM token_reservations').fetchone()[0] == 0


def test_start_event_insert_failure_does_not_leave_a_tool_record(db):
    import sqlite3
    task = db.create_task('Verify atomic tool registration')
    owner = db.claim_next_task('worker-a')
    with db.transaction() as conn:
        conn.execute("CREATE TRIGGER reject_start BEFORE INSERT ON events WHEN NEW.event_type='tool_call_started' BEGIN SELECT RAISE(ABORT,'injected start failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.begin_tool_call(task['task_id'], 'call', 'key', 'web_search', '{}', 'hash', owner['worker_generation'], 'worker-a')
    assert db.get_open_tool_calls(task['task_id']) == []

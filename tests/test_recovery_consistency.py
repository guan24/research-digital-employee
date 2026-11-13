import json
import time

import pytest
from langchain_core.messages import ToolMessage

from app.infrastructure.artifact_store import ArtifactStore
from app.runtime.control import WorkerLeaseLost
from app.runtime.executor import TaskEventCallback
from app.runtime.recovery_manager import RecoveryManager


@pytest.mark.parametrize('output,status', [
    ('ok', 'succeeded'),
    ({'success': False, 'retryable': True}, 'failed'),
    (json.dumps({'success': False, 'retryable': False}), 'failed'),
    (ToolMessage(content='{"success": false}', tool_call_id='call'), 'failed'),
    (RuntimeError('failed'), 'failed'),
])
def test_callback_ledger_matches_event(settings, db, output, status):
    task = db.create_task('Verify durable tool status')
    db.claim_next_task('worker')
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    callback = TaskEventCallback(db, task['task_id'], time.monotonic(), 60,
                                 recovery_manager=manager)
    callback.on_tool_start({'name': 'web_search'}, '{}', run_id='call')
    if isinstance(output, Exception):
        callback.on_tool_error(output, run_id='call')
    else:
        callback.on_tool_end(output, run_id='call')
    assert tool_status(db, task['task_id']) == status
    events = db.get_events(task['task_id'])
    terminal = [e for e in events if e['event_type'] in {'tool_call_completed', 'tool_call_failed'}]
    assert len(terminal) == 1
    assert terminal[0]['event_type'] == ('tool_call_completed' if status == 'succeeded' else 'tool_call_failed')


@pytest.mark.parametrize('error', [False, True])
def test_old_generation_cannot_finish_tool(settings, db, error):
    task = db.create_task('Verify old generation is fenced')
    first = db.claim_next_task('worker')
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    callback = TaskEventCallback(db, task['task_id'], time.monotonic(), 60,
                                 worker_generation=first['worker_generation'], recovery_manager=manager)
    callback.on_tool_start({'name': 'web_search'}, '{}', run_id='old-call')
    with db.connect() as conn:
        conn.execute("UPDATE tasks SET status='queued',worker_id=NULL WHERE task_id=?", (task['task_id'],))
    successor = db.claim_next_task('successor')
    assert successor['worker_generation'] == first['worker_generation'] + 1
    before = db.get_events(task['task_id'])
    with pytest.raises(WorkerLeaseLost):
        if error:
            callback.on_tool_error(RuntimeError('late error'), run_id='old-call')
        else:
            callback.on_tool_end('late result', run_id='old-call')
    assert tool_status(db, task['task_id']) == 'running'
    assert db.get_events(task['task_id']) == before


def test_corrupt_checkpoint_is_preserved_and_parked(settings, db):
    task = db.create_task('Recover damaged checkpoint safely')
    claimed = db.claim_next_task('worker')
    settings.checkpoint_path.write_bytes(b'broken sqlite checkpoint')
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    result = manager.reconcile_stale_task(claimed)
    assert result['requires_human']
    assert db.get_task(task['task_id'])['status'] == 'waiting_for_human'
    assert settings.checkpoint_path.read_bytes() == b'broken sqlite checkpoint'


@pytest.mark.parametrize('persisted,action', [('expected', 'mark_succeeded'), ('conflict', 'manual_review')])
def test_interrupted_artifact_readback(settings, db, persisted, action):
    task = db.create_task('Recover artifact interrupted after write')
    claimed = db.claim_next_task('worker')
    store = ArtifactStore(settings.workspace_dir, task['task_id'])
    store.write_text('output.json', persisted)
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    manager.begin_tool_call(task['task_id'], 'call', 'save_artifact', json.dumps({'path': 'output.json', 'content': 'expected'}))
    outcome = manager.reconcile_stale_task(claimed)
    assert outcome['tool_calls'][0]['action'] == action
    assert store.read_text('output.json') == persisted


def tool_status(db, task_id):
    with db.connect() as conn:
        return conn.execute('SELECT status FROM tool_calls WHERE task_id=?', (task_id,)).fetchone()[0]



def test_registered_tool_recovery_policy_is_complete(settings, db):
    from app.tools.research_tools import ResearchToolService
    from app.runtime.recovery_manager import TOOL_RECOVERY_POLICIES
    task = db.create_task("Inspect all registered tool policies")
    store = ArtifactStore(settings.workspace_dir, task['task_id'])
    tools = ResearchToolService(settings, db, task['task_id'], store, time.monotonic()).as_langchain_tools()
    assert {tool.name for tool in tools} <= TOOL_RECOVERY_POLICIES.keys()
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    claimed = db.claim_next_task('worker')
    manager.begin_tool_call(task['task_id'], 'unknown', 'unregistered_side_effect', '{}')
    assert manager.reconcile_stale_task(claimed)['requires_human']


def test_finding_write_interruption_is_confirmed_without_duplicate(settings, db):
    from app.infrastructure.evidence_registry import EvidenceRegistry
    task = db.create_task('Recover interrupted finding persistence')
    claimed = db.claim_next_task('worker')
    store = ArtifactStore(settings.workspace_dir, task['task_id'])
    registry = EvidenceRegistry(store)
    source = registry.register_source(url='https://example.org/source', title='Source', content='Verified primary evidence')
    args = dict(claim='Durable graph state', source_ids=[source['source_id']], dimension_id='architecture')
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    manager.begin_tool_call(task['task_id'], 'finding', 'record_finding', json.dumps(args))
    registry.record_finding(**args)
    result = manager.reconcile_stale_task(claimed)
    assert result['tool_calls'][0]['action'] == 'mark_succeeded'
    assert registry.summary()['finding_count'] == 1
    assert tool_status(db, task['task_id']) == 'succeeded'
    assert manager.reconcile_stale_task(claimed)['tool_calls'] == []


def test_plan_interruption_with_missing_progress_requires_human(settings, db):
    task = db.create_task('Recover incomplete plan publication')
    claimed = db.claim_next_task('worker')
    store = ArtifactStore(settings.workspace_dir, task['task_id'])
    plan = dict(title='Plan', objective='Study', steps=[dict(id='read', title='Read primary source')])
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    manager.begin_tool_call(task['task_id'], 'plan', 'update_research_plan', json.dumps(plan))
    db.add_event(task['task_id'], 'research_plan_updated', dict(version=1, plan=plan))
    store.write_text('notes/research_plan.json', json.dumps(dict(version=1, **plan)))
    before = store.read_text('notes/research_plan.json')
    result = manager.reconcile_stale_task(claimed)
    assert result['requires_human']
    assert 'progress' in result['tool_calls'][0]['reason']
    assert store.read_text('notes/research_plan.json') == before


@pytest.mark.parametrize('content,action', [('saved memory', 'mark_succeeded'), ('other memory', 'manual_review')])
def test_memory_recovery_checks_persisted_content(settings, db, content, action):
    task = db.create_task('Recover memory write after interruption')
    claimed = db.claim_next_task('worker')
    store = ArtifactStore(settings.workspace_dir, task['task_id'])
    store.write_text('notes/research_memory.md', content + '\n')
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    manager.begin_tool_call(task['task_id'], 'memory', 'save_research_memory', json.dumps({'content': 'saved memory'}))
    assert manager.reconcile_stale_task(claimed)['tool_calls'][0]['action'] == action


def test_completed_step_recovery_reads_actual_artifact(settings, db):
    import hashlib
    task = db.create_task('Detect changed artifact after step completion')
    claimed = db.claim_next_task('worker')
    store = ArtifactStore(settings.workspace_dir, task['task_id'])
    store.write_text('result.json', 'changed')
    db.add_event(task['task_id'], 'research_plan_updated', {'version': 1, 'plan': {'steps': [{'id': 'step', 'expected_output_path': 'result.json'}]}})
    db.add_event(task['task_id'], 'research_plan_step_status', {'step_id': 'step', 'status': 'completed', 'plan_version': 1, 'verified_output': {'path': 'result.json', 'sha256': hashlib.sha256(b'original').hexdigest()}})
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    manager.begin_tool_call(task['task_id'], 'step', 'update_plan_step_status', json.dumps({'step_id': 'step', 'status': 'completed'}))
    assert manager.reconcile_stale_task(claimed)['requires_human']
    assert store.read_text('result.json') == 'changed'



def test_checkpoint_replay_uses_original_durable_call_id(settings, db):
    task = db.create_task('Resume tool with a new callback run id')
    claimed = db.claim_next_task('worker')
    manager = RecoveryManager(db, settings.workspace_dir, settings.checkpoint_path)
    metadata = {'langgraph_node': 'tools', 'langgraph_step': 3}
    manager.begin_tool_call(task['task_id'], 'original', 'web_search', '{}', metadata=metadata)
    callback = TaskEventCallback(db, task['task_id'], time.monotonic(), 60, recovery_manager=manager)
    callback.on_tool_start({'name': 'web_search'}, '{}', run_id='replayed', metadata=metadata)
    callback.on_tool_end('retried result', run_id='replayed')
    assert tool_status(db, task['task_id']) == 'succeeded'
    completed = [e for e in db.get_events(task['task_id']) if e['event_type'] == 'tool_call_completed']
    assert completed[-1]['payload']['call_id'] == 'original'

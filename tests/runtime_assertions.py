import json
import hashlib
from contextlib import closing

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.executor import sqlite_checkpointer


def assert_tool_event_consistency(db, task_id):
    events = db.get_events(task_id, limit=5000)
    with closing(db.connect()) as conn:
        calls = conn.execute('SELECT * FROM tool_calls WHERE task_id=?', (task_id,)).fetchall()
    for call in calls:
        terminal = [event for event in events if event['event_type'] in {'tool_call_completed', 'tool_call_failed'}
                    and event['payload'].get('call_id') == call['call_id']
                    and event['payload'].get('attempt', 1) == call['attempts']]
        if call['status'] in {'succeeded', 'failed'}:
            assert len(terminal) == 1, dict(call)
            assert terminal[0]['event_type'] == ('tool_call_completed' if call['status'] == 'succeeded' else 'tool_call_failed')
        else:
            assert terminal == [], dict(call)


def assert_research_storage_consistency(db, settings, task_id, model):
    task = db.get_task(task_id)
    assert task['status'] == 'completed' and task['worker_generation'] >= 1
    assert_tool_event_consistency(db, task_id)
    events = db.get_events(task_id, limit=5000)
    plans = [event['payload'] for event in events if event['event_type'] == 'research_plan_updated']
    store = ArtifactStore(settings.workspace_dir, task_id)
    saved = json.loads(store.read_text('notes/research_plan.json'))
    assert saved['version'] == plans[-1]['version']
    planned_steps = plans[-1]['plan']['steps']
    assert len(saved['steps']) == len(planned_steps)
    # The workspace snapshot adds live status fields to the structural plan.
    assert [{key: actual.get(key) for key in planned} for actual, planned in zip(saved['steps'], planned_steps)] == planned_steps
    for step in saved['steps']:
        if step.get('status') == 'completed':
            verified = step['verified_output']
            assert hashlib.sha256(store.read_text(verified['path']).encode('utf-8')).hexdigest() == verified['sha256']
    registry = EvidenceRegistry(store).summary()
    sources = {source['source_id']: source for source in registry['sources']}
    assert all(store.exists(source['snapshot_path']) for source in sources.values())
    assert all(set(finding['source_ids']) <= sources.keys() for finding in registry['findings'])
    with sqlite_checkpointer(settings.checkpoint_path) as saver:
        from app.agents.research.agent import build_research_agent
        import time
        checkpoint = saver.get_tuple({'configurable': {'thread_id': task_id}})
        assert checkpoint is not None
        # Let the production graph reconstruct versioned/delta channels.
        graph = build_research_agent(settings, db, task, saver, time.monotonic(), model=model)
        snapshot = graph.get_state({'configurable': {'thread_id': task_id}})
        assert snapshot.next == ()
        messages = snapshot.values['messages']
        assert any(getattr(message, 'name', '') == 'record_finding' for message in messages)
        assert any(getattr(message, 'name', '') == 'get_research_status' for message in messages)
    return messages

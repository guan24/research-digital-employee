import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.api.task_examples import EXAMPLE_DIR, EXAMPLE_ID
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.evidence_registry import EvidenceRegistry


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspaces"))
    with TestClient(app) as value:
        yield value


def test_bundled_example_is_visible_with_empty_database_and_never_claimed(client):
    assert client.get("/tasks").json() == []
    examples = client.get("/task-examples").json()
    assert len(examples) == 1 and examples[0]["task_id"] == EXAMPLE_ID
    assert examples[0]["is_example"] is True
    assert "降级交付" in examples[0]["example_description"]
    task = client.get(f"/tasks/{EXAMPLE_ID}").json()
    assert task["status"] == "completed" and task["is_example"]
    assert client.app.state.db.get_task(EXAMPLE_ID) is None
    assert client.app.state.db.claim_next_task("example-worker-probe") is None
    assert not (client.app.state.settings.workspace_dir / EXAMPLE_ID).exists()
    live = client.post("/tasks", json={"goal": "Create a separate real task alongside the static example"}).json()
    assert live["is_example"] is False
    assert [t["task_id"] for t in client.get("/tasks").json()] == [live["task_id"]]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("suffix", ["", "/retry", "/resume", "/pause", "/cancel", "/budget/approve",
                                     "/human-intervention/resolve", "/artifacts/report.md", "/unknown-future-action"])
def test_every_example_mutation_is_rejected_before_database_or_file_changes(client, method, suffix):
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in EXAMPLE_DIR.rglob("*") if p.is_file()}
    response = client.request(method, f"/tasks/{EXAMPLE_ID}{suffix}", json={"reason": "Try modifying the example"})
    assert response.status_code == 405 and response.headers["allow"] == "GET"
    assert "只读" in response.json()["detail"]
    assert client.app.state.db.list_tasks() == []
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in EXAMPLE_DIR.rglob("*") if p.is_file()}


def test_example_full_history_plan_quality_and_report_are_readable(client):
    root = f"/tasks/{EXAMPLE_ID}"
    events = client.get(root + "/events?limit=2000").json()
    assert len(events) == 177 and events[0]["event_type"] == "task_created"
    assert any(e["event_type"] == "report_delivered" for e in events)
    assert client.get(root + "/events?limit=2").json() == events[-2:]
    first = events[0]["event_id"]
    assert client.get(root + f"/events?after_id={first}&limit=2").json() == events[1:3]
    assert "event: task_event" in client.get(root + "/events/stream?limit=2").text
    for name in ("plan", "budget", "memory", "evidence", "research"):
        assert client.get(root + "/" + name).status_code == 200
    quality = client.get(root + "/research").json()["quality_assessments"][-1]
    assert quality["passed"] is False and quality["reviewer"]["status"] == "unavailable"
    files = client.get(root + "/artifacts").json()["artifacts"]
    assert len(files) == 10 and any(f["path"] == "report.md" for f in files)
    for item in files:
        response = client.get(item["url"])
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == item["sha256"]
    report = client.get(root + "/artifacts/report.md").text
    assert "S0001" in report and "彩虹" in report
    evidence = client.get(root + "/evidence").json()
    for source in evidence["sources"]:
        snapshot = client.get(root + f'/evidence/sources/{source["source_id"]}/snapshot')
        assert snapshot.status_code == 200 and source["url"] in snapshot.text
    assert client.get(root + "/artifacts/%2e%2e%2f%2e%2e%2f.env").status_code == 404
    assert client.get(root + "/evidence/sources/S9999/snapshot").status_code == 404


def test_published_evidence_integrity_and_citations_survive_export(tmp_path, client):
    import shutil
    shutil.copytree(EXAMPLE_DIR / "artifacts", tmp_path / EXAMPLE_ID)
    registry = EvidenceRegistry(ArtifactStore(tmp_path, EXAMPLE_ID))
    evidence = registry.summary()
    assert evidence["source_count"] == 1 and evidence["finding_count"] > 0
    ids = {s["source_id"] for s in evidence["sources"]}
    for source_id in ids:
        assert registry.read_snapshot(source_id)
    assert all(set(f["source_ids"]) <= ids for f in evidence["findings"])
    assert "[S0001]" in (EXAMPLE_DIR / "artifacts/report.md").read_text(encoding="utf-8")


def test_example_reader_rejects_tampered_artifact(tmp_path, monkeypatch, client):
    import shutil
    import app.api.task_examples as module
    copied = tmp_path / "example"
    shutil.copytree(EXAMPLE_DIR, copied)
    (copied / "artifacts/report.md").write_text("Tampered report", encoding="utf-8")
    monkeypatch.setattr(module, "EXAMPLE_DIR", copied)
    assert client.get(f"/tasks/{EXAMPLE_ID}/artifacts/report.md").status_code == 409


def test_published_snapshot_has_no_runtime_databases_or_personal_paths():
    from app.infrastructure.redaction import OPENAI_PATTERN, TAVILY_PATTERN, BEARER_PATTERN
    files = [p for p in EXAMPLE_DIR.rglob("*") if p.is_file()]
    assert all(p.suffix in {".json", ".md"} for p in files)
    for path in files:
        content = path.read_text(encoding="utf-8")
        assert "C:\\Users\\" not in content and "D:\\Project\\" not in content
        assert "ws-z0" not in content
        assert not OPENAI_PATTERN.search(content) and not TAVILY_PATTERN.search(content)
        assert not BEARER_PATTERN.search(content)


def test_frontend_example_disables_actions_without_sending_requests():
    import subprocess
    source = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text(encoding="utf-8")
    functions = []
    for marker in ("function renderTaskSummary()", "async function action(name)",
                   "async function deleteSelected()", "async function submitHumanDecision(event)"):
        start = source.index(marker)
        functions.append(source[start:source.index("\n}", start) + 2])
    # Only execute function bodies under test, with local DOM and network doubles.
    script = """
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = {};
const sandbox = {
  detail: {is_example:true,status:'failed',goal:'Example',task_id:'example-rainbow-colors',attempts:1,max_attempts:3},
  $: id => nodes[id] ||= {disabled:false,innerHTML:'',textContent:''},
  names:{failed:'Failed'},parseObject:()=>null,planSnapshot:()=>({latest:null}),roleLabel:()=> 'Research',
  compact:v=>v,esc:v=>String(v??''),formatDuration:()=>'',taskRuntimeSeconds:()=>0,time:()=>'',
  errorCategoryClass:()=>'',errorCategoryLabel:()=>'',renderHealth:()=>{},renderBudgetAndEvidence:()=>{},
  renderQuality:()=>{},renderInstanceDiagnostics:()=>{},pendingHumanIntervention:{},
  req:()=>{throw Error('Read-only example sent a mutation request')},
  confirm:()=>{throw Error('Read-only example requested mutation confirmation')}
};
vm.createContext(sandbox);
"""
    script += "vm.runInContext(" + json.dumps("\n".join(functions)) + ", sandbox);\n" + """
(async()=>{
  vm.runInContext('renderTaskSummary()',sandbox);
  for(const id of ['pause','resume','retry','cancel','delete']) assert.equal(nodes[id].disabled,true);
  assert.match(nodes.detail.innerHTML,/\u53ea\u8bfb\u4efb\u52a1\u6267\u884c\u6837\u4f8b/);
  await vm.runInContext("action('retry')",sandbox);
  await vm.runInContext('deleteSelected()',sandbox);
  await vm.runInContext('submitHumanDecision({preventDefault(){}})',sandbox);
  console.log('read-only frontend verified');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "read-only frontend verified" in result.stdout

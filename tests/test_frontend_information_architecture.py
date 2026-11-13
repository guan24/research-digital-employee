from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")


def test_task_detail_information_architecture_order():
    positions = [
        HTML.index('id="detail"'),
        HTML.index('id="healthPanel"'),
        HTML.index('id="planPanel"'),
        HTML.index('id="events"'),
        HTML.index('id="evidencePanel"'),
        HTML.index('id="qualityPanel"'),
        HTML.index('id="artifacts"'),
        HTML.index('id="instanceDiagnostics"'),
    ]
    assert positions == sorted(positions)
    assert "任务身份" in HTML
    assert "执行计划" in HTML
    assert "执行轨迹" in HTML


def test_budget_panel_renders_remaining_reserved_and_chinese_states():
    assert "剩余 '+esc(remaining)" in JS
    assert "已预留 " in JS
    for label in ("正常", "预警", "受限收敛", "已达软上限", "已终止"):
        assert label in JS
    assert "探索工具已禁用，仅可复用已有证据并完成报告" in JS
    assert "requestBudgetExtension" in JS
    assert "/budget/approve" in JS


def test_evidence_ledger_has_source_id_verification_and_snapshot_preview():
    assert "来源编号" in JS
    assert "核验状态" in JS
    assert "source.source_id" in JS
    assert "sourceDomain(source.url)" in JS
    assert "data-snapshot" in JS
    assert "openEvidenceSnapshot(button.dataset.snapshotSource" in JS
    assert "data-snapshot-source" in JS
    assert "/evidence/sources/" in JS and "/snapshot" in JS
    assert "SHA-256 已核验" in JS
    assert "原始来源" in JS
    assert "尚未登记证据" in JS


def test_failure_classification_error_code_first_cause_and_history_are_rendered():
    assert "detail.error_category" in JS
    assert "detail.error_code" in JS
    assert "detail.first_error_json" in JS
    assert "首次失败原因" in JS
    assert "错误历史" in JS
    assert "可重试" in JS and "终态" in JS and "未知" in JS
    assert "下次重试" in JS


def test_agent_selection_and_role_identity_use_registered_agent_catalog():
    assert 'id="agentType"' in HTML
    assert "req('/agents')" in JS
    assert "display_name" in JS
    assert "agent_type:form.get('agent_type')" in JS
    assert "岗位默认配额" in JS
    assert "max_revisions" in JS
    assert "roleLabel(detail.agent_type)" in JS


def test_navigation_has_no_inert_clickable_items():
    assert HTML.count('class="nav') == 1
    assert '<a class="nav active" href="#taskWorkspace">' in HTML
    assert 'class="nav">◉' not in HTML
    assert 'class="nav">▤' not in HTML
    assert 'class="nav">⌁' not in HTML


def test_list_filters_visibility_polling_and_mobile_worker_summary_are_present():
    assert 'value="abnormal"' in HTML
    assert 'value="budget_alert"' in HTML
    assert 'value="activity"' in HTML
    assert "function taskIsStale(task)" in JS
    assert "已降级为轮询" in JS
    assert "document.visibilityState === 'hidden'" in JS
    assert "@media(max-width:560px)" in CSS
    assert ".appbar .worker" in CSS


def test_delivery_validation_event_exposes_existing_quality_metrics():
    source = (ROOT / "app" / "runtime" / "executor.py").read_text(encoding="utf-8")
    assert '"evidence_coverage": getattr(validation, "evidence_coverage", None)' in source
    assert '"source_count": getattr(validation, "source_count", None)' in source
    assert '"finding_count": getattr(validation, "finding_count", None)' in source

from pathlib import Path


def test_execution_trace_uses_incremental_dom_updates_and_collapsed_raw_model_io():
    source = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function upsertTraceEvent(event)" in source
    assert "container.prepend(next)" in source
    assert "oldDetails.open = wasOpen" in source
    assert "node.scrollTop = scrollPositions[index] || 0" in source
    assert "renderTrace()" in source
    assert "renderDetail()" not in source
    assert "查看完整请求 / 响应" in source
    assert "raw_request" in source and "raw_response" in source
    assert "decision_summary" in source
    assert "traceItems()" in source

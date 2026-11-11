from app.runtime.report_validator import ReportValidator


def test_report_validator_requires_sources_and_expected_structure():
    report = "# Research\n\n## 摘要\n\n" + ("Evidence and analysis. " * 20) + "\n\n## Sources\n- https://example.org/source"
    result = ReportValidator().validate(report, "Research autonomous agent architecture")
    assert result.valid
    assert result.source_count == 1


def test_report_validator_returns_actionable_feedback_for_unsupported_report():
    result = ReportValidator().validate("# Short\n\nNo citations.", "Research a sufficiently detailed technical topic")
    assert not result.valid
    assert any("来源 URL" in error for error in result.errors)

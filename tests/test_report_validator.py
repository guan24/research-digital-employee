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


def test_report_validator_checks_registered_source_ids_and_paragraph_coverage(tmp_path):
    from app.infrastructure.artifact_store import ArtifactStore
    from app.infrastructure.evidence_registry import EvidenceRegistry

    registry = EvidenceRegistry(ArtifactStore(tmp_path, "validator-task"))
    source = registry.register_source(url="https://example.org/primary", title="Primary source",
                                      content="Verified primary source text " * 20)
    report = (
        "# Research report\n\n## 摘要\n\n"
        + ("The primary source documents the verified architecture and its implementation details. [" + source["source_id"] + "]\n\n")
        + ("This paragraph compares the implementation and discusses its implications for long-running tasks. [" + source["source_id"] + "]\n\n")
        + "## 结论\n\nThe source supports the summary. [" + source["source_id"] + "]\n\n"
        + "## Sources\n- https://example.org/primary"
    )
    result = ReportValidator().validate(report, evidence_registry=registry.summary())
    assert result.valid
    assert result.cited_source_ids == (source["source_id"],)
    assert result.evidence_coverage == 1.0


def test_evidence_registry_rejects_unknown_source_and_deduplicates(tmp_path):
    from app.infrastructure.artifact_store import ArtifactStore
    from app.infrastructure.evidence_registry import EvidenceRegistry

    registry = EvidenceRegistry(ArtifactStore(tmp_path, "evidence-task"))
    first = registry.register_source(url="https://example.org/a", title="A", content="same content")
    duplicate = registry.register_source(url="https://example.org/a", title="A", content="same content")
    assert first["source_id"] == duplicate["source_id"]
    finding = registry.record_finding(claim="A grounded claim", source_ids=[first["source_id"]])
    assert finding["source_ids"] == [first["source_id"]]
    try:
        registry.record_finding(claim="unsupported", source_ids=["S9999"])
    except ValueError as exc:
        assert "unknown source_id" in str(exc)
    else:
        raise AssertionError("unknown evidence IDs must be rejected")

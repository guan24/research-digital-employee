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

def test_validator_reports_registered_sources_separately_from_report_urls(tmp_path):
    from app.infrastructure.artifact_store import ArtifactStore
    from app.infrastructure.evidence_registry import EvidenceRegistry

    registry = EvidenceRegistry(ArtifactStore(tmp_path, "source-count-task"))
    [
        registry.register_source(
            url=f"https://example.org/source-{index}",
            title=f"Source {index}",
            content=f"Verified source body {index}. " * 10,
        )
        for index in range(1, 6)
    ]
    report = (
        "# Research report\n\n## 摘要\n\n"
        + ("A substantive but uncited finding is presented here. " * 12)
        + "\n\n## Sources\n- https://example.org/source-1"
    )
    result = ReportValidator().validate(report, evidence_registry=registry.summary())

    assert not result.valid
    assert result.source_count == 5
    assert result.registered_source_count == 5
    assert result.verified_source_count == 5
    assert result.report_url_count == 1
    assert result.cited_source_count == 0
    assert result.evidence_coverage == 0.0
    assert any("结论级来源引用" in error for error in result.errors)


def test_validator_counts_citations_in_bullet_and_table_blocks(tmp_path):
    from app.infrastructure.artifact_store import ArtifactStore
    from app.infrastructure.evidence_registry import EvidenceRegistry

    registry = EvidenceRegistry(ArtifactStore(tmp_path, "bullet-evidence-task"))
    source = registry.register_source(
        url="https://example.org/primary",
        title="Primary source",
        content="Primary source evidence for the report. " * 20,
    )
    report = (
        "# Research report\n\n## Findings\n\n"
        + ("- The first verified finding is grounded in the primary source and explains the observed design choice. [" + source["source_id"] + "]\n")
        + ("- The second finding compares the evidence and describes the consequence for implementation. [" + source["source_id"] + "]\n\n")
        + "## Sources\n- https://example.org/primary"
    )
    result = ReportValidator().validate(report, evidence_registry=registry.summary())

    assert result.valid
    assert result.evidence_coverage == 1.0


def test_validator_rejects_a_cited_source_without_an_intact_snapshot(tmp_path):
    from app.infrastructure.artifact_store import ArtifactStore
    from app.infrastructure.evidence_registry import EvidenceRegistry

    registry = EvidenceRegistry(ArtifactStore(tmp_path, "snapshot-integrity-task"))
    source = registry.register_source(
        url="https://example.org/primary",
        title="Primary source",
        content="Verified primary evidence " * 20,
    )
    summary = registry.summary()
    summary["sources"][0]["snapshot_exists"] = False
    report = (
        "# Research report\n\n## 摘要\n\n"
        + ("This paragraph summarizes the verified primary evidence and its implications. [" + source["source_id"] + "]\n\n")
        + ("This paragraph explains how the source supports the comparison and conclusion. [" + source["source_id"] + "]\n\n")
        + "## Sources\n- https://example.org/primary"
    )
    result = ReportValidator().validate(report, evidence_registry=summary)

    assert not result.valid
    assert any("快照存在性/完整性核验" in error for error in result.errors)

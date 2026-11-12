from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

URL_RE = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)
SOURCE_ID_RE = re.compile(r"\[(S\d{4})\]", re.IGNORECASE)
FINDING_ID_RE = re.compile(r"\[(F\d{4})\]", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
SOURCE_HEADING_RE = re.compile(
    r"(?:^|\b)(?:sources?|references?|参考资料|参考来源|参考文献|资料来源|来源)(?:\b|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReportValidation:
    valid: bool
    errors: tuple[str, ...]
    source_count: int
    word_count: int
    cited_source_ids: tuple[str, ...] = field(default_factory=tuple)
    evidence_coverage: float = 0.0
    finding_count: int = 0
    registered_source_count: int = 0
    verified_source_count: int = 0
    report_url_count: int = 0
    cited_source_count: int = 0
    cited_finding_ids: tuple[str, ...] = field(default_factory=tuple)


def _normalize_url(value: str) -> str:
    return str(value or "").strip().rstrip(".,;:，。；：)]}")


def _source_section(text: str) -> tuple[bool, str, int | None]:
    headings = list(HEADING_RE.finditer(text))
    for index, heading in enumerate(headings):
        title = heading.group(2).strip().strip("#").strip()
        if not SOURCE_HEADING_RE.search(title):
            continue
        level = len(heading.group(1))
        end = len(text)
        for next_heading in headings[index + 1:]:
            if len(next_heading.group(1)) <= level:
                end = next_heading.start()
                break
        return True, text[heading.end():end], heading.start()
    return False, "", None


class ReportValidator:
    """Deterministic structure and provenance checks; it does not claim to prove truth."""

    def validate(
        self,
        report: str,
        goal: str = "",
        evidence_registry: dict[str, Any] | None = None,
    ) -> ReportValidation:
        text = (report or "").strip()
        errors: list[str] = []
        if not re.search(r"^#\s+\S+", text, re.MULTILINE):
            errors.append("缺少 Markdown 一级标题。")

        has_sources_section, sources_section, source_heading_start = _source_section(text)
        if not has_sources_section:
            errors.append("缺少独立的 Sources/参考资料章节。")
            body_text = text
        else:
            body_text = text[:source_heading_start]
        if len(body_text.strip()) < 240:
            errors.append("报告正文过短，缺少足以支撑研究结论的内容（至少约 240 个字符，不含来源列表）。")
        all_urls = {_normalize_url(value) for value in URL_RE.findall(text)}
        source_urls = {_normalize_url(value) for value in URL_RE.findall(sources_section)}
        if not all_urls:
            errors.append("报告没有可核验的来源 URL，请补充来源链接及其支持的结论。")
        if has_sources_section and not source_urls:
            errors.append("Sources/参考资料章节没有直接来源 URL。")

        registry = evidence_registry if isinstance(evidence_registry, dict) else None
        sources = (registry or {}).get("sources", [])
        findings = (registry or {}).get("findings", [])
        if not isinstance(sources, list):
            sources = []
        if not isinstance(findings, list):
            findings = []

        cited_ids = tuple(sorted({value.upper() for value in SOURCE_ID_RE.findall(body_text)}))
        cited_findings = tuple(sorted({value.upper() for value in FINDING_ID_RE.findall(text)}))
        known = {
            str(item.get("source_id", "")).upper(): item
            for item in sources if isinstance(item, dict) and item.get("source_id")
        }
        registered_count = len(sources)
        verified_count = sum(
            bool(item.get("verified"))
            and bool(item.get("snapshot_path"))
            and bool(item.get("snapshot_exists", True))
            and bool(item.get("snapshot_integrity", True))
            for item in sources if isinstance(item, dict)
        )

        coverage = 0.0
        cited_valid: set[str] = set()
        if registry is not None:
            if not sources:
                errors.append("证据登记簿没有已登记来源；请先检查来源快照和登记结果。")
            if sources and not cited_ids:
                errors.append("已登记证据，但报告正文没有使用 [S0001] 格式的结论级来源引用。")

            unknown = [value for value in cited_ids if value not in known]
            if unknown:
                errors.append("报告引用了不存在的来源 ID：" + ", ".join(unknown))

            invalid = [
                value for value in cited_ids
                if value in known and (
                    not known[value].get("verified")
                    or not known[value].get("snapshot_path")
                    or not known[value].get("snapshot_exists", True)
                    or not known[value].get("snapshot_integrity", True)
                )
            ]
            if invalid:
                errors.append("部分引用来源未通过快照存在性/完整性核验：" + ", ".join(invalid))

            unlisted_urls = []
            for source_id in cited_ids:
                item = known.get(source_id)
                if not item:
                    continue
                url = _normalize_url(item.get("url", ""))
                if url and url not in source_urls:
                    unlisted_urls.append(f"{source_id} ({url})")
                if source_id not in invalid:
                    cited_valid.add(source_id)
            if unlisted_urls:
                errors.append("Sources 章节缺少正文引用来源的直接 URL：" + "; ".join(unlisted_urls))

            paragraphs = [
                part.strip()
                for part in re.split(r"\n\s*\n", body_text)
                if len(part.strip()) >= 80
                and not part.lstrip().startswith("#")
            ]
            if paragraphs:
                coverage = (
                    sum(
                        any(value.upper() in known and value.upper() in cited_valid
                            for value in SOURCE_ID_RE.findall(part))
                        for part in paragraphs
                    ) / len(paragraphs)
                )
                if coverage < 0.4:
                    errors.append(
                        f"结论级证据覆盖率不足（{coverage:.0%}）；请为主要事实和分析段落补充有效 [Sxxxx] 引用。"
                    )
            elif sources:
                errors.append("报告正文缺少可核验的结论段落，无法计算证据覆盖率。")

        known_findings = {
            str(item.get("finding_id", "")).upper()
            for item in findings if isinstance(item, dict) and item.get("finding_id")
        }
        unknown_findings = sorted(set(cited_findings) - known_findings)
        if unknown_findings:
            errors.append("报告引用了不存在的研究发现 ID：" + ", ".join(unknown_findings))

        source_count = registered_count if registry is not None else len(source_urls or all_urls)
        return ReportValidation(
            valid=not errors,
            errors=tuple(errors),
            source_count=source_count,
            word_count=len(text.split()),
            cited_source_ids=cited_ids,
            evidence_coverage=coverage,
            finding_count=len(findings),
            registered_source_count=registered_count,
            verified_source_count=verified_count,
            report_url_count=len(all_urls),
            cited_source_count=len(cited_valid),
            cited_finding_ids=cited_findings,
        )


def validation_feedback(result: ReportValidation) -> str:
    return (
        "交付前质量门禁未通过。请只针对下列问题补充或修订，不要重做已完成的研究：\n"
        + "\n".join(f"{index}. {error}" for index, error in enumerate(result.errors, 1))
        + "\n先读取 evidence/registry.json，并核对每个被引用来源的不可变快照；正文中的每个主要事实段落必须引用有效 [Sxxxx]，Sources 章节必须列出这些来源的直接 URL。"
        + "\n完成后使用 save_artifact 覆盖 report.md；不要只回复修订说明。"
    )

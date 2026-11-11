from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

URL_RE = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)
SOURCE_ID_RE = re.compile(r"\[(S\d{4})\]", re.IGNORECASE)
FINDING_ID_RE = re.compile(r"\[(F\d{4})\]", re.IGNORECASE)


@dataclass(frozen=True)
class ReportValidation:
    valid: bool
    errors: tuple[str, ...]
    source_count: int
    word_count: int
    cited_source_ids: tuple[str, ...] = field(default_factory=tuple)
    evidence_coverage: float = 0.0
    finding_count: int = 0


class ReportValidator:
    """Deterministic format and provenance checks; it does not claim to prove truth."""

    REQUIRED_SECTIONS = ("摘要", "结论", "sources", "参考资料", "参考来源", "参考文献")

    def validate(self, report: str, goal: str = "",
                 evidence_registry: dict[str, Any] | None = None) -> ReportValidation:
        text = (report or "").strip()
        errors: list[str] = []
        if len(text) < 300:
            errors.append("报告正文过短，缺少足以支撑研究结论的内容。")
        if not re.search(r"^#\s+\S+", text, re.MULTILINE):
            errors.append("缺少 Markdown 一级标题。")
        urls = set(URL_RE.findall(text))
        if not urls:
            errors.append("报告没有可核验的来源 URL，请补充来源链接及其支持的结论。")
        if not any(section in text.lower() for section in self.REQUIRED_SECTIONS):
            errors.append("缺少独立的来源/参考资料章节。")

        registry = evidence_registry or {}
        sources = registry.get("sources", [])
        findings = registry.get("findings", [])
        cited_ids = tuple(sorted({value.upper() for value in SOURCE_ID_RE.findall(text)}))
        cited_findings = {value.upper() for value in FINDING_ID_RE.findall(text)}
        coverage = 0.0
        if sources:
            known = {str(item.get("source_id", "")).upper(): item for item in sources}
            if not cited_ids:
                errors.append("已登记证据，但报告没有使用 [S0001] 格式的结论级来源引用。")
            unknown = [value for value in cited_ids if value not in known]
            if unknown:
                errors.append("报告引用了不存在的来源 ID：" + ", ".join(unknown))
            invalid = [
                value for value in cited_ids
                if value in known and (
                    not known[value].get("verified")
                    or not known[value].get("snapshot_path")
                )
            ]
            if invalid:
                errors.append("部分来源未通过读取核验或缺少快照：" + ", ".join(invalid))
            paragraphs = [
                part.strip() for part in re.split(r"\n\s*\n", text)
                if len(part.strip()) >= 80
                and not part.lstrip().startswith("#")
                and not part.lstrip().startswith("- ")
                and not part.lstrip().startswith("|")
            ]
            coverage = (
                sum(bool(SOURCE_ID_RE.search(part)) for part in paragraphs) / len(paragraphs)
                if paragraphs else 0.0
            )
            if paragraphs and coverage < 0.4:
                errors.append(f"结论级证据覆盖率不足（{coverage:.0%}）；请为主要事实段落补充 [Sxxxx] 引用。")
        if findings:
            known_findings = {str(item.get("finding_id", "")).upper() for item in findings}
            unknown_findings = sorted(cited_findings - known_findings)
            if unknown_findings:
                errors.append("报告引用了不存在的研究发现 ID：" + ", ".join(unknown_findings))

        return ReportValidation(
            valid=not errors, errors=tuple(errors), source_count=len(urls),
            word_count=len(text.split()), cited_source_ids=cited_ids,
            evidence_coverage=coverage, finding_count=len(findings),
        )


def validation_feedback(result: ReportValidation) -> str:
    return (
        "交付前质量门禁未通过。请只针对下列问题补充或修订，不要重做已完成的研究：\n"
        + "\n".join(f"{index}. {error}" for index, error in enumerate(result.errors, 1))
        + "\n检查 evidence/registry.json 与不可变来源快照，确保 [Sxxxx] 引用存在并支持对应段落，再保存 report.md。"
    )

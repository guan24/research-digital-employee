from __future__ import annotations

import re
from dataclasses import dataclass


URL_RE = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)


@dataclass(frozen=True)
class ReportValidation:
    valid: bool
    errors: tuple[str, ...]
    source_count: int
    word_count: int


class ReportValidator:
    """Deterministic quality gate for a research report; it does not judge factual truth."""

    REQUIRED_SECTIONS = ("摘要", "结论", "sources", "参考资料", "参考来源", "参考文献")

    def validate(self, report: str, goal: str = "") -> ReportValidation:
        text = (report or "").strip()
        errors: list[str] = []
        if len(text) < 300:
            errors.append("报告正文过短，缺少足以支撑研究结论的内容。")
        if not re.search(r"^#\s+\S+", text, re.MULTILINE):
            errors.append("缺少 Markdown 一级标题。")
        source_count = len(set(URL_RE.findall(text)))
        if source_count == 0:
            errors.append("报告没有可核验的来源 URL，请补充来源链接及其支持的结论。")
        if not any(section in text.lower() for section in self.REQUIRED_SECTIONS):
            errors.append("缺少独立的来源/参考资料章节。")
        return ReportValidation(not errors, tuple(errors), source_count, len(text.split()))


def validation_feedback(result: ReportValidation) -> str:
    return "报告质量检查未通过，请根据以下问题补充研究并重新生成 report.md：\n" + "\n".join(
        f"{index}. {error}" for index, error in enumerate(result.errors, 1)
    )

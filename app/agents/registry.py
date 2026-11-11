from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class AgentProfile:
    name: str
    role: str
    output_format: str
    max_steps: int
    system_prompt: str
    description: str = ""
    output_artifact: str = "result.md"


@dataclass(frozen=True)
class BudgetDefaults:
    max_steps: int = 30
    max_runtime_seconds: int = 3600
    max_total_tokens: int = 100_000
    max_cost_microusd: int | None = None


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentRegistration:
    agent_type: str
    builder: Callable[..., Any]
    profile_loader: Callable[[], AgentProfile] | None = None
    output_artifact: str = "result.md"
    validator: Callable[..., Any] | None = None
    postprocessor: Callable[..., Any] | None = None
    max_revisions: int = 0
    finalizer: Callable[..., Any] | None = None
    budget_defaults: BudgetDefaults = field(default_factory=BudgetDefaults)


def load_research_profile() -> AgentProfile:
    import tomllib
    profile_dir = Path(__file__).resolve().parent / "research"
    with (profile_dir / "profile.toml").open("rb") as handle:
        raw = tomllib.load(handle)
    return AgentProfile(
        name=raw["name"], role=raw["role"], output_format=raw["output_format"],
        max_steps=int(raw.get("max_steps", 12)),
        system_prompt=(profile_dir / "system_prompt.md").read_text(encoding="utf-8"),
        description=raw.get("description", raw["role"]),
        output_artifact=raw.get("output_artifact", "report.md"),
    )


_REGISTRY: dict[str, AgentRegistration] = {}


def register_agent(agent_type: str, builder: Callable[..., Any], *,
                   profile_loader: Callable[[], AgentProfile] | None = None,
                   output_artifact: str = "result.md",
                   validator: Callable[..., Any] | None = None,
                   postprocessor: Callable[..., Any] | None = None,
                   max_revisions: int = 0,
                   finalizer: Callable[..., Any] | None = None,
                   budget_defaults: BudgetDefaults | dict[str, int] | None = None,
                   replace: bool = False) -> None:
    key = str(agent_type or "").strip().lower()
    if not key:
        raise ValueError("agent_type cannot be empty")
    if key in _REGISTRY and not replace:
        raise ValueError(f"agent_type '{key}' is already registered")
    path = Path(output_artifact)
    if path.is_absolute() or ".." in path.parts or not output_artifact:
        raise ValueError("output_artifact must be a safe task-relative path")
    defaults = budget_defaults or BudgetDefaults()
    if isinstance(defaults, dict):
        defaults = BudgetDefaults(**defaults)
    _REGISTRY[key] = AgentRegistration(
        key, builder, profile_loader, output_artifact, validator,
        postprocessor, max(0, int(max_revisions)), finalizer, defaults
    )


def get_agent_registration(agent_type: str) -> AgentRegistration:
    key = (agent_type or "research").strip().lower()
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise ValueError(f"unknown agent_type '{key}'; registered: {', '.join(sorted(_REGISTRY))}") from exc


def get_agent_builder(agent_type: str) -> Callable[..., Any]:
    return get_agent_registration(agent_type).builder


def registered_agent_types() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def list_agent_profiles() -> list[dict[str, str]]:
    result = []
    for key, registration in sorted(_REGISTRY.items()):
        profile = registration.profile_loader() if registration.profile_loader else None
        result.append({
            "agent_type": key, "name": profile.name if profile else key,
            "role": profile.role if profile else key,
            "description": profile.description if profile else key,
            "output_format": profile.output_format if profile else "task artifact",
            "output_artifact": registration.output_artifact,
        })
    return result


def _build_research(settings, db, task, checkpointer, started):
    from app.agents.research.agent import build_research_agent
    return build_research_agent(settings, db, task, checkpointer, started)


def _validate_research(report: str, task: dict, artifacts):
    from app.infrastructure.evidence_registry import EvidenceRegistry
    from app.runtime.report_validator import ReportValidator
    return ReportValidator().validate(report, task.get("goal", ""), EvidenceRegistry(artifacts).summary())


def _postprocess_research(agent, result, config, task, artifacts, validation):
    from app.runtime.report_validator import validation_feedback
    feedback = validation_feedback(validation)
    agent.invoke({"messages": [{"role": "user", "content": feedback}]}, config=config)
    return None


def _finalize_research(db, task_id: str, artifacts, status: str, task: dict):
    from app.agents.research.lifecycle import finalize_research_task
    return finalize_research_task(db, task_id, artifacts, status, task)


register_agent(
    "research", _build_research, profile_loader=load_research_profile,
    output_artifact="report.md", validator=_validate_research,
    postprocessor=_postprocess_research, max_revisions=2,
    finalizer=_finalize_research,
    budget_defaults=BudgetDefaults(max_steps=30, max_runtime_seconds=3600, max_total_tokens=100_000),
)

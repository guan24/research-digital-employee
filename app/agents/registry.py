from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class AgentProfile:
    name: str
    role: str
    output_format: str
    max_steps: int
    system_prompt: str


def load_research_profile() -> AgentProfile:
    import tomllib

    profile_dir = Path(__file__).resolve().parent / "research"
    with (profile_dir / "profile.toml").open("rb") as handle:
        raw = tomllib.load(handle)
    prompt = (profile_dir / "system_prompt.md").read_text(encoding="utf-8")
    return AgentProfile(
        name=raw["name"],
        role=raw["role"],
        output_format=raw["output_format"],
        max_steps=int(raw.get("max_steps", 12)),
        system_prompt=prompt,
    )


_AGENT_BUILDERS: dict[str, Callable] = {}


def register_agent(agent_type: str, builder: Callable, *, replace: bool = False) -> None:
    key = agent_type.strip().lower()
    if not key:
        raise ValueError("agent_type cannot be empty")
    if key in _AGENT_BUILDERS and not replace:
        raise ValueError(f"agent_type '{key}' is already registered")
    _AGENT_BUILDERS[key] = builder


def get_agent_builder(agent_type: str) -> Callable:
    key = (agent_type or "research").strip().lower()
    try:
        return _AGENT_BUILDERS[key]
    except KeyError as exc:
        raise ValueError(f"unknown agent_type '{key}'; registered: {', '.join(sorted(_AGENT_BUILDERS))}") from exc


def registered_agent_types() -> tuple[str, ...]:
    return tuple(sorted(_AGENT_BUILDERS))


def _build_research(settings, db, task, checkpointer, started):
    from app.agents.research.agent import build_research_agent
    return build_research_agent(settings, db, task, checkpointer, started)


register_agent("research", _build_research)

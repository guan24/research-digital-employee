from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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

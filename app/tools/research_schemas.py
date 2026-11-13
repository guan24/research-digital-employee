"""Contracts shared by model-facing tools and the plan persistence boundary."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonEmpty = Annotated[str, Field(min_length=1)]


class ResearchDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: Annotated[str, Field(min_length=1, max_length=64)]
    title: NonEmpty
    questions: list[str]
    expected_evidence: list[str]
    priority: Literal["critical", "high", "normal"]
    min_sources: Annotated[int, Field(strict=True, ge=1, le=10)]


class ResearchQualityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_total_score: Annotated[float, Field(ge=0, le=100)] | None = None
    min_dimension_score: Annotated[float, Field(ge=0, le=100)] | None = None


class ResearchRequirementsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimensions: Annotated[list[ResearchDimension], Field(min_length=3, max_length=12)]
    quality_policy: ResearchQualityPolicy | None = None


class ResearchPlanStep(BaseModel):
    # Execution telemetry is stripped before strategic plan versioning.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: NonEmpty
    title: NonEmpty
    objective: NonEmpty
    expected_output: NonEmpty
    phase: str | None = None
    dependencies: list[str] | None = None
    expected_output_path: str | None = None


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: NonEmpty
    objective: NonEmpty
    steps: Annotated[list[ResearchPlanStep], Field(min_length=3, max_length=8)]

    @model_validator(mode="after")
    def unique_step_ids(self):
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("plan step IDs must be unique")
        return self


class ResearchPlanInput(ResearchPlan):
    change_reason: str = ""
    trigger: str = ""
    new_information: str = ""
    problem_analysis: str = ""
    decision: str = ""
    changes: str = ""

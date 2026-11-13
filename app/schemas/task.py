from __future__ import annotations

from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field, ConfigDict


class TaskCreate(BaseModel):
    goal: str = Field(min_length=10, max_length=12_000)
    agent_type: str = Field(default="research", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    max_steps: int | None = Field(default=None, ge=2, le=100)
    max_runtime_seconds: int | None = Field(default=None, ge=60, le=86_400)
    budget_enabled: bool = True
    max_total_tokens: int | None = Field(default=None, ge=1000, le=5_000_000)
    max_attempts: int = Field(default=3, ge=1, le=5)


class TaskOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    is_example: bool = False
    example_description: str | None = None
    goal: str
    agent_type: str = "research"
    runtime_seconds_used: float = 0
    execution_steps: int = 0
    status: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    worker_id: str | None = None
    heartbeat_at: float | None = None
    next_run_at: float | None = None
    resume_mode: bool = False
    pause_requested: bool = False
    cancel_requested: bool = False
    attempts: int
    max_attempts: int
    max_steps: int
    max_runtime_seconds: int
    budget_enabled: bool = True
    max_total_tokens: int = 100_000
    total_tokens_used: int = 0
    worker_generation: int = 0
    last_error: str | None = None
    error_category: str | None = None
    error_code: str | None = None
    first_error_json: str | None = None
    error_history_json: str = "[]"
    result_path: str | None = None


class EventOut(BaseModel):
    event_id: int
    task_id: str
    timestamp: str
    event_type: str
    payload: dict


class TaskRetry(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class TaskRetryOut(BaseModel):
    task_id: str
    status: str
    checkpoint_reuse_requested: bool
    workspace_preserved: bool
    previous_attempts: int
    message: str


class HumanInterventionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    intervention_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    # Natural-language instruction from the operator; internal schemas stay inside the Agent.
    instruction: str = Field(default="", max_length=10000)
    reason: str = Field(default="", max_length=1200)
    # Temporary compatibility for clients using the former direction-only field.
    direction: str = Field(default="", max_length=3000)
    confirm: bool = False


class BudgetExtensionApproval(BaseModel):
    reason: str = Field(min_length=3, max_length=1200)
    additional_steps: int = Field(default=0, ge=0, le=100)
    additional_runtime_seconds: int = Field(default=0, ge=0, le=86400)
    additional_tokens: int = Field(default=0, ge=0, le=5_000_000)

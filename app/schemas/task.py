from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


class TaskCreate(BaseModel):
    goal: str = Field(min_length=10, max_length=12_000)
    max_steps: int = Field(default=12, ge=2, le=40)
    max_runtime_seconds: int = Field(default=3600, ge=60, le=14_400)
    max_attempts: int = Field(default=3, ge=1, le=5)


class TaskOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str
    goal: str
    status: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    attempts: int
    max_attempts: int
    max_steps: int
    max_runtime_seconds: int
    last_error: str | None = None
    result_path: str | None = None


class EventOut(BaseModel):
    event_id: int
    task_id: str
    timestamp: str
    event_type: str
    payload: dict

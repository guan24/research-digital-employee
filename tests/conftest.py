from __future__ import annotations

from pathlib import Path
import pytest

from app.config import Settings
from app.infrastructure.database import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_home=tmp_path,
        database_path=tmp_path / "data" / "tasks.sqlite",
        checkpoint_path=tmp_path / "data" / "checkpoints.sqlite",
        workspace_dir=tmp_path / "workspaces",
        openai_api_key="test-key",
        openai_base_url=None,
        openai_model="test-model",
        model_timeout_seconds=5,
        tavily_api_key="",
        search_timeout_seconds=5,
        fetch_timeout_seconds=5,
        max_fetch_bytes=100_000,
        worker_poll_seconds=0.01,
        worker_heartbeat_seconds=0.01,
        worker_lease_seconds=1,
        retry_base_seconds=1,
        retry_max_seconds=2,
        default_max_attempts=3,
        default_max_steps=12,
        default_max_runtime_seconds=60,
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.init()
    return database

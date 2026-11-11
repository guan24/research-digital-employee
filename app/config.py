from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    app_home: Path
    database_path: Path
    checkpoint_path: Path
    workspace_dir: Path
    openai_api_key: str
    openai_base_url: str | None
    openai_model: str
    model_timeout_seconds: float
    tavily_api_key: str
    search_timeout_seconds: float
    fetch_timeout_seconds: float
    max_fetch_bytes: int
    worker_poll_seconds: float
    worker_heartbeat_seconds: float
    worker_lease_seconds: int
    retry_base_seconds: int
    retry_max_seconds: int
    default_max_attempts: int
    default_max_steps: int
    default_max_runtime_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        app_home = Path(os.getenv("APP_HOME", Path(__file__).resolve().parents[1])).expanduser().resolve()
        data_dir = Path(os.getenv("DATA_DIR", app_home / "data")).expanduser().resolve()
        workspace_dir = Path(os.getenv("WORKSPACE_DIR", app_home / "workspaces")).expanduser().resolve()
        return cls(
            app_home=app_home,
            database_path=Path(os.getenv("DATABASE_PATH", data_dir / "tasks.sqlite")).expanduser().resolve(),
            checkpoint_path=Path(os.getenv("CHECKPOINT_PATH", data_dir / "checkpoints.sqlite")).expanduser().resolve(),
            workspace_dir=workspace_dir,
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip() or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
            model_timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "90")),
            tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip(),
            search_timeout_seconds=float(os.getenv("SEARCH_TIMEOUT_SECONDS", "30")),
            fetch_timeout_seconds=float(os.getenv("FETCH_TIMEOUT_SECONDS", "25")),
            max_fetch_bytes=int(os.getenv("MAX_FETCH_BYTES", str(2 * 1024 * 1024))),
            worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "2")),
            worker_heartbeat_seconds=float(os.getenv("WORKER_HEARTBEAT_SECONDS", "10")),
            worker_lease_seconds=int(os.getenv("WORKER_LEASE_SECONDS", "45")),
            retry_base_seconds=int(os.getenv("RETRY_BASE_SECONDS", "5")),
            retry_max_seconds=int(os.getenv("RETRY_MAX_SECONDS", "120")),
            default_max_attempts=int(os.getenv("DEFAULT_MAX_ATTEMPTS", "3")),
            default_max_steps=int(os.getenv("DEFAULT_MAX_STEPS", "12")),
            default_max_runtime_seconds=int(os.getenv("DEFAULT_MAX_RUNTIME_SECONDS", "3600")),
        )

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

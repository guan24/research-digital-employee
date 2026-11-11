from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import Settings
from app.infrastructure.database import Database


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    settings.ensure_directories()
    db = Database(settings.database_path)
    db.init()
    app.state.settings = settings
    app.state.db = db
    yield


app = FastAPI(
    title="Research Digital Employee MVP",
    description="Long-running autonomous research agent with task persistence and checkpoint recovery.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.api.task_examples import router as examples_router
from app.config import Settings
from app.infrastructure.database import Database


STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


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
    version="0.2.0",
    lifespan=lifespan,
)
app.include_router(examples_router)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def frontend():
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return JSONResponse(
        {
            "message": "Research Digital Employee API is running.",
            "frontend": "Run 'cd frontend && npm install && npm run dev' for the development console.",
            "frontend_dev_url": "http://127.0.0.1:5173",
            "api_docs": "/docs",
        }
    )

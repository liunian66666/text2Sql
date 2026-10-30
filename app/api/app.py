"""Compose API routers and the local static demonstration frontend."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import create_chat_router, create_database_router, create_evaluation_router
from app.api.schemas import HealthResponse


def create_app(*, manager, registry, evaluations, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Enterprise Data Analyst Agent", version="2.0")
    static_dir = static_dir or Path(__file__).resolve().parents[1] / "demo" / "static"

    @app.get("/api/health", response_model=HealthResponse, tags=["system"])
    def health():
        return HealthResponse()

    app.include_router(create_chat_router(manager), prefix="/api")
    app.include_router(create_database_router(registry), prefix="/api")
    app.include_router(create_evaluation_router(evaluations), prefix="/api")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "chat.html")

    @app.middleware("http")
    async def no_store_static(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app

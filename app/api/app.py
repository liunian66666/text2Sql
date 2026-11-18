"""Compose API routers and the local static web frontend."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import (create_chat_router, create_database_router,
                            create_evaluation_router, create_knowledge_router)
from app.api.schemas import HealthResponse


def create_app(*, manager, registry, evaluations, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Enterprise Data Analyst Agent", version="2.0")
    static_dir = static_dir or Path(__file__).resolve().parents[1] / "web" / "static"
    project_root = Path(__file__).resolve().parents[2]

    @app.get("/api/health", response_model=HealthResponse, tags=["system"])
    def health():
        return HealthResponse()

    app.include_router(create_chat_router(manager), prefix="/api")
    app.include_router(create_database_router(registry), prefix="/api")
    app.include_router(create_evaluation_router(evaluations), prefix="/api")
    # 只读知识资产 / 数据源状态；仓库在装配期构造，不改变既有 create_app 签名。
    from app.web.knowledge import KnowledgeOverviewRepository, SystemStatusRepository
    app.include_router(create_knowledge_router(
        KnowledgeOverviewRepository(project_root),
        SystemStatusRepository(project_root, registry),
    ), prefix="/api")

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

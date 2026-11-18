"""Read-only knowledge-asset and system-status routes (no secrets, no paths)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException


def create_knowledge_router(overview, status) -> APIRouter:
    router = APIRouter(tags=["knowledge"])

    @router.get("/knowledge/overview")
    def knowledge_overview():
        """Aggregate counts for the read-only enterprise knowledge page."""
        try:
            return overview.overview()
        except Exception as exc:  # 缺产物时降级为空态，而不是 500 打崩页面
            raise HTTPException(status_code=503, detail=f"知识产物不可读取: {type(exc).__name__}") from exc

    @router.get("/knowledge/metrics")
    def knowledge_metrics():
        try:
            return {"metrics": overview.metrics()}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"知识产物不可读取: {type(exc).__name__}") from exc

    @router.get("/knowledge/schema")
    def knowledge_schema():
        try:
            return {"databases": overview.schema_dictionary()}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"知识产物不可读取: {type(exc).__name__}") from exc

    @router.get("/knowledge/relations")
    def knowledge_relations():
        try:
            return {"databases": overview.relations()}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"知识产物不可读取: {type(exc).__name__}") from exc

    @router.get("/system/status")
    def system_status():
        """Data-source registration / readiness facts for the sidebar."""
        return status.status()

    return router

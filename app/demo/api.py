from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.skills import SQLSkillRequest

from .serializers import EvaluationRepository


def create_app(*, manager, registry, evaluations: EvaluationRepository, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="DeepEye-SQL Demo", version="1.0")
    static_dir = static_dir or Path(__file__).with_name("static")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/databases")
    def databases():
        return {"databases": registry.public_entries()}

    @app.post("/api/query", status_code=202)
    def create_query(request: SQLSkillRequest):
        try:
            task = manager.submit(request)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"request_id": task["id"], "status": task["status"]}

    @app.get("/api/query/{request_id}")
    def query_result(request_id: str):
        try:
            return manager.get(request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown request ID") from exc

    @app.get("/api/query/{request_id}/events")
    async def query_events(request_id: str, after: int = Query(default=0, ge=0)):
        try:
            manager.get(request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown request ID") from exc

        async def stream():
            cursor = after
            while True:
                events, terminal = await asyncio.to_thread(manager.wait_for_events, request_id, cursor, 15.0)
                if not events:
                    yield ": keep-alive\n\n"
                for event in events:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if terminal:
                    break

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/evaluations")
    def evaluation_list():
        return {"evaluations": evaluations.list()}

    @app.get("/api/evaluations/{evaluation_id:path}")
    def evaluation_detail(evaluation_id: str):
        try:
            return evaluations.get(evaluation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown evaluation ID") from exc

    @app.get("/")
    def index():
        return FileResponse(static_dir / "chat.html")

    # Local single-user demo: never let the browser serve a stale chat.js. An
    # edited frontend otherwise looks unchanged until a hard refresh, which
    # silently masks both real fixes and real regressions during a rehearsal.
    # (StaticFiles in this Starlette version has no `headers=` argument.)
    @app.middleware("http")
    async def no_store_static(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app

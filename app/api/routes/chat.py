"""Conversational task submission, status and progress routes."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.agent import ChatRequest
from app.api.schemas import TaskAccepted


def create_chat_router(manager) -> APIRouter:
    router = APIRouter(tags=["chat"])

    @router.post("/chat", response_model=TaskAccepted, status_code=202)
    @router.post("/query", response_model=TaskAccepted, status_code=202, include_in_schema=False)
    def create_turn(request: ChatRequest):
        try:
            task = manager.submit(request)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return TaskAccepted(
            request_id=task["id"], status=task["status"],
            conversation_id=task["request"]["conversation_id"],
        )

    @router.get("/conversations")
    def list_conversations(limit: int = Query(default=50, ge=1, le=200)):
        return {"conversations": manager.list_conversations(limit)}

    @router.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str):
        try:
            return manager.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown conversation ID") from exc

    @router.delete("/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: str):
        try:
            manager.delete_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown conversation ID") from exc

    # 必须声明在 /query/{request_id} 之前，否则 "active" 会被当成请求 ID。
    @router.get("/query/active")
    def active_query():
        """当前是否还有排队/执行中的任务；新标签页靠它决定要不要接手。"""
        return {"task": manager.active()}

    @router.get("/query/{request_id}")
    def query_result(request_id: str):
        try:
            return manager.get(request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown request ID") from exc

    @router.get("/query/{request_id}/events")
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

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    return router

"""Read-only evaluation report routes."""
from fastapi import APIRouter, HTTPException


def create_evaluation_router(evaluations) -> APIRouter:
    router = APIRouter(tags=["evaluations"])

    @router.get("/evaluations")
    def evaluation_list():
        return {"evaluations": evaluations.list()}

    @router.get("/evaluations/{evaluation_id:path}")
    def evaluation_detail(evaluation_id: str):
        try:
            return evaluations.get(evaluation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown evaluation ID") from exc

    return router

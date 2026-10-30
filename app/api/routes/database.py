"""Registered-database discovery routes."""
from fastapi import APIRouter


def create_database_router(registry) -> APIRouter:
    router = APIRouter(tags=["databases"])

    @router.get("/databases")
    def databases():
        return {"databases": registry.public_entries()}

    return router

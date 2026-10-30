"""Public API response models."""
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str = "ok"


class TaskAccepted(BaseModel):
    request_id: str
    status: str
    conversation_id: str

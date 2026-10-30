"""Route factories with dependencies supplied by the application assembly."""

from .chat import create_chat_router
from .database import create_database_router
from .evaluation import create_evaluation_router

__all__ = ["create_chat_router", "create_database_router", "create_evaluation_router"]

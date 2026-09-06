from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class FactoryEvent(BaseModel):
    event_type: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    project_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


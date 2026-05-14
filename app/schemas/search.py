from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SearchTraceItem(BaseModel):
    entity_type: str
    entity_id: str
    title: str
    score: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


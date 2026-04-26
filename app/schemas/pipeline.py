from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PipelineRunRequest(BaseModel):
    user_email: str
    lookback_hours: int = Field(default=24, ge=1, le=168)
    delivery_channel: str = "db"
    force_fetch: bool = False


class SummarySectionItem(BaseModel):
    title: str
    summary: str
    source: str
    people: list[str] = Field(default_factory=list)
    needs_action: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SummaryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    summary_date: date
    period_start: datetime
    period_end: datetime
    human_readable: str
    summary_json: dict[str, Any]

class PipelineRunResponse(BaseModel):
    summary_id: str
    summary_date: str
    counts: dict[str, int]
    summary: dict[str, Any]

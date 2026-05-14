from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class ActionApprovalRequest(BaseModel):
    approved_by_person_id: str | None = None
    execute: bool = False


class ActionRejectRequest(BaseModel):
    rejected_by_person_id: str | None = None
    reason: str = Field(default="Rejected by reviewer", min_length=1, max_length=1000)


class ActionCancelRequest(BaseModel):
    actor_person_id: str | None = None
    reason: str = Field(default="Canceled", min_length=1, max_length=1000)


class ActionEditRequest(BaseModel):
    payload: dict[str, Any]
    actor_person_id: str | None = None

    @field_validator("payload")
    @classmethod
    def require_non_empty_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("payload cannot be empty")
        return value


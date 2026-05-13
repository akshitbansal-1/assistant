from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator


class ExtractedCommitment(BaseModel):
    owner: str | None = None
    requester: str | None = None
    task_title: str
    jira_key: str | None = None
    project: str | None = None
    commitment_text: str
    source_system: str
    source_url: str | None = None
    source_message_id: str | None = None
    due_date: date | None = None
    status: str = "open"
    needs_follow_up: bool = False
    jira_appears_stale: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("status")
    @classmethod
    def normalize_status(cls, value: str) -> str:
        normalized = (value or "open").strip().lower()
        allowed = {"open", "done", "blocked", "stale", "suggestion"}
        return normalized if normalized in allowed else "suggestion"

    @field_validator("task_title", "commitment_text", "source_system")
    @classmethod
    def require_text(cls, value: str) -> str:
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError("required text field is empty")
        return stripped


class CommitmentExtractionResult(BaseModel):
    commitments: list[ExtractedCommitment] = Field(default_factory=list)

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class InviteUserRequest(BaseModel):
    email: str
    name: str | None = Field(default=None, max_length=255)
    role: str = Field(default="member", pattern="^(admin|manager|member)$")
    manager_email: str | None = None

    @field_validator("email", "manager_email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if "@" not in normalized:
            raise ValueError("email must contain @")
        return normalized


class AcceptInviteRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)

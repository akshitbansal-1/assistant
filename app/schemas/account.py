from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.constants import SourceType


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: str | None = None


class AccountCreate(BaseModel):
    user_email: str
    source: SourceType
    label: str
    account_identifier: str
    access_token: str | None = None
    user_access_token: str | None = None
    refresh_token: str | None = None
    user_refresh_token: str | None = None
    token_type: str | None = None
    expires_at: datetime | None = None
    user_expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    source: str
    label: str
    account_identifier: str
    expires_at: datetime | None = None
    user_expires_at: datetime | None = None
    metadata_json: dict[str, Any]
    is_active: bool

class OAuthStartResponse(BaseModel):
    authorization_url: str
    state: str


class OAuthCallbackRequest(BaseModel):
    code: str
    state: str | None = None
    user_email: str
    label: str | None = None
    account_identifier: str | None = None

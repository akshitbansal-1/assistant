import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base
from app.utils.datetime import utcnow


def json_type():
    return JSONB().with_variant(JSON(), "sqlite")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    linked_accounts: Mapped[list["LinkedAccount"]] = relationship(back_populates="user")


class UserInvitation(Base):
    __tablename__ = "user_invitations"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", "status", name="uq_active_invite_per_email"),
        Index("ix_user_invitations_org_status", "organization_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(50), default="member")
    manager_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    token: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True)
    invited_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LinkedAccount(Base):
    __tablename__ = "linked_accounts"
    __table_args__ = (
        UniqueConstraint("source", "user_id", "account_identifier", name="uq_account_per_user"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    label: Mapped[str] = mapped_column(String(255))
    account_identifier: Mapped[str] = mapped_column(String(255))
    access_token: Mapped[str] = mapped_column(Text, nullable=True)
    user_access_token: Mapped[str] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=True)
    user_refresh_token: Mapped[str] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(String(50), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    user_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="linked_accounts")

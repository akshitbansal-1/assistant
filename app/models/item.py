import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base
from app.utils.datetime import utcnow


def json_type():
    return JSONB().with_variant(JSON(), "sqlite")


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint("source", "account_id", "external_id", name="uq_external_item"),
        Index("ix_work_items_user_timestamp", "user_id", "timestamp"),
        Index("ix_work_items_user_dedupe_key", "user_id", "dedupe_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("linked_accounts.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    people_json: Mapped[list] = mapped_column(json_type(), default=list)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), index=True)
    classification: Mapped[str] = mapped_column(String(50), nullable=True)
    needs_action: Mapped[bool] = mapped_column(Boolean, default=False)
    who_should_act: Mapped[str] = mapped_column(String(255), nullable=True)
    short_summary: Mapped[str] = mapped_column(String(500), nullable=True)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

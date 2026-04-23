import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base
from app.utils.datetime import utcnow


def json_type():
    return JSONB().with_variant(JSON(), "sqlite")


class KnownEntity(Base):
    __tablename__ = "known_entities"
    __table_args__ = (Index("ix_known_entities_user_name", "user_id", "name"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    entity_type: Mapped[str] = mapped_column(String(50), default="person")
    aliases_json: Mapped[list] = mapped_column(json_type(), default=list)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TrackedTask(Base):
    __tablename__ = "tracked_tasks"
    __table_args__ = (Index("ix_tracked_tasks_user_key", "user_id", "canonical_key"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    canonical_key: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(50), default="open")
    people_json: Mapped[list] = mapped_column(json_type(), default=list)
    source_refs_json: Mapped[list] = mapped_column(json_type(), default=list)
    latest_summary: Mapped[str] = mapped_column(String(500), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

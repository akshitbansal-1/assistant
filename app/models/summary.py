import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base
from app.utils.datetime import utcnow


def json_type():
    return JSONB().with_variant(JSON(), "sqlite")


class DailySummary(Base):
    __tablename__ = "daily_summaries"
    __table_args__ = (
        UniqueConstraint("user_id", "summary_date", name="uq_user_summary_date"),
        Index("ix_daily_summaries_user_date", "user_id", "summary_date"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    summary_date: Mapped[date] = mapped_column(Date)
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    summary_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    human_readable: Mapped[str] = mapped_column(Text)
    delivery_channel: Mapped[str] = mapped_column(String(50), default="db")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

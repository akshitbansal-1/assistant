"""
UserFeedback model — persists thumbs up/down votes against AI-generated outputs.

Each record stores:
- which entity type and ID was rated (task assignment, follow-up routing, etc.)
- whether the feedback was positive (👍) or negative (👎)
- an optional free-text correction for negative feedback
- the snapshot of context that was fed to the LLM and the output it produced,
  used for dynamic few-shot correction injection on future LLM calls
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base
from app.utils.datetime import utcnow


def json_type():
    return JSONB().with_variant(JSON(), "sqlite")


# Allowed output categories that can carry feedback
FEEDBACK_ENTITY_TYPES = frozenset(
    {
        "task_assignment",       # Was this task correctly assigned to the right person?
        "commitment_extraction", # Was this commitment extracted correctly from context?
        "follow_up_routing",     # Was this follow-up routed to the right person?
        "whereis_answer",        # Was this /whereis status answer correct?
        "stale_alert",           # Was this stale-Jira alert valid and actionable?
        "jira_draft",            # Was this Jira update draft accurate?
    }
)


class UserFeedback(Base):
    """Records a single thumbs-up or thumbs-down vote from a human on an AI output.

    Negative feedback with a correction teaches the model what not to do next time.
    The ``context_json`` field stores the raw LLM input + output so the relearning
    service can inject a ``CRITICAL MISTAKES TO AVOID`` block into future prompts.
    """

    __tablename__ = "user_feedback"
    __table_args__ = (
        Index("ix_user_feedback_org_entity", "organization_id", "entity_type", "entity_id"),
        Index("ix_user_feedback_org_negative", "organization_id", "is_positive"),
        Index("ix_user_feedback_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    # What kind of AI output was rated, and which DB record it corresponds to.
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_id: Mapped[str] = mapped_column(String, index=True)

    # Core vote.
    is_positive: Mapped[bool] = mapped_column(Boolean, default=True)

    # Optional human correction (only meaningful for negative votes).
    # Example: "The task was assigned to Carol, not Bob."
    correction_text: Mapped[str] = mapped_column(Text, nullable=True)

    # Short question that was surfaced to the user alongside the vote buttons.
    # Example: "Is this task assignment correct?"
    prompt_question: Mapped[str] = mapped_column(String(500), nullable=True)

    # Snapshot of the LLM context (inputs) and the output that was rated.
    # Used by the relearning service to build few-shot correction examples.
    context_json: Mapped[dict] = mapped_column(json_type(), default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

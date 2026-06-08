"""
FeedbackService — stores thumbs-up/down votes and retrieves correction examples
for dynamic few-shot prompt injection (the relearning loop).

Design:
- store()     saves a UserFeedback record. Negative votes with corrections are
              the inputs to the relearning system.
- corrections_for() returns the N most-recent negative corrections for a given
              entity_type, ready to be passed to LLMService._build_corrected_system_prompt.

This service never modifies existing task / commitment records directly —
side-effects on task memory happen only through the normal CommunicationLoopService
write path.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.models.communication import Organization, OrganizationMember
from app.models.feedback import FEEDBACK_ENTITY_TYPES, UserFeedback
from app.utils.datetime import utcnow


logger = logging.getLogger(__name__)

# Prompt questions surfaced to the user alongside each entity type.
FEEDBACK_QUESTIONS: dict[str, str] = {
    "task_assignment":        "Is this task correctly assigned to the right person?",
    "commitment_extraction":  "Was this commitment extracted correctly?",
    "follow_up_routing":      "Is this follow-up correctly routed?",
    "whereis_answer":         "Is this status answer accurate?",
    "stale_alert":            "Is this stale-ticket alert valid and actionable?",
    "jira_draft":             "Is this Jira update draft accurate?",
}

# Maximum number of negative corrections returned per entity_type for injection.
_MAX_CORRECTIONS = 5


class FeedbackService:
    """Records user feedback votes and exposes correction examples for relearning."""

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def store(
        self,
        db: Session,
        *,
        organization_id: str,
        user_id: str,
        entity_type: str,
        entity_id: str,
        is_positive: bool,
        correction_text: str | None = None,
        context_snapshot: str = "",
        incorrect_output: str = "",
    ) -> UserFeedback:
        """Persist a thumbs-up or thumbs-down vote.

        For negative votes, ``correction_text`` is stored alongside a compact
        snapshot of the LLM context and the incorrect output it produced.
        Together these form one "mistake example" that will be injected into
        future prompts of the same entity_type.

        Args:
            context_snapshot: The text that was fed to the LLM (e.g. the Slack message
                or Jira item that triggered the extraction/assignment).
            incorrect_output: The LLM's output that the user marked wrong
                (e.g. the commitment text or person name it produced).
        """
        if entity_type not in FEEDBACK_ENTITY_TYPES:
            raise ValueError(
                f"Unknown feedback entity_type '{entity_type}'. "
                f"Valid types: {sorted(FEEDBACK_ENTITY_TYPES)}"
            )
        context_json: dict[str, Any] = {}
        if not is_positive:
            context_json = {
                "context_snapshot": context_snapshot[:800],
                "incorrect_output": incorrect_output[:400],
                "correction_text": (correction_text or "").strip()[:400],
            }

        feedback = UserFeedback(
            organization_id=organization_id,
            user_id=user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            is_positive=is_positive,
            correction_text=(correction_text or "").strip() or None,
            prompt_question=FEEDBACK_QUESTIONS.get(entity_type),
            context_json=context_json,
        )
        db.add(feedback)
        db.flush()
        logger.info(
            "Feedback stored org=%s user=%s entity_type=%s entity_id=%s is_positive=%s",
            organization_id,
            user_id,
            entity_type,
            entity_id,
            is_positive,
        )
        return feedback

    # ------------------------------------------------------------------
    # Relearning read path
    # ------------------------------------------------------------------

    def corrections_for(
        self,
        db: Session,
        *,
        organization_id: str,
        entity_type: str,
        limit: int = _MAX_CORRECTIONS,
    ) -> list[dict[str, Any]]:
        """Return the most-recent negative correction dicts for an entity type.

        These are ready to be passed directly to
        ``LLMService._build_corrected_system_prompt`` via the ``corrections`` arg.
        Each dict has keys: context_snapshot, incorrect_output, correction_text.
        """
        rows = (
            db.query(UserFeedback)
            .filter(
                UserFeedback.organization_id == organization_id,
                UserFeedback.entity_type == entity_type,
                UserFeedback.is_positive.is_(False),
                UserFeedback.correction_text.isnot(None),
            )
            .order_by(desc(UserFeedback.created_at))
            .limit(limit)
            .all()
        )
        # Reverse so oldest correction comes first (the prompt block reads top-to-bottom).
        corrections = []
        for row in reversed(rows):
            ctx = row.context_json or {}
            corrections.append(
                {
                    "context_snapshot": ctx.get("context_snapshot") or "",
                    "incorrect_output": ctx.get("incorrect_output") or "",
                    "correction_text": row.correction_text or "",
                }
            )
        logger.info(
            "Corrections fetched org=%s entity_type=%s count=%d",
            organization_id,
            entity_type,
            len(corrections),
        )
        return corrections

    # ------------------------------------------------------------------
    # Stats helper (used by the dashboard to display vote tallies)
    # ------------------------------------------------------------------

    def summary_for_entity(
        self,
        db: Session,
        *,
        organization_id: str,
        entity_type: str,
        entity_id: str,
    ) -> dict[str, Any]:
        """Return positive and negative vote counts for a single entity."""
        rows = (
            db.query(UserFeedback)
            .filter(
                UserFeedback.organization_id == organization_id,
                UserFeedback.entity_type == entity_type,
                UserFeedback.entity_id == entity_id,
            )
            .all()
        )
        thumbs_up = sum(1 for r in rows if r.is_positive)
        thumbs_down = sum(1 for r in rows if not r.is_positive)
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "thumbs_up": thumbs_up,
            "thumbs_down": thumbs_down,
            "question": FEEDBACK_QUESTIONS.get(entity_type, ""),
        }

"""
Verification Agent — cross-checks human status claims against external sources.

When a person posts in Slack "I finished the auth migration PR" the agent:
1. Fetches the linked Jira ticket or GitHub PR status.
2. Asks the LLM to compare the claim against the external data.
3. Updates the task confidence score, appends a verified citation, or flags the
   claim as a low-confidence suggestion stored in ActionProposal.

This service is intentionally read-only against external connectors and only
writes within the trust boundary (memory updates + low-confidence suggestions).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.jira import JiraConnector
from app.llm.service import LLMService
from app.models.account import LinkedAccount, User
from app.models.communication import (
    ActionProposal,
    CommunicationTask,
    MemoryEvent,
    TaskStatusSnapshot,
)
from app.services.actions import ActionProposalService
from app.services.agent_runs import AgentRunRecorder
from app.utils.datetime import utcnow


logger = logging.getLogger(__name__)

# Minimum confidence to accept a claim as verified without external data.
_UNVERIFIED_MIN_CONFIDENCE = 0.50
_VERIFIED_CONFIDENCE_BOOST = 0.20
_MAX_VERIFIED_CONFIDENCE = 0.97


class VerificationService:
    """Verifies human status claims against external system data.

    Usage:
        result = VerificationService().verify_task_claim(
            db, user,
            task=task,
            claimed_status="I merged the PR yesterday.",
            source_context="Slack thread C123/T456",
        )
    """

    def __init__(self) -> None:
        self.llm = LLMService()
        self.actions = ActionProposalService()
        self.agent_runs = AgentRunRecorder()

    def verify_task_claim(
        self,
        db: Session,
        user: User,
        *,
        task: CommunicationTask,
        claimed_status: str,
        source_context: str = "",
    ) -> dict[str, Any]:
        """Verify a claimed task status update against linked external sources.

        Returns a summary dict with keys:
            verified, confidence, discrepancy, action (updated | suggestion | skipped)
        """
        if not task.jira_key:
            logger.info(
                "Verification skipped — no Jira key task=%s", task.id
            )
            return {"verified": False, "action": "skipped", "reason": "no_jira_key"}
        run = self.agent_runs.start(
            db,
            organization_id=task.organization_id,
            user_id=task.user_id,
            task_id=task.id,
            person_id=task.owner_person_id,
            agent_name="verification",
            input_payload={
                "jira_key": task.jira_key,
                "claimed_status": claimed_status[:600],
                "source_context": source_context[:300],
            },
            source_system="jira",
        )

        # Step 1: Fetch current external state.
        external_data = self._fetch_jira_state(db, user, task.jira_key)

        # Step 2: Ask the LLM to compare claim vs. external reality.
        verdict = self.llm.verify_claim(
            claimed_status=claimed_status,
            external_data=external_data or {},
            source_context=source_context,
        )

        verified: bool = bool(verdict.get("verified"))
        confidence: float = float(verdict.get("confidence") or 0)
        discrepancy: str | None = verdict.get("discrepancy")
        suggested_status: str | None = verdict.get("suggested_status")

        logger.info(
            "Claim verification result task=%s jira_key=%s verified=%s confidence=%.2f",
            task.id,
            task.jira_key,
            verified,
            confidence,
        )

        if verified and confidence >= 0.70:
            # Boost task confidence and update status if external data confirms.
            old_confidence = task.confidence or 0.0
            task.confidence = min(
                _MAX_VERIFIED_CONFIDENCE,
                old_confidence + _VERIFIED_CONFIDENCE_BOOST,
            )
            task.latest_status = claimed_status[:1000]
            task.last_human_update_at = utcnow()
            if external_data:
                citation = {
                    "source": "jira",
                    "title": f"{task.jira_key} — verified",
                    "url": external_data.get("source_url"),
                    "external_id": task.jira_key,
                    "timestamp": utcnow().isoformat(),
                    "verified": True,
                }
                citations = list(task.source_citations_json or [])
                citations.append(citation)
                task.source_citations_json = citations[-12:]
            db.add(
                TaskStatusSnapshot(
                    task_id=task.id,
                    latest_known_status=claimed_status[:1000],
                    owner_person_id=task.owner_person_id,
                    linked_jira_ticket=task.jira_key,
                    source_citations_json=task.source_citations_json or [],
                    confidence=task.confidence,
                    last_human_update_at=task.last_human_update_at,
                )
            )
            db.add(
                MemoryEvent(
                    organization_id=task.organization_id,
                    user_id=task.user_id,
                    task_id=task.id,
                    event_type="claim.verified",
                    payload_json={
                        "claimed_status": claimed_status,
                        "verdict": verdict,
                        "confidence_delta": task.confidence - old_confidence,
                    },
                    source_url=external_data.get("source_url") if external_data else None,
                    confidence=task.confidence,
                )
            )
            db.flush()
            self.agent_runs.finish(
                db,
                run,
                output_payload={
                    "verified": True,
                    "action": "updated",
                    "jira_key": task.jira_key,
                    "verdict": verdict,
                },
                confidence=task.confidence,
            )
            return {
                "verified": True,
                "confidence": task.confidence,
                "action": "updated",
                "discrepancy": None,
            }

        # Claim not verified or low confidence — create a suggestion proposal.
        reason_parts: list[str] = [
            verdict.get("reasoning") or "External data does not confirm this status.",
        ]
        if discrepancy:
            reason_parts.append(f"Discrepancy: {discrepancy}")
        if suggested_status:
            reason_parts.append(f"Suggested status from Jira: {suggested_status}")

        proposal = self.actions.create(
            db,
            organization_id=task.organization_id,
            user_id=task.user_id,
            task_id=task.id,
            proposal_type="verification_suggestion",
            target_system="internal",
            title=f"Unverified status claim for {task.jira_key or task.title[:60]}",
            reason=". ".join(reason_parts),
            payload={
                "claimed_status": claimed_status,
                "external_data": external_data or {},
                "verdict": verdict,
            },
            citations=task.source_citations_json or [],
        )
        db.add(
            MemoryEvent(
                organization_id=task.organization_id,
                user_id=task.user_id,
                task_id=task.id,
                event_type="claim.unverified",
                payload_json={
                    "claimed_status": claimed_status,
                    "verdict": verdict,
                    "proposal_id": proposal.id,
                },
                confidence=confidence,
            )
        )
        db.flush()
        self.agent_runs.finish(
            db,
            run,
            output_payload={
                "verified": False,
                "action": "suggestion",
                "jira_key": task.jira_key,
                "proposal_id": proposal.id,
                "verdict": verdict,
            },
            confidence=confidence,
        )
        logger.info(
            "Unverified claim stored as suggestion task=%s proposal=%s",
            task.id,
            proposal.id,
        )
        return {
            "verified": False,
            "confidence": confidence,
            "action": "suggestion",
            "discrepancy": discrepancy,
            "proposal_id": proposal.id,
        }

    def _fetch_jira_state(
        self,
        db: Session,
        user: User,
        jira_key: str,
    ) -> dict[str, Any] | None:
        """Fetch the current state of a Jira issue via the linked account connector."""
        accounts = (
            db.query(LinkedAccount)
            .filter(
                LinkedAccount.user_id == user.id,
                LinkedAccount.source == "jira",
                LinkedAccount.is_active.is_(True),
            )
            .all()
        )
        for account in accounts:
            try:
                raw = JiraConnector().fetch_issue_by_key(account, jira_key)
                if raw:
                    metadata = raw.get("metadata") or {}
                    return {
                        "jira_key": jira_key,
                        "status": raw.get("status") or metadata.get("status"),
                        "assignee": raw.get("assignee") or metadata.get("assignee"),
                        "resolution": metadata.get("resolution"),
                        "updated": raw.get("updated") or metadata.get("updated"),
                        "source_url": metadata.get("source_url"),
                        "summary": raw.get("title") or raw.get("summary"),
                    }
            except Exception as exc:
                logger.warning(
                    "Jira state fetch failed user=%s account=%s jira_key=%s error=%s",
                    user.email,
                    account.id,
                    jira_key,
                    exc,
                )
        return None

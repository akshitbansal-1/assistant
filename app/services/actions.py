from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.account import LinkedAccount
from app.models.communication import ActionProposal, AuditLog, FollowUp
from app.services.oauth import TokenCipher
from app.utils.datetime import utcnow


logger = logging.getLogger(__name__)


class ActionProposalService:
    def create(
        self,
        db: Session,
        *,
        organization_id: str,
        user_id: str,
        proposal_type: str,
        target_system: str,
        title: str,
        payload: dict[str, Any],
        task_id: str | None = None,
        reason: str | None = None,
        citations: list[dict[str, Any]] | None = None,
        requested_by_person_id: str | None = None,
    ) -> ActionProposal:
        proposal = ActionProposal(
            organization_id=organization_id,
            user_id=user_id,
            task_id=task_id,
            proposal_type=proposal_type,
            target_system=target_system,
            title=title,
            reason=reason,
            payload_json=payload,
            original_payload_json=payload,
            source_citations_json=citations or [],
            requested_by_person_id=requested_by_person_id,
        )
        db.add(proposal)
        db.flush()
        self._audit(db, proposal, "action_proposal.created", after=payload)
        logger.info(
            "Created action proposal proposal=%s org=%s user=%s type=%s target=%s task=%s citations=%d",
            proposal.id,
            organization_id,
            user_id,
            proposal_type,
            target_system,
            task_id,
            len(citations or []),
        )
        return proposal

    def approve(
        self,
        db: Session,
        proposal_id: str,
        *,
        approved_by_person_id: str | None = None,
        execute: bool = False,
    ) -> ActionProposal:
        proposal = db.query(ActionProposal).filter(ActionProposal.id == proposal_id).first()
        if not proposal:
            raise ValueError(f"Action proposal not found: {proposal_id}")
        if proposal.status not in {"pending_approval", "approved"}:
            raise ValueError(f"Cannot approve proposal in status {proposal.status}")
        before = {"status": proposal.status}
        logger.info("Approving action proposal proposal=%s target=%s type=%s execute=%s", proposal.id, proposal.target_system, proposal.proposal_type, execute)
        proposal.status = "approved"
        proposal.approved_by_person_id = approved_by_person_id
        proposal.approved_at = utcnow()
        db.flush()
        self._audit(db, proposal, "action_proposal.approved", before=before, after={"status": proposal.status})
        if execute:
            self.execute(db, proposal.id)
        return proposal

    def reject(
        self,
        db: Session,
        proposal_id: str,
        *,
        rejected_by_person_id: str | None = None,
        reason: str,
    ) -> ActionProposal:
        proposal = self._proposal_or_error(db, proposal_id)
        if proposal.status not in {"pending_approval", "approved"}:
            raise ValueError(f"Cannot reject proposal in status {proposal.status}")
        before = {"status": proposal.status, "payload": proposal.payload_json}
        proposal.status = "rejected"
        proposal.rejected_by_person_id = rejected_by_person_id
        proposal.rejected_at = utcnow()
        proposal.rejection_reason = reason
        db.flush()
        self._audit(
            db,
            proposal,
            "action_proposal.rejected",
            actor_person_id=rejected_by_person_id,
            before=before,
            after={"status": proposal.status, "reason": reason},
        )
        logger.info("Rejected action proposal proposal=%s reason_chars=%d", proposal.id, len(reason or ""))
        return proposal

    def cancel(
        self,
        db: Session,
        proposal_id: str,
        *,
        actor_person_id: str | None = None,
        reason: str,
    ) -> ActionProposal:
        proposal = self._proposal_or_error(db, proposal_id)
        if proposal.status in {"executed", "failed"}:
            raise ValueError(f"Cannot cancel proposal in status {proposal.status}")
        before = {"status": proposal.status}
        proposal.status = "canceled"
        proposal.rejection_reason = reason
        db.flush()
        self._audit(
            db,
            proposal,
            "action_proposal.canceled",
            actor_person_id=actor_person_id,
            before=before,
            after={"status": proposal.status, "reason": reason},
        )
        logger.info("Canceled action proposal proposal=%s", proposal.id)
        return proposal

    def edit(
        self,
        db: Session,
        proposal_id: str,
        *,
        payload: dict[str, Any],
        actor_person_id: str | None = None,
    ) -> ActionProposal:
        proposal = self._proposal_or_error(db, proposal_id)
        if proposal.status != "pending_approval":
            raise ValueError(f"Cannot edit proposal in status {proposal.status}")
        before = {"payload": proposal.payload_json, "status": proposal.status}
        if not proposal.original_payload_json:
            proposal.original_payload_json = proposal.payload_json or {}
        proposal.payload_json = payload
        proposal.updated_at = utcnow()
        db.flush()
        self._audit(
            db,
            proposal,
            "action_proposal.edited",
            actor_person_id=actor_person_id,
            before=before,
            after={"payload": proposal.payload_json, "original_payload": proposal.original_payload_json},
        )
        logger.info("Edited action proposal proposal=%s", proposal.id)
        return proposal

    def execute(self, db: Session, proposal_id: str) -> ActionProposal:
        proposal = self._proposal_or_error(db, proposal_id)
        if proposal.requires_approval and proposal.status != "approved":
            logger.warning("Blocked unapproved action execution proposal=%s status=%s", proposal.id, proposal.status)
            raise ValueError("External write is blocked until proposal approval")

        try:
            logger.info("Executing action proposal proposal=%s target=%s type=%s", proposal.id, proposal.target_system, proposal.proposal_type)
            if proposal.target_system == "slack" and proposal.proposal_type == "slack_dm":
                self._send_slack_dm(db, proposal)
            elif proposal.target_system == "jira" and proposal.proposal_type == "jira_update":
                self._post_jira_update(db, proposal)
            else:
                raise ValueError(f"Unsupported action target: {proposal.target_system}/{proposal.proposal_type}")
        except Exception as exc:
            proposal.status = "failed"
            proposal.error = str(exc)
            db.flush()
            self._audit(db, proposal, "action_proposal.failed", after={"error": proposal.error})
            logger.exception("Action proposal execution failed proposal=%s target=%s type=%s", proposal.id, proposal.target_system, proposal.proposal_type)
            raise ValueError(str(exc)) from exc

        proposal.status = "executed"
        proposal.executed_at = utcnow()
        db.flush()
        self._audit(db, proposal, "action_proposal.executed", after={"status": proposal.status})
        logger.info("Executed action proposal proposal=%s external_url=%s", proposal.id, proposal.external_url)
        return proposal

    def slack_blocks(self, proposal: ActionProposal) -> list[dict[str, Any]]:
        draft = proposal.payload_json or {}
        text = draft.get("text") or draft.get("body") or proposal.reason or proposal.title
        citations = proposal.source_citations_json or []
        citation_text = "No citations attached."
        if citations:
            citation_titles = [
                citation.get("title") or citation.get("external_id") or citation.get("source") or "source"
                for citation in citations[:3]
            ]
            citation_text = "Sources: " + ", ".join(str(item) for item in citation_titles)
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{proposal.title}*\n{proposal.reason or ''}".strip()}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{str(text)[:2400]}```"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": citation_text[:2800]}]},
            {
                "type": "actions",
                "block_id": f"proposal:{proposal.id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "approve_proposal",
                        "value": proposal.id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "style": "danger",
                        "action_id": "reject_proposal",
                        "value": proposal.id,
                    },
                ],
            },
        ]

    def _proposal_or_error(self, db: Session, proposal_id: str) -> ActionProposal:
        proposal = db.query(ActionProposal).filter(ActionProposal.id == proposal_id).first()
        if not proposal:
            raise ValueError(f"Action proposal not found: {proposal_id}")
        return proposal

    def _send_slack_dm(self, db: Session, proposal: ActionProposal) -> None:
        payload = proposal.payload_json or {}
        target_user_id = payload.get("target_slack_user_id")
        text = payload.get("text")
        if not target_user_id or not text:
            raise ValueError("Slack DM proposal requires target_slack_user_id and text")
        logger.info("Sending Slack DM proposal=%s target_slack_user=%s", proposal.id, target_user_id)

        query = (
            db.query(LinkedAccount)
            .filter(
                LinkedAccount.user_id == proposal.user_id,
                LinkedAccount.source == "slack",
                LinkedAccount.is_active.is_(True),
            )
        )
        if payload.get("slack_account_id"):
            query = query.filter(LinkedAccount.id == payload["slack_account_id"])
        account = query.first()
        if not account or not account.access_token:
            raise ValueError("No active Slack account is available for sending DMs")
        token = TokenCipher().decrypt(account.access_token) or account.access_token
        settings = get_settings()
        with httpx.Client(timeout=settings.request_timeout_seconds) as client:
            opened = client.post(
                "https://slack.com/api/conversations.open",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"users": target_user_id},
            )
            opened.raise_for_status()
            opened_payload = opened.json()
            if not opened_payload.get("ok"):
                raise ValueError(opened_payload.get("error") or "Slack conversations.open failed")
            channel_id = opened_payload["channel"]["id"]
            sent = client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"channel": channel_id, "text": text},
            )
            sent.raise_for_status()
            sent_payload = sent.json()
            if not sent_payload.get("ok"):
                raise ValueError(sent_payload.get("error") or "Slack chat.postMessage failed")
        proposal.external_url = f"https://slack.com/app_redirect?channel={channel_id}&message_ts={sent_payload.get('ts')}"
        follow_up_id = payload.get("follow_up_id")
        if follow_up_id:
            follow_up = db.query(FollowUp).filter(FollowUp.id == follow_up_id).first()
            if follow_up:
                follow_up.status = "sent"
                follow_up.channel_id = channel_id
                follow_up.external_message_id = sent_payload.get("ts")
                follow_up.updated_at = utcnow()
                logger.info("Marked follow-up sent follow_up=%s channel=%s message_ts=%s", follow_up.id, channel_id, sent_payload.get("ts"))

    def _post_jira_update(self, db: Session, proposal: ActionProposal) -> None:
        payload = proposal.payload_json or {}
        jira_key = payload.get("jira_key")
        body = payload.get("body")
        operation = payload.get("operation", "add_comment")
        if operation != "add_comment":
            raise ValueError("Only Jira comment updates are supported in MVP")
        if not jira_key or not body:
            raise ValueError("Jira update proposal requires jira_key and body")
        logger.info("Posting Jira update proposal=%s jira_key=%s operation=%s", proposal.id, jira_key, operation)

        query = (
            db.query(LinkedAccount)
            .filter(
                LinkedAccount.user_id == proposal.user_id,
                LinkedAccount.source == "jira",
                LinkedAccount.is_active.is_(True),
            )
        )
        if payload.get("jira_account_id"):
            query = query.filter(LinkedAccount.id == payload["jira_account_id"])
        account = query.first()
        if not account or not account.access_token:
            raise ValueError("No active Jira account is available for posting updates")
        base_url = (account.metadata_json or {}).get("base_url")
        if not base_url:
            raise ValueError("Jira account metadata must include base_url")

        token = TokenCipher().decrypt(account.access_token) or account.access_token
        url = f"{base_url.rstrip('/')}/rest/api/3/issue/{jira_key}/comment"
        adf_body = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": body}],
                }
            ],
        }
        settings = get_settings()
        with httpx.Client(timeout=settings.request_timeout_seconds) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
                json={"body": adf_body},
            )
            response.raise_for_status()
            response_payload = response.json()
        proposal.external_url = response_payload.get("self")
        logger.info("Posted Jira update proposal=%s jira_key=%s external_url=%s", proposal.id, jira_key, proposal.external_url)

    def _audit(
        self,
        db: Session,
        proposal: ActionProposal,
        action: str,
        *,
        actor_person_id: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> None:
        db.add(
            AuditLog(
                organization_id=proposal.organization_id,
                user_id=proposal.user_id,
                actor_person_id=actor_person_id or proposal.approved_by_person_id or proposal.rejected_by_person_id or proposal.requested_by_person_id,
                action=action,
                entity_type="action_proposal",
                entity_id=proposal.id,
                before_json=before or {},
                after_json=after or {},
            )
        )

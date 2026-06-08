from __future__ import annotations

import logging
from datetime import timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.account import User
from app.models.communication import ActionProposal, CommunicationTask, TaskSource
from app.models.item import WorkItem
from app.services.actions import ActionProposalService
from app.services.agent_runs import AgentRunRecorder
from app.services.communication import CommunicationLoopService
from app.utils.datetime import utcnow


logger = logging.getLogger(__name__)


class JiraHygieneService:
    def __init__(self) -> None:
        self.actions = ActionProposalService()
        self.agent_runs = AgentRunRecorder()
        self.loop = CommunicationLoopService()

    def detect_stale_tickets(self, db: Session, user: User, stale_days: int | None = None) -> list[ActionProposal]:
        org = self.loop.get_or_create_organization_for_user(db, user)
        cutoff = utcnow() - timedelta(days=stale_days or get_settings().jira_stale_days)
        run = self.agent_runs.start(
            db,
            organization_id=org.id,
            user_id=user.id,
            agent_name="stale_alert",
            input_payload={
                "stale_days": stale_days or get_settings().jira_stale_days,
                "cutoff": cutoff.isoformat(),
            },
            source_system="jira",
        )
        proposals: list[ActionProposal] = []
        try:
            tasks = (
                db.query(CommunicationTask)
                .filter(CommunicationTask.organization_id == org.id, CommunicationTask.jira_key.isnot(None))
                .all()
            )
            logger.info("Jira hygiene scan started user=%s org=%s tasks=%d cutoff=%s", user.email, org.id, len(tasks), cutoff)
            for task in tasks:
                if task.jira_key:
                    self.loop.refresh_jira_issue_context(db, user, org.id, task.jira_key)
                    db.refresh(task)
                reasons = self._stale_reasons(db, task, cutoff)
                if not reasons:
                    logger.info("Jira hygiene task clean task=%s jira_key=%s", task.id, task.jira_key)
                    continue
                existing = (
                    db.query(ActionProposal)
                    .filter(
                        ActionProposal.organization_id == org.id,
                        ActionProposal.task_id == task.id,
                        ActionProposal.proposal_type == "jira_update",
                        ActionProposal.status == "pending_approval",
                    )
                    .first()
                )
                if existing:
                    proposals.append(existing)
                    logger.info("Jira hygiene reused existing proposal task=%s jira_key=%s proposal=%s", task.id, task.jira_key, existing.id)
                    continue
                proposal = self.actions.create(
                    db,
                    organization_id=org.id,
                    user_id=user.id,
                    task_id=task.id,
                    proposal_type="jira_update",
                    target_system="jira",
                    title=f"Draft Jira hygiene update for {task.jira_key}",
                    reason="; ".join(reasons),
                    payload={
                        "jira_key": task.jira_key,
                        "operation": "add_comment",
                        "body": self._draft_update(task, reasons),
                        "context_metadata": {
                            "stale_reasons": reasons,
                            "last_updated": task.updated_at.isoformat() if task.updated_at else None,
                            "requires_attention": True,
                        },
                    },
                    citations=task.source_citations_json or [],
                )
                proposals.append(proposal)
                logger.info("Jira hygiene created proposal task=%s jira_key=%s proposal=%s reasons=%s", task.id, task.jira_key, proposal.id, "; ".join(reasons))
            db.flush()
            self.agent_runs.finish(
                db,
                run,
                output_payload={"scanned_tasks": len(tasks), "proposal_count": len(proposals)},
                confidence=1.0,
            )
            logger.info("Jira hygiene scan finished user=%s org=%s proposals=%d", user.email, org.id, len(proposals))
            return proposals
        except Exception as exc:
            self.agent_runs.fail(db, run, error=str(exc), output_payload={"proposal_count": len(proposals)})
            raise

    def _stale_reasons(self, db: Session, task: CommunicationTask, cutoff) -> list[str]:
        reasons: list[str] = []
        latest_slack = self._latest_source_item(db, task.id, "slack")
        latest_jira = self._latest_source_item(db, task.id, "jira")
        last_human_update = self._as_utc(task.last_human_update_at)
        updated_at = self._as_utc(task.updated_at)
        latest_slack_at = self._as_utc(latest_slack.timestamp) if latest_slack else None
        latest_jira_at = self._as_utc(latest_jira.timestamp) if latest_jira else None

        if latest_slack_at and (not latest_jira_at or latest_slack_at > latest_jira_at):
            reasons.append("Related Slack activity is newer than the latest Jira activity")
        if last_human_update and updated_at and last_human_update > updated_at:
            reasons.append("Slack/human activity is newer than task memory update")
        if updated_at and updated_at < cutoff:
            reasons.append("Jira-linked task has no recent update")
        if task.eta and self._as_utc(task.eta) < utcnow():
            reasons.append(f"Task commitment date ({task.eta}) has passed")

        latest_text = (task.latest_status or "").lower()
        if task.status != "blocked" and (task.blocker or any(token in latest_text for token in ["blocked", "stuck", "waiting on"])):
            reasons.append("Latest human context mentions a blocker but status is not blocked")
        if task.status == "blocked" and not task.blocker:
            reasons.append("Task is blocked without a blocker explanation")
        return reasons

    def _latest_source_item(self, db: Session, task_id: str, source: str) -> WorkItem | None:
        return (
            db.query(WorkItem)
            .join(TaskSource, TaskSource.work_item_id == WorkItem.id)
            .filter(TaskSource.task_id == task_id, TaskSource.source_system == source)
            .order_by(WorkItem.timestamp.desc())
            .first()
        )

    def _draft_update(self, task: CommunicationTask, reasons: list[str]) -> str:
        lines = [
            f"Coordination memory update for {task.jira_key}:",
            task.latest_status or "No explicit status extracted.",
            "",
            "---",
            f"Attention required for: {'; '.join(reasons)}",
        ]
        if task.blocker:
            lines.append(f"Blocker: {task.blocker}")
        if task.eta:
            lines.append(f"Commitment/ETA: {task.eta}")
        return "\n".join(lines)

    def _as_utc(self, value):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

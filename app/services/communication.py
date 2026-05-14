from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from dateutil.parser import parse as parse_dt
from sqlalchemy import String, func
from sqlalchemy.orm import Session

from app.connectors.jira import JiraConnector
from app.connectors.slack import SlackConnector
from app.models.account import LinkedAccount, User
from app.models.communication import (
    ActionProposal,
    AuditLog,
    Commitment,
    CommitmentParticipant,
    CommunicationTask,
    FollowUp,
    FollowUpMessage,
    MemoryEvent,
    Organization,
    OrganizationMember,
    Person,
    TaskSource,
    TaskStatusSnapshot,
)
from app.models.item import WorkItem
from app.services.actions import ActionProposalService
from app.services.authorization import AuthorizationService
from app.services.ingestion import IngestionService
from app.services.intelligence import IntelligenceService
from app.services.normalization import NormalizationService
from app.services.retrieval import RetrievalService
from app.services.search import SearchIndexService
from app.utils.datetime import utcnow
from app.utils.idempotency import extract_issue_keys, fingerprint_for_text


logger = logging.getLogger(__name__)


class CommunicationLoopService:
    def __init__(self) -> None:
        self.actions = ActionProposalService()
        self.authorization = AuthorizationService()
        self.retrieval = RetrievalService()
        self.search = SearchIndexService()

    def get_or_create_organization_for_user(self, db: Session, user: User) -> Organization:
        domain = (user.email.split("@", 1)[1] if "@" in user.email else user.email).lower()
        slug = re.sub(r"[^a-z0-9]+", "-", domain).strip("-") or f"user-{user.id}"
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=domain, slug=slug)
            db.add(org)
            db.flush()
            logger.info("Created organization org=%s slug=%s user=%s", org.id, slug, user.email)
        member = (
            db.query(OrganizationMember)
            .filter(OrganizationMember.organization_id == org.id, OrganizationMember.user_id == user.id)
            .first()
        )
        if not member:
            db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role="admin"))
            db.flush()
            logger.info("Added organization member org=%s user=%s", org.id, user.email)
        return org

    def get_or_create_person(
        self,
        db: Session,
        organization_id: str,
        name: str | None,
        *,
        user_id: str | None = None,
        source_system: str | None = None,
        source_id: str | None = None,
        email: str | None = None,
    ) -> Person | None:
        cleaned = (name or email or source_id or "").strip()
        if not cleaned:
            return None
        normalized = cleaned.strip("@").lower()
        query = db.query(Person).filter(Person.organization_id == organization_id)
        person = (
            query.filter(func.lower(Person.display_name) == normalized).first()
            or (query.filter(func.lower(Person.email) == normalized).first() if "@" in normalized else None)
            or query.filter(Person.aliases_json.cast(String).ilike(f"%{cleaned}%")).first()
            or query.filter(Person.source_ids_json.cast(String).ilike(f"%{source_id}%")).first()
        )
        if person:
            if source_system and source_id:
                source_ids = dict(person.source_ids_json or {})
                source_ids[source_system] = source_id
                person.source_ids_json = source_ids
            if user_id and not person.user_id:
                person.user_id = user_id
            person.updated_at = utcnow()
            db.flush()
            return person

        source_ids_json = {source_system: source_id} if source_system and source_id else {}
        person = Person(
            organization_id=organization_id,
            user_id=user_id,
            manager_person_id=None,
            display_name=cleaned.strip("@"),
            email=email if email and "@" in email else cleaned if "@" in cleaned else None,
            aliases_json=[cleaned],
            source_ids_json=source_ids_json,
        )
        db.add(person)
        db.flush()
        logger.info("Created person org=%s person=%s source=%s has_email=%s", organization_id, person.id, source_system, bool(person.email))
        return person

    def process_work_items(self, db: Session, user: User, items: list[WorkItem], extraction_payload: dict[str, Any]) -> None:
        org = self.get_or_create_organization_for_user(db, user)
        logger.info(
            "Processing communication memory user=%s org=%s items=%d extracted_commitments=%d",
            user.email,
            org.id,
            len(items),
            len(extraction_payload.get("commitments", [])),
        )
        for item in items:
            self.upsert_task_from_item(db, org.id, user.id, item)
            self.search.upsert_work_item(db, organization_id=org.id, user_id=user.id, item=item)
        for raw in extraction_payload.get("commitments", []):
            self.store_extracted_commitment(db, org.id, user.id, raw)
        db.flush()
        logger.info("Finished communication memory processing user=%s org=%s", user.email, org.id)

    def upsert_task_from_item(self, db: Session, organization_id: str, user_id: str, item: WorkItem) -> CommunicationTask:
        metadata = item.metadata_json or {}
        issue_keys = metadata.get("issue_keys") or ([metadata["issue_key"]] if metadata.get("issue_key") else [])
        jira_key = issue_keys[0] if issue_keys else None
        canonical_key = f"issue:{jira_key}" if jira_key else item.dedupe_key
        citation = self._citation_for_item(item)
        owner = None
        for person_name in item.people_json or []:
            owner = self.get_or_create_person(db, organization_id, person_name, source_system=item.source, source_id=person_name)
            if owner:
                break

        task = (
            db.query(CommunicationTask)
            .filter(CommunicationTask.organization_id == organization_id, CommunicationTask.canonical_key == canonical_key)
            .first()
        )
        if not task:
            task = CommunicationTask(
                organization_id=organization_id,
                user_id=user_id,
                canonical_key=canonical_key,
                title=item.title,
                jira_key=jira_key,
                owner_person_id=owner.id if owner else None,
                status="blocked" if item.classification == "blocker" else "open",
                latest_status=item.short_summary or item.content[:300],
                blocker=item.short_summary if item.classification == "blocker" else None,
                confidence=0.65 if item.needs_action else 0.45,
                source_citations_json=[citation],
                last_human_update_at=item.timestamp,
            )
            db.add(task)
            db.flush()
            logger.info(
                "Created communication task org=%s task=%s canonical_key=%s source=%s jira_key=%s confidence=%.2f",
                organization_id,
                task.id,
                canonical_key,
                item.source,
                jira_key,
                task.confidence or 0.0,
            )
        else:
            jira_refresh_is_stale = item.source == "jira" and self._is_older_than(item.timestamp, task.last_human_update_at)
            task.title = item.title or task.title
            task.jira_key = task.jira_key or jira_key
            task.owner_person_id = task.owner_person_id or (owner.id if owner else None)
            if not jira_refresh_is_stale:
                task.status = "blocked" if item.classification == "blocker" else task.status
                task.latest_status = item.short_summary or item.content[:300] or task.latest_status
                if item.classification == "blocker":
                    task.blocker = item.short_summary or item.content[:300]
                task.last_human_update_at = self._latest_datetime(task.last_human_update_at, item.timestamp)
            task.confidence = max(task.confidence or 0.0, 0.65 if item.needs_action else 0.45)
            task.source_citations_json = self._append_citation(task.source_citations_json or [], citation)
            task.updated_at = utcnow()
            db.flush()
            logger.info(
                "Updated communication task org=%s task=%s canonical_key=%s source=%s jira_key=%s confidence=%.2f",
                organization_id,
                task.id,
                canonical_key,
                item.source,
                jira_key,
                task.confidence or 0.0,
            )

        source = (
            db.query(TaskSource)
            .filter(
                TaskSource.task_id == task.id,
                TaskSource.source_system == item.source,
                TaskSource.external_id == item.external_id,
            )
            .first()
        )
        if not source:
            db.add(
                TaskSource(
                    task_id=task.id,
                    work_item_id=item.id,
                    source_system=item.source,
                    external_id=item.external_id,
                    source_url=citation.get("url"),
                    slack_thread_id=item.thread_id if item.source == "slack" else None,
                    metadata_json=metadata,
                )
            )
            logger.info("Linked task source task=%s source=%s external_id=%s", task.id, item.source, item.external_id)

        self._snapshot(db, task)
        self.search.upsert_task(db, task)
        self.search.upsert_work_item(db, organization_id=organization_id, user_id=user_id, item=item)
        return task

    def store_extracted_commitment(
        self,
        db: Session,
        organization_id: str,
        user_id: str,
        raw: dict[str, Any],
    ) -> Commitment | ActionProposal | None:
        confidence = float(raw.get("confidence") or 0)
        title = raw.get("task_title") or raw.get("jira_key") or raw.get("commitment_text") or "Untitled commitment"
        jira_key = raw.get("jira_key")
        canonical_key = f"issue:{jira_key}" if jira_key else f"task:{fingerprint_for_text(title)[:20]}"
        owner = self.get_or_create_person(db, organization_id, raw.get("owner"), source_system=raw.get("source_system"))
        requester = self.get_or_create_person(db, organization_id, raw.get("requester"), source_system=raw.get("source_system"))
        task = (
            db.query(CommunicationTask)
            .filter(CommunicationTask.organization_id == organization_id, CommunicationTask.canonical_key == canonical_key)
            .first()
        )
        citations = [
            {
                "source": raw.get("source_system"),
                "title": title,
                "url": raw.get("source_url"),
                "external_id": raw.get("source_message_id"),
            }
        ]
        if not task:
            task = CommunicationTask(
                organization_id=organization_id,
                user_id=user_id,
                canonical_key=canonical_key,
                title=title,
                project=raw.get("project"),
                jira_key=jira_key,
                owner_person_id=owner.id if owner else None,
                status=raw.get("status") or "open",
                latest_status=raw.get("commitment_text"),
                confidence=confidence,
                source_citations_json=citations,
            )
            db.add(task)
            db.flush()

        if confidence < 0.65:
            logger.info(
                "Low-confidence commitment became suggestion org=%s task=%s confidence=%.2f source=%s",
                organization_id,
                task.id,
                confidence,
                raw.get("source_system"),
            )
            return self.actions.create(
                db,
                organization_id=organization_id,
                user_id=user_id,
                task_id=task.id,
                proposal_type="commitment_suggestion",
                target_system="internal",
                title=f"Review possible commitment: {title}",
                reason="Low-confidence extraction stored as a suggestion, not a fact.",
                payload=raw,
                citations=citations,
            )

        existing = (
            db.query(Commitment)
            .filter(
                Commitment.organization_id == organization_id,
                Commitment.source_system == (raw.get("source_system") or ""),
                Commitment.source_message_id == raw.get("source_message_id"),
                Commitment.commitment_text == (raw.get("commitment_text") or ""),
            )
            .first()
        )
        if existing:
            logger.info("Skipped duplicate commitment org=%s commitment=%s source=%s", organization_id, existing.id, raw.get("source_system"))
            return existing
        commitment = Commitment(
            organization_id=organization_id,
            user_id=user_id,
            task_id=task.id,
            owner_person_id=owner.id if owner else None,
            requester_person_id=requester.id if requester else None,
            commitment_text=raw.get("commitment_text") or title,
            source_system=raw.get("source_system") or "unknown",
            source_url=raw.get("source_url"),
            source_message_id=raw.get("source_message_id"),
            due_date=self._parse_date(raw.get("due_date")),
            status=raw.get("status") or "open",
            confidence=confidence,
            extraction_json=raw,
        )
        db.add(commitment)
        db.flush()
        for person, role in [(owner, "owner"), (requester, "requester")]:
            if person:
                db.add(CommitmentParticipant(commitment_id=commitment.id, person_id=person.id, role=role))
        self._event(
            db,
            organization_id,
            user_id,
            "commitment.extracted",
            task_id=task.id,
            person_id=owner.id if owner else None,
            payload=raw,
            source_url=raw.get("source_url"),
            confidence=confidence,
        )
        logger.info(
            "Stored commitment org=%s commitment=%s task=%s status=%s confidence=%.2f source=%s due_date=%s",
            organization_id,
            commitment.id,
            task.id,
            commitment.status,
            commitment.confidence or 0.0,
            commitment.source_system,
            commitment.due_date,
        )
        return commitment

    def answer_whereis(
        self,
        db: Session,
        user: User,
        person: str,
        task_query: str,
        *,
        requester: str | None = None,
        enforce_authorization: bool = False,
    ) -> dict[str, Any]:
        org = self.get_or_create_organization_for_user(db, user)
        issue_keys = extract_issue_keys(task_query)
        if issue_keys:
            self.refresh_jira_issue_context(db, user, org.id, issue_keys[0])
        resolved_person = self.resolve_slack_person(db, user, org.id, person)
        if enforce_authorization or requester:
            requester_person = self.resolve_slack_person(db, user, org.id, requester or "") or self.get_or_create_person(
                db,
                org.id,
                requester,
                user_id=user.id if requester == user.email else None,
                source_system="slack" if requester and not "@" in requester else None,
                source_id=(requester or "").strip("<@>") or None,
                email=requester if requester and "@" in requester else None,
            )
            target_person = resolved_person or self.get_or_create_person(db, org.id, person, source_system="slack", source_id=person.strip("<@>"))
            if not self.authorization.can_ask_whereis(db, org.id, requester_person, target_person):
                logger.warning(
                    "Whereis denied org=%s requester=%s target=%s task_query=%s",
                    org.id,
                    requester_person.id if requester_person else None,
                    target_person.id if target_person else None,
                    task_query,
                )
                raise PermissionError("Only a manager, admin, or the task owner can ask /whereis for that person's tasks")
        retrieval_person = resolved_person.display_name if resolved_person else person
        logger.info("Whereis requested user=%s org=%s person=%s task_query=%s", user.email, org.id, person, task_query)
        result = self.retrieval.retrieve(
            db,
            organization_id=org.id,
            user_id=user.id,
            person=retrieval_person,
            jira_key=issue_keys[0] if issue_keys else None,
            task_query=task_query,
        )
        task = result["tasks"][0] if result["tasks"] else None
        commitments = result["commitments"]
        snapshots = result["snapshots"]
        items = result["items"]
        latest_snapshot = snapshots[0] if snapshots else None
        citations = result["citations"]

        if not task and not items:
            logger.info("Whereis no reliable memory user=%s org=%s person=%s task_query=%s", user.email, org.id, person, task_query)
            return {
                "answer": f"No reliable memory found for {person} on {task_query}.",
                "status": "unknown",
                "blocker": None,
                "eta": None,
                "confidence": 0.0,
                "citations": [],
            }

        status = (
            (latest_snapshot.latest_known_status if latest_snapshot else None)
            or (task.latest_status if task else None)
            or (items[0].short_summary if items else None)
            or "Recent context exists, but no explicit status was extracted."
        )
        blocker = (latest_snapshot.blocker if latest_snapshot else None) or (task.blocker if task else None)
        eta = (latest_snapshot.eta if latest_snapshot else None) or (task.eta if task else None)
        confidence = max([task.confidence if task else 0.0, latest_snapshot.confidence if latest_snapshot else 0.0, 0.45])
        open_commitments = [c.commitment_text for c in commitments if c.status in {"open", "blocked", "stale"}][:2]
        answer_parts = [f"Status: {status}"]
        if blocker:
            answer_parts.append(f"Blocker: {blocker}")
        if eta:
            answer_parts.append(f"ETA: {eta}")
        if open_commitments:
            answer_parts.append(f"Open commitment: {'; '.join(open_commitments)}")
        answer_parts.append(f"Confidence: {confidence:.2f}")
        logger.info(
            "Whereis answered user=%s org=%s task=%s confidence=%.2f citations=%d commitments=%d",
            user.email,
            org.id,
            task.id if task else None,
            confidence,
            len(citations),
            len(commitments),
        )
        return {
            "answer": "\n".join(answer_parts),
            "status": status,
            "blocker": blocker,
            "eta": eta,
            "confidence": confidence,
            "task_id": task.id if task else None,
            "citations": citations,
        }

    def create_follow_up(
        self,
        db: Session,
        user: User,
        *,
        person: str,
        task_query: str,
        question: str,
        requester: str | None = None,
    ) -> dict[str, Any]:
        org = self.get_or_create_organization_for_user(db, user)
        logger.info("Creating follow-up user=%s org=%s person=%s task_query=%s", user.email, org.id, person, task_query)
        target = self.resolve_slack_person(db, user, org.id, person) or self.get_or_create_person(
            db,
            org.id,
            person,
            source_system="slack",
            source_id=person.strip("<@>"),
        )
        requester_person = self.get_or_create_person(db, org.id, requester or user.email, user_id=user.id)
        whereis = self.answer_whereis(db, user, target.display_name if target else person, task_query)
        task = db.query(CommunicationTask).filter(CommunicationTask.id == whereis.get("task_id")).first() if whereis.get("task_id") else None
        follow_up = FollowUp(
            organization_id=org.id,
            user_id=user.id,
            task_id=task.id if task else None,
            target_person_id=target.id,
            requester_person_id=requester_person.id if requester_person else None,
            question=question,
            context_json={"task_query": task_query, "whereis": whereis},
        )
        db.add(follow_up)
        db.flush()
        text = self._follow_up_text(person, task_query, question, whereis)
        db.add(
            FollowUpMessage(
                follow_up_id=follow_up.id,
                sender_person_id=requester_person.id if requester_person else None,
                direction="outbound",
                body=text,
            )
        )
        proposal = self.actions.create(
            db,
            organization_id=org.id,
            user_id=user.id,
            task_id=task.id if task else None,
            proposal_type="slack_dm",
            target_system="slack",
            title=f"Send follow-up to {person} about {task_query}",
            reason="Manager requested a contextual follow-up. External DM waits for approval.",
            payload={
                "target_slack_user_id": (target.source_ids_json or {}).get("slack") or person.strip("<@>"),
                "text": text,
                "follow_up_id": follow_up.id,
            },
            citations=whereis.get("citations", []),
            requested_by_person_id=requester_person.id if requester_person else None,
        )
        self._event(
            db,
            org.id,
            user.id,
            "follow_up.created",
            task_id=task.id if task else None,
            person_id=target.id,
            payload={"question": question, "proposal_id": proposal.id},
        )
        logger.info(
            "Created follow-up org=%s follow_up=%s proposal=%s target_person=%s task=%s",
            org.id,
            follow_up.id,
            proposal.id,
            target.id,
            task.id if task else None,
        )
        return {"follow_up": follow_up, "proposal": proposal, "whereis": whereis}

    def resolve_slack_person(self, db: Session, user: User, organization_id: str, person_ref: str) -> Person | None:
        account = self._active_account(db, user.id, "slack")
        if not account:
            return None
        try:
            profile = SlackConnector().resolve_user(account, person_ref)
        except Exception as exc:
            logger.warning("Slack user lookup failed user=%s person_ref=%s error=%s", user.email, person_ref, exc)
            return None
        if not profile:
            return None
        person = self.get_or_create_person(
            db,
            organization_id,
            profile.get("display_name") or person_ref,
            source_system="slack",
            source_id=profile.get("id"),
            email=profile.get("email"),
        )
        if not person:
            return None
        aliases = set(person.aliases_json or [])
        aliases.update(alias for alias in (profile.get("aliases") or []) if alias)
        person.aliases_json = sorted(aliases)
        metadata = dict(person.metadata_json or {})
        if profile.get("team_id"):
            metadata["slack_team_id"] = profile["team_id"]
        person.metadata_json = metadata
        db.flush()
        return person

    def refresh_jira_issue_context(self, db: Session, user: User, organization_id: str, jira_key: str) -> WorkItem | None:
        account = self._active_account(db, user.id, "jira")
        if not account:
            return None
        try:
            raw_item = JiraConnector().fetch_issue_by_key(account, jira_key)
            if not raw_item:
                return None
            normalized = NormalizationService().normalize_item(account, raw_item)
            record = IngestionService().upsert_item(db, user.id, normalized)
            IntelligenceService().classify_all(db, [record])
            self.upsert_task_from_item(db, organization_id, user.id, record)
            db.flush()
            logger.info("Refreshed Jira issue context user=%s jira_key=%s work_item=%s", user.email, jira_key, record.id)
            return record
        except Exception as exc:
            logger.warning("Jira issue refresh skipped user=%s jira_key=%s error=%s", user.email, jira_key, exc)
            return None

    def capture_follow_up_reply(
        self,
        db: Session,
        *,
        slack_user_id: str,
        text: str,
        channel_id: str | None = None,
        message_ts: str | None = None,
    ) -> FollowUpMessage | None:
        person = None
        for candidate in db.query(Person).all():
            if (candidate.source_ids_json or {}).get("slack") == slack_user_id:
                person = candidate
                break
        if not person:
            logger.info("Follow-up reply ignored reason=unknown_slack_user slack_user=%s", slack_user_id)
            return None
        follow_up = (
            db.query(FollowUp)
            .filter(FollowUp.target_person_id == person.id, FollowUp.status.in_(["pending", "sent"]))
            .order_by(FollowUp.created_at.desc())
            .first()
        )
        if not follow_up:
            logger.info("Follow-up reply ignored reason=no_pending_follow_up person=%s slack_user=%s", person.id, slack_user_id)
            return None
        source_url = None
        if channel_id and message_ts:
            source_url = f"https://slack.com/app_redirect?channel={channel_id}&message_ts={message_ts}"
        message = FollowUpMessage(
            follow_up_id=follow_up.id,
            sender_person_id=person.id,
            direction="inbound",
            body=text,
            source_external_id=message_ts,
            source_url=source_url,
        )
        db.add(message)
        follow_up.status = "responded"
        follow_up.channel_id = channel_id or follow_up.channel_id
        follow_up.external_message_id = message_ts or follow_up.external_message_id
        follow_up.updated_at = utcnow()
        if follow_up.task_id:
            task = db.query(CommunicationTask).filter(CommunicationTask.id == follow_up.task_id).first()
            if task:
                task.latest_status = text[:1000]
                task.last_human_update_at = utcnow()
                task.source_citations_json = self._append_citation(
                    task.source_citations_json or [],
                    {"source": "slack", "title": "Follow-up reply", "url": source_url, "external_id": message_ts},
                )
                self._snapshot(db, task)
                self._event(
                    db,
                    follow_up.organization_id,
                    follow_up.user_id,
                    "follow_up.reply_captured",
                    task_id=task.id,
                    person_id=person.id,
                    payload={"text": text},
                    source_url=source_url,
                    confidence=0.85,
                )
        db.flush()
        logger.info(
            "Captured follow-up reply follow_up=%s person=%s task=%s channel=%s message_ts=%s",
            follow_up.id,
            person.id,
            follow_up.task_id,
            channel_id,
            message_ts,
        )
        return message

    def draft_jira_update_from_follow_up(self, db: Session, follow_up_id: str) -> ActionProposal:
        follow_up = db.query(FollowUp).filter(FollowUp.id == follow_up_id).first()
        if not follow_up:
            raise ValueError(f"Follow-up not found: {follow_up_id}")
        task = db.query(CommunicationTask).filter(CommunicationTask.id == follow_up.task_id).first() if follow_up.task_id else None
        latest_reply = (
            db.query(FollowUpMessage)
            .filter(FollowUpMessage.follow_up_id == follow_up.id, FollowUpMessage.direction == "inbound")
            .order_by(FollowUpMessage.received_at.desc())
            .first()
        )
        if not task or not task.jira_key:
            raise ValueError("Cannot draft Jira update without a linked Jira ticket")
        if not latest_reply:
            raise ValueError("Cannot draft Jira update before a human reply is captured")
        proposal = self.actions.create(
            db,
            organization_id=follow_up.organization_id,
            user_id=follow_up.user_id,
            task_id=task.id,
            proposal_type="jira_update",
            target_system="jira",
            title=f"Draft Jira update for {task.jira_key}",
            reason="Drafted from the latest human follow-up reply. Jira posting remains approval-gated.",
            payload={
                "jira_key": task.jira_key,
                "body": latest_reply.body,
                "operation": "add_comment",
            },
            citations=task.source_citations_json or [],
            requested_by_person_id=follow_up.requester_person_id,
        )
        logger.info("Drafted Jira update from follow-up follow_up=%s task=%s proposal=%s", follow_up.id, task.id, proposal.id)
        return proposal

    def _snapshot(self, db: Session, task: CommunicationTask) -> None:
        db.add(
            TaskStatusSnapshot(
                task_id=task.id,
                latest_known_status=task.latest_status,
                blocker=task.blocker,
                eta=task.eta,
                owner_person_id=task.owner_person_id,
                linked_jira_ticket=task.jira_key,
                source_citations_json=task.source_citations_json or [],
                confidence=task.confidence or 0.0,
                last_human_update_at=task.last_human_update_at,
                last_agent_nudge_at=task.last_agent_nudge_at,
            )
        )
        logger.info("Recorded task status snapshot task=%s jira_key=%s confidence=%.2f", task.id, task.jira_key, task.confidence or 0.0)

    def _event(
        self,
        db: Session,
        organization_id: str,
        user_id: str,
        event_type: str,
        *,
        task_id: str | None = None,
        person_id: str | None = None,
        payload: dict[str, Any] | None = None,
        source_url: str | None = None,
        confidence: float = 1.0,
    ) -> None:
        db.add(
            MemoryEvent(
                organization_id=organization_id,
                user_id=user_id,
                task_id=task_id,
                person_id=person_id,
                event_type=event_type,
                payload_json=payload or {},
                source_url=source_url,
                confidence=confidence,
            )
        )

    def _follow_up_text(self, person: str, task_query: str, question: str, whereis: dict[str, Any]) -> str:
        context = whereis.get("status") or "I found some related context, but no clear status."
        return (
            f"Quick coordination check on {task_query}.\n"
            f"Current memory: {context}\n"
            f"Question: {question}\n"
            "Reply here naturally; I will attach your response to the task memory before any Jira/Slack update is drafted."
        )

    def _citation_for_item(self, item: WorkItem) -> dict[str, Any]:
        metadata = item.metadata_json or {}
        return {
            "source": item.source,
            "title": item.title,
            "url": metadata.get("source_url"),
            "external_id": item.external_id,
            "timestamp": item.timestamp.isoformat() if item.timestamp else None,
        }

    def _append_citation(self, existing: list[dict[str, Any]], citation: dict[str, Any]) -> list[dict[str, Any]]:
        key = citation.get("url") or citation.get("external_id")
        if not key:
            return existing
        keys = {item.get("url") or item.get("external_id") for item in existing}
        if key not in keys:
            existing.append(citation)
        return existing[:12]

    def _parse_date(self, value: Any) -> date | None:
        if not value:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        try:
            return parse_dt(str(value)).date()
        except Exception:
            return None

    def _latest_datetime(self, left: datetime | None, right: datetime | None) -> datetime | None:
        if not left:
            return right
        if not right:
            return left
        left_utc = left.replace(tzinfo=timezone.utc) if left.tzinfo is None else left.astimezone(timezone.utc)
        right_utc = right.replace(tzinfo=timezone.utc) if right.tzinfo is None else right.astimezone(timezone.utc)
        return left if left_utc >= right_utc else right

    def _is_older_than(self, left: datetime | None, right: datetime | None) -> bool:
        if not left or not right:
            return False
        left_utc = left.replace(tzinfo=timezone.utc) if left.tzinfo is None else left.astimezone(timezone.utc)
        right_utc = right.replace(tzinfo=timezone.utc) if right.tzinfo is None else right.astimezone(timezone.utc)
        return left_utc < right_utc

    def _active_account(self, db: Session, user_id: str, source: str) -> LinkedAccount | None:
        return (
            db.query(LinkedAccount)
            .filter(
                LinkedAccount.user_id == user_id,
                LinkedAccount.source == source,
                LinkedAccount.is_active.is_(True),
            )
            .first()
        )

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base
from app.utils.datetime import utcnow


def json_type():
    return JSONB().with_variant(JSON(), "sqlite")


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (UniqueConstraint("slug", name="uq_organization_slug"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), index=True)
    settings_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_org_member_user"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(50), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (
        UniqueConstraint("organization_id", "display_name", name="uq_person_org_display_name"),
        Index("ix_people_org_email", "organization_id", "email"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), nullable=True)
    aliases_json: Mapped[list] = mapped_column(json_type(), default=list)
    source_ids_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CommunicationTask(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("organization_id", "canonical_key", name="uq_task_org_canonical_key"),
        Index("ix_tasks_org_jira_key", "organization_id", "jira_key"),
        Index("ix_tasks_org_project", "organization_id", "project"),
        Index("ix_tasks_org_owner", "organization_id", "owner_person_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    canonical_key: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    project: Mapped[str] = mapped_column(String(255), nullable=True)
    jira_key: Mapped[str] = mapped_column(String(50), nullable=True)
    owner_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(50), default="open")
    latest_status: Mapped[str] = mapped_column(Text, nullable=True)
    blocker: Mapped[str] = mapped_column(Text, nullable=True)
    eta: Mapped[str] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_citations_json: Mapped[list] = mapped_column(json_type(), default=list)
    last_human_update_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_agent_nudge_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TaskSource(Base):
    __tablename__ = "task_sources"
    __table_args__ = (
        UniqueConstraint("task_id", "source_system", "external_id", name="uq_task_source_external"),
        Index("ix_task_sources_slack_thread", "slack_thread_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"), nullable=True, index=True)
    source_system: Mapped[str] = mapped_column(String(50), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(Text, nullable=True)
    slack_thread_id: Mapped[str] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Commitment(Base):
    __tablename__ = "commitments"
    __table_args__ = (
        Index("ix_commitments_org_status", "organization_id", "status"),
        Index("ix_commitments_org_due_date", "organization_id", "due_date"),
        Index("ix_commitments_org_owner", "organization_id", "owner_person_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    owner_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    requester_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    commitment_text: Mapped[str] = mapped_column(Text)
    source_system: Mapped[str] = mapped_column(String(50), index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=True)
    source_message_id: Mapped[str] = mapped_column(String(255), nullable=True)
    due_date: Mapped[date] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="open", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extraction_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CommitmentParticipant(Base):
    __tablename__ = "commitment_participants"
    __table_args__ = (UniqueConstraint("commitment_id", "person_id", "role", name="uq_commitment_participant_role"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    commitment_id: Mapped[str] = mapped_column(ForeignKey("commitments.id"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    role: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FollowUp(Base):
    __tablename__ = "follow_ups"
    __table_args__ = (
        Index("ix_follow_ups_org_status", "organization_id", "status"),
        Index("ix_follow_ups_org_target", "organization_id", "target_person_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    target_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    requester_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    context_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    source_system: Mapped[str] = mapped_column(String(50), default="slack")
    channel_id: Mapped[str] = mapped_column(String(255), nullable=True)
    external_message_id: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class FollowUpMessage(Base):
    __tablename__ = "follow_up_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    follow_up_id: Mapped[str] = mapped_column(ForeignKey("follow_ups.id"), index=True)
    sender_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    source_system: Mapped[str] = mapped_column(String(50), default="slack")
    source_external_id: Mapped[str] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TaskStatusSnapshot(Base):
    __tablename__ = "task_status_snapshots"
    __table_args__ = (Index("ix_task_status_snapshots_task_created", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    latest_known_status: Mapped[str] = mapped_column(Text, nullable=True)
    blocker: Mapped[str] = mapped_column(Text, nullable=True)
    eta: Mapped[str] = mapped_column(String(255), nullable=True)
    owner_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    linked_jira_ticket: Mapped[str] = mapped_column(String(50), nullable=True, index=True)
    source_citations_json: Mapped[list] = mapped_column(json_type(), default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    last_human_update_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_agent_nudge_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MemoryEvent(Base):
    __tablename__ = "memory_events"
    __table_args__ = (
        Index("ix_memory_events_org_task", "organization_id", "task_id"),
        Index("ix_memory_events_org_person", "organization_id", "person_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    payload_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    source_system: Mapped[str] = mapped_column(String(50), nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ActionProposal(Base):
    __tablename__ = "action_proposals"
    __table_args__ = (
        Index("ix_action_proposals_org_status", "organization_id", "status"),
        Index("ix_action_proposals_org_target", "organization_id", "target_system"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    proposal_type: Mapped[str] = mapped_column(String(80), index=True)
    target_system: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    source_citations_json: Mapped[list] = mapped_column(json_type(), default=list)
    status: Mapped[str] = mapped_column(String(50), default="pending_approval", index=True)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    requested_by_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    approved_by_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    external_url: Mapped[str] = mapped_column(Text, nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_org_created", "organization_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    actor_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String, nullable=True)
    before_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    after_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    metadata_json: Mapped[dict] = mapped_column(json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

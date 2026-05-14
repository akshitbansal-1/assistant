from __future__ import annotations

import secrets
from datetime import timedelta, timezone

from sqlalchemy.orm import Session

from app.models.account import User, UserInvitation
from app.models.communication import Organization, OrganizationMember, Person
from app.schemas.admin import InviteUserRequest
from app.services.account import AccountService
from app.services.communication import CommunicationLoopService
from app.utils.datetime import utcnow


class AdminService:
    def __init__(self) -> None:
        self.accounts = AccountService()
        self.loop = CommunicationLoopService()

    def invite_user(
        self,
        db: Session,
        *,
        organization: Organization,
        invited_by: User,
        payload: InviteUserRequest,
    ) -> UserInvitation:
        manager = self._manager_for_email(db, organization.id, payload.manager_email)
        existing = (
            db.query(UserInvitation)
            .filter(
                UserInvitation.organization_id == organization.id,
                UserInvitation.email == payload.email,
                UserInvitation.status == "pending",
            )
            .first()
        )
        if existing:
            existing.name = payload.name or existing.name
            existing.role = payload.role
            existing.manager_person_id = manager.id if manager else None
            existing.expires_at = utcnow() + timedelta(days=14)
            existing.updated_at = utcnow()
            db.flush()
            return existing

        invite = UserInvitation(
            organization_id=organization.id,
            email=payload.email,
            name=payload.name,
            role=payload.role,
            manager_person_id=manager.id if manager else None,
            token=secrets.token_urlsafe(32),
            invited_by_user_id=invited_by.id,
            expires_at=utcnow() + timedelta(days=14),
        )
        db.add(invite)
        db.flush()
        return invite

    def accept_invite(self, db: Session, *, token: str, name: str | None = None) -> User:
        invite = db.query(UserInvitation).filter(UserInvitation.token == token).first()
        if not invite:
            raise ValueError("Invite not found")
        if invite.status != "pending":
            raise ValueError("Invite is no longer pending")
        expires_at = invite.expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at < utcnow():
            invite.status = "expired"
            db.flush()
            raise ValueError("Invite has expired")

        user = self.accounts.get_or_create_user(db, invite.email, name or invite.name)
        member = (
            db.query(OrganizationMember)
            .filter(OrganizationMember.organization_id == invite.organization_id, OrganizationMember.user_id == user.id)
            .first()
        )
        if not member:
            db.add(OrganizationMember(organization_id=invite.organization_id, user_id=user.id, role=invite.role))
        else:
            member.role = invite.role

        person = self.loop.get_or_create_person(
            db,
            invite.organization_id,
            name or invite.name or invite.email,
            user_id=user.id,
            email=invite.email,
        )
        if person:
            person.manager_person_id = invite.manager_person_id
        invite.status = "accepted"
        invite.accepted_at = utcnow()
        invite.updated_at = utcnow()
        db.flush()
        return user

    def organization_people(self, db: Session, organization_id: str) -> list[Person]:
        return (
            db.query(Person)
            .filter(Person.organization_id == organization_id)
            .order_by(Person.display_name.asc())
            .all()
        )

    def organization_members(self, db: Session, organization_id: str) -> list[tuple[OrganizationMember, User, Person | None]]:
        rows: list[tuple[OrganizationMember, User, Person | None]] = []
        members = (
            db.query(OrganizationMember)
            .filter(OrganizationMember.organization_id == organization_id)
            .order_by(OrganizationMember.created_at.asc())
            .all()
        )
        for member in members:
            user = db.query(User).filter(User.id == member.user_id).first()
            person = (
                db.query(Person)
                .filter(Person.organization_id == organization_id, Person.user_id == member.user_id)
                .first()
            )
            if user:
                rows.append((member, user, person))
        return rows

    def pending_invites(self, db: Session, organization_id: str) -> list[UserInvitation]:
        return (
            db.query(UserInvitation)
            .filter(UserInvitation.organization_id == organization_id, UserInvitation.status == "pending")
            .order_by(UserInvitation.created_at.desc())
            .all()
        )

    def _manager_for_email(self, db: Session, organization_id: str, email: str | None) -> Person | None:
        if not email:
            return None
        manager = db.query(Person).filter(Person.organization_id == organization_id, Person.email == email).first()
        if manager:
            return manager
        return self.loop.get_or_create_person(db, organization_id, email, email=email)

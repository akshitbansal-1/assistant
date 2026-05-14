from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.communication import OrganizationMember, Person


class AuthorizationService:
    manager_roles = {"admin", "manager"}

    def member_role_for_person(self, db: Session, organization_id: str, person: Person | None) -> str | None:
        if not person or not person.user_id:
            return None
        member = (
            db.query(OrganizationMember)
            .filter(OrganizationMember.organization_id == organization_id, OrganizationMember.user_id == person.user_id)
            .first()
        )
        return member.role if member else None

    def can_ask_whereis(self, db: Session, organization_id: str, requester: Person | None, target: Person | None) -> bool:
        if not requester:
            return False
        role = self.member_role_for_person(db, organization_id, requester)
        if role in self.manager_roles:
            return True
        if target and requester.id == target.id:
            return True
        if target and target.manager_person_id == requester.id:
            return True
        return False

    def can_approve_actions(self, db: Session, organization_id: str, requester: Person | None) -> bool:
        if not requester:
            return False
        return self.member_role_for_person(db, organization_id, requester) in self.manager_roles


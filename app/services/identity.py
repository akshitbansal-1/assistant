from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import getaddresses
from typing import Any

from sqlalchemy.orm import Session

from app.models.account import LinkedAccount, User
from app.utils.idempotency import fingerprint_for_text

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


@dataclass(frozen=True)
class PersonIdentity:
    global_id: str
    display_name: str
    email: str | None
    aliases: tuple[str, ...]


class IdentityService:
    """Canonicalize people and owner aliases without adding identity infra yet."""

    OWNER_METADATA_KEYS = {
        "email",
        "account_email",
        "user_email",
        "user_id",
        "slack_user_id",
        "notion_user_id",
        "jira_account_id",
    }

    def owner_global_id(self, user: User) -> str:
        return f"user:{user.id}"

    def owner_aliases(self, db: Session, user: User) -> set[str]:
        aliases = {self._norm(user.email)}
        if user.name:
            aliases.add(self._norm(user.name))
        accounts = (
            db.query(LinkedAccount)
            .filter(LinkedAccount.user_id == user.id, LinkedAccount.is_active.is_(True))
            .all()
        )
        for account in accounts:
            aliases.update(self._alias_values(account.account_identifier, account.label))
            aliases.update(self._owner_metadata_aliases(account.metadata_json or {}))
        return {alias for alias in aliases if alias}

    def owner_metadata(self, user: User, account_identifier: str, label: str, metadata: dict[str, Any]) -> dict[str, Any]:
        aliases = sorted(
            self._alias_values(user.email, user.name, account_identifier, label)
            | self._owner_metadata_aliases(metadata or {})
        )
        return {**(metadata or {}), "owner_global_id": self.owner_global_id(user), "owner_aliases": aliases}

    def normalize_people(self, people: list[str]) -> list[str]:
        identities = [self.normalize_person(person) for person in self._expand_people(people)]
        seen: set[str] = set()
        normalized: list[str] = []
        for identity in identities:
            if identity.global_id in seen:
                continue
            seen.add(identity.global_id)
            normalized.append(self.display_label(identity))
        return normalized

    def normalize_person(self, value: str) -> PersonIdentity:
        cleaned = " ".join(str(value or "").strip().split())
        name, email = self._parse_address(cleaned)
        aliases = self._alias_values(cleaned, name, email)
        if email:
            display_name = name or email
            return PersonIdentity(
                global_id=f"email:{email}",
                display_name=display_name,
                email=email,
                aliases=tuple(sorted(aliases)),
            )
        display_name = cleaned
        return PersonIdentity(
            global_id=f"name:{fingerprint_for_text(cleaned.lower())[:16]}",
            display_name=display_name,
            email=None,
            aliases=tuple(sorted(aliases)),
        )

    def display_label(self, identity: PersonIdentity) -> str:
        if identity.email and identity.display_name and identity.display_name.lower() != identity.email:
            return f"{identity.display_name} <{identity.email}>"
        return identity.display_name

    def is_self_reference(self, value: str, owner_aliases: set[str]) -> bool:
        identity = self.normalize_person(value)
        return any(alias in owner_aliases for alias in identity.aliases)

    def metadata(self, identity: PersonIdentity) -> dict[str, Any]:
        return {
            "person_global_id": identity.global_id,
            "person_email": identity.email,
            "person_aliases": list(identity.aliases),
        }

    def _expand_people(self, people: list[str]) -> list[str]:
        expanded: list[str] = []
        for value in people:
            raw = str(value or "").strip()
            if not raw:
                continue
            addresses = getaddresses([raw])
            parsed = [(name.strip(), email.strip().lower()) for name, email in addresses if email and _EMAIL_RE.match(email.strip().lower())]
            if parsed:
                expanded.extend(f"{name} <{email}>" if name else email for name, email in parsed)
            else:
                expanded.append(raw)
        return expanded

    def _parse_address(self, value: str) -> tuple[str, str | None]:
        parsed = getaddresses([value])
        for name, email in parsed:
            email = email.strip().lower()
            if _EMAIL_RE.match(email):
                return (" ".join(name.strip().split()), email)
        lowered = value.lower()
        if _EMAIL_RE.match(lowered):
            return "", lowered
        return value, None

    def _owner_metadata_aliases(self, metadata: dict[str, Any]) -> set[str]:
        aliases: set[str] = set()
        for key, value in metadata.items():
            if key in self.OWNER_METADATA_KEYS:
                aliases.update(self._alias_values(str(value)))
            elif key == "owner_aliases" and isinstance(value, list):
                aliases.update(self._alias_values(*(str(item) for item in value)))
        return aliases

    def _alias_values(self, *values: str | None) -> set[str]:
        aliases: set[str] = set()
        for value in values:
            cleaned = " ".join(str(value or "").strip().split())
            if not cleaned:
                continue
            aliases.add(self._norm(cleaned))
            name, email = self._parse_address(cleaned)
            if name:
                aliases.add(self._norm(name))
            if email:
                aliases.add(self._norm(email))
        return aliases

    def _norm(self, value: str | None) -> str:
        return " ".join(str(value or "").strip().lower().split())

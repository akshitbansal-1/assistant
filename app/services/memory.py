from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.memory import KnownEntity, TrackedTask
from app.services.identity import IdentityService
from app.utils.datetime import utcnow

_AUTOMATED_ENTITY_RE = re.compile(
    r"(no.?reply|noreply|do.not.reply|mailer.daemon|postmaster|bounce|"
    r"notifications?@|alert(s)?@|automated@|newsletter@|digest@|"
    r"statement@|donotreply|<no-reply)",
    re.IGNORECASE,
)


class MemoryService:
    actionable = {"task", "follow_up", "blocker"}

    def __init__(self) -> None:
        self.identity = IdentityService()

    def update_entities(
        self,
        db: Session,
        user_id: str,
        items: list[dict[str, Any]],
        user_email: str = "",
        self_aliases: set[str] | None = None,
    ) -> None:
        self_aliases = self_aliases or ({user_email.lower()} if user_email else set())
        existing_entities = db.query(KnownEntity).filter(KnownEntity.user_id == user_id).all()
        for entity in existing_entities:
            aliases = [entity.name, *(entity.aliases_json or [])]
            if any(self.identity.is_self_reference(alias, self_aliases) for alias in aliases):
                db.delete(entity)
        db.flush()
        existing_entities = db.query(KnownEntity).filter(KnownEntity.user_id == user_id).all()

        for item in items:
            if item.get("classification") == "info" and not item.get("needs_action"):
                continue
            for person in item.get("people", []):
                if not person:
                    continue
                identity = self.identity.normalize_person(person)
                if any(alias in self_aliases for alias in identity.aliases):
                    continue
                if _AUTOMATED_ENTITY_RE.search(person):
                    continue
                normalized_aliases = set(identity.aliases)
                entity = next(
                    (
                        candidate
                        for candidate in existing_entities
                        if candidate.metadata_json.get("person_global_id") == identity.global_id
                        or candidate.name.strip().lower() in normalized_aliases
                        or bool(normalized_aliases.intersection({alias.strip().lower() for alias in (candidate.aliases_json or [])}))
                    ),
                    None,
                )
                if entity:
                    entity.name = self.identity.display_label(identity)
                    entity.aliases_json = sorted(set(entity.aliases_json or []).union(identity.aliases))
                    entity.metadata_json = {**(entity.metadata_json or {}), **self.identity.metadata(identity)}
                    entity.updated_at = utcnow()
                else:
                    entity = KnownEntity(
                        user_id=user_id,
                        name=self.identity.display_label(identity),
                        entity_type="person",
                        aliases_json=list(identity.aliases),
                        metadata_json=self.identity.metadata(identity),
                    )
                    db.add(entity)
                    existing_entities.append(entity)
        db.flush()

    def update_tasks(
        self,
        db: Session,
        user_id: str,
        items: list[dict[str, Any]],
        self_aliases: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        already_tracked: list[dict[str, Any]] = []
        for item in items:
            if item.get("classification") not in self.actionable and not item.get("needs_action"):
                continue
            people = [
                person
                for person in item.get("people", [])
                if not self_aliases or not self.identity.is_self_reference(person, self_aliases)
            ]
            task = (
                db.query(TrackedTask)
                .filter(TrackedTask.user_id == user_id, TrackedTask.canonical_key == item["dedupe_key"])
                .first()
            )
            refs = item.get("metadata", {}).get("sources", [])
            if task:
                already_tracked.append(item)
                task.title = item["title"]
                task.status = "blocked" if item.get("classification") == "blocker" else "open"
                task.people_json = people
                task.source_refs_json = refs
                task.latest_summary = item.get("summary")
                task.last_seen_at = utcnow()
            else:
                db.add(
                    TrackedTask(
                        user_id=user_id,
                        canonical_key=item["dedupe_key"],
                        title=item["title"],
                        status="blocked" if item.get("classification") == "blocker" else "open",
                        people_json=people,
                        source_refs_json=refs,
                        latest_summary=item.get("summary"),
                    )
                )
        db.flush()
        return already_tracked

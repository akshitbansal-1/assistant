from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.memory import KnownEntity, TrackedTask
from app.utils.datetime import utcnow


class MemoryService:
    actionable = {"task", "follow_up", "blocker"}

    def update_entities(self, db: Session, user_id: str, items: list[dict[str, Any]]) -> None:
        for item in items:
            for person in item.get("people", []):
                if not person:
                    continue
                entity = (
                    db.query(KnownEntity)
                    .filter(KnownEntity.user_id == user_id, KnownEntity.name == person)
                    .first()
                )
                if entity:
                    entity.updated_at = utcnow()
                else:
                    db.add(KnownEntity(user_id=user_id, name=person, entity_type="person"))
        db.flush()

    def update_tasks(self, db: Session, user_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        already_tracked: list[dict[str, Any]] = []
        for item in items:
            if item.get("classification") not in self.actionable and not item.get("needs_action"):
                continue
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
                task.people_json = item.get("people", [])
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
                        people_json=item.get("people", []),
                        source_refs_json=refs,
                        latest_summary=item.get("summary"),
                    )
                )
        db.flush()
        return already_tracked

from __future__ import annotations

from sqlalchemy.orm import Session

from app.llm.service import LLMService
from app.models.item import WorkItem


class IntelligenceService:
    def __init__(self) -> None:
        self.llm = LLMService()

    def classify(self, db: Session, item: WorkItem) -> WorkItem:
        payload = {
            "title": item.title,
            "content": item.content,
            "source": item.source,
            "people": item.people_json,
            "metadata": item.metadata_json,
        }
        result = self.llm.classify_item(payload)
        item.classification = result.get("classification", "info")
        item.needs_action = bool(result.get("needs_action", False))
        item.who_should_act = result.get("who_should_act") or None
        item.short_summary = result.get("short_summary") or item.title[:160]
        if result.get("people"):
            merged = list(dict.fromkeys((item.people_json or []) + result["people"]))
            item.people_json = merged
        db.flush()
        return item

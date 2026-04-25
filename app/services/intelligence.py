from __future__ import annotations

from sqlalchemy.orm import Session

from app.llm.service import LLMService
from app.models.item import WorkItem


class IntelligenceService:
    def __init__(self) -> None:
        self.llm = LLMService()

    def classify_all(self, db: Session, items: list[WorkItem]) -> list[WorkItem]:
        if not items:
            return items
        payloads = [
            {
                "title": item.title,
                "content": item.content,
                "source": item.source,
                "people": item.people_json,
                "metadata": item.metadata_json,
            }
            for item in items
        ]
        results = self.llm.classify_items(payloads)
        for item, result in zip(items, results):
            item.classification = result.get("classification", "info")
            item.needs_action = bool(result.get("needs_action", False))
            item.who_should_act = result.get("who_should_act") or None
            item.short_summary = result.get("short_summary") or item.title[:160]
            if result.get("people"):
                merged = list(dict.fromkeys((item.people_json or []) + result["people"]))
                item.people_json = merged
        db.flush()
        return items

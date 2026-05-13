from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.llm.service import LLMService
from app.models.account import User
from app.models.item import WorkItem
from app.schemas.communication import CommitmentExtractionResult
from app.services.communication import CommunicationLoopService


logger = logging.getLogger(__name__)


class CommitmentExtractionService:
    def __init__(self) -> None:
        self.llm = LLMService()
        self.loop = CommunicationLoopService()

    def extract_and_store(self, db: Session, user: User, items: list[WorkItem]) -> dict[str, Any]:
        if not items:
            logger.info("Skipping commitment extraction user=%s reason=no_items", user.email)
            return {"commitments": []}
        payload_items = [
            {
                "id": item.external_id,
                "source": item.source,
                "title": item.title,
                "content": item.content,
                "people": item.people_json or [],
                "timestamp": item.timestamp.isoformat() if item.timestamp else None,
                "metadata": item.metadata_json or {},
            }
            for item in items
            if item.needs_action or item.classification in {"task", "follow_up", "blocker", "decision"} or item.source in {"slack", "jira"}
        ]
        logger.info(
            "Preparing commitment extraction user=%s actionable_items=%d payload_items=%d",
            user.email,
            len(items),
            len(payload_items),
        )
        extraction = self.llm.extract_commitments(payload_items)
        validated = self._validate_extraction(extraction)
        logger.info(
            "Validated commitment extraction user=%s candidates=%d valid=%d",
            user.email,
            len(extraction.get("commitments", [])),
            len(validated["commitments"]),
        )
        self.loop.process_work_items(db, user, items, validated)
        return validated

    def _validate_extraction(self, extraction: dict[str, Any]) -> dict[str, Any]:
        valid = []
        for raw in extraction.get("commitments", []):
            try:
                valid.append(CommitmentExtractionResult(commitments=[raw]).commitments[0].model_dump(mode="json"))
            except Exception as exc:
                logger.warning("Dropping malformed commitment extraction error=%s raw_keys=%s", exc, sorted(raw.keys()) if isinstance(raw, dict) else type(raw).__name__)
                continue
        return {"commitments": valid}

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from app.models.communication import CommunicationTask, SearchDocument
from app.models.item import WorkItem
from app.utils.datetime import utcnow


TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}", re.IGNORECASE)


class SearchIndexService:
    """Cheap lexical fallback after structured task/person/Jira lookup misses."""

    def upsert_task(self, db: Session, task: CommunicationTask) -> SearchDocument:
        text = " ".join(
            part
            for part in [
                task.title,
                task.canonical_key,
                task.jira_key,
                task.project,
                task.latest_status,
                task.blocker,
                task.eta,
            ]
            if part
        )
        return self._upsert(
            db,
            organization_id=task.organization_id,
            user_id=task.user_id,
            entity_type="task",
            entity_id=task.id,
            source_system="memory",
            title=task.jira_key or task.title,
            body=text,
            metadata={"task_id": task.id, "jira_key": task.jira_key},
        )

    def upsert_work_item(self, db: Session, *, organization_id: str, user_id: str, item: WorkItem) -> SearchDocument:
        text = " ".join(part for part in [item.title, item.short_summary, item.content, item.dedupe_key] if part)
        return self._upsert(
            db,
            organization_id=organization_id,
            user_id=user_id,
            entity_type="work_item",
            entity_id=item.id,
            source_system=item.source,
            title=item.title,
            body=text,
            metadata={
                "work_item_id": item.id,
                "external_id": item.external_id,
                "source_url": (item.metadata_json or {}).get("source_url"),
                "timestamp": item.timestamp.isoformat() if item.timestamp else None,
            },
        )

    def search(
        self,
        db: Session,
        *,
        organization_id: str,
        query: str,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        terms = self._terms(query)
        if not terms:
            return []
        docs = (
            db.query(SearchDocument)
            .filter(SearchDocument.organization_id == organization_id)
            .order_by(SearchDocument.updated_at.desc())
            .limit(250)
            .all()
        )
        ranked: list[dict[str, Any]] = []
        for doc in docs:
            index = doc.term_index_json or {}
            score = sum(int(index.get(term, 0)) for term in terms)
            if score <= 0:
                continue
            ranked.append(
                {
                    "document": doc,
                    "score": score,
                    "entity_type": doc.entity_type,
                    "entity_id": doc.entity_id,
                    "title": doc.title,
                    "metadata": doc.metadata_json or {},
                }
            )
        ranked.sort(key=lambda item: (item["score"], item["document"].updated_at), reverse=True)
        return ranked[:limit]

    def _upsert(
        self,
        db: Session,
        *,
        organization_id: str,
        user_id: str,
        entity_type: str,
        entity_id: str,
        source_system: str | None,
        title: str,
        body: str,
        metadata: dict[str, Any],
    ) -> SearchDocument:
        doc = (
            db.query(SearchDocument)
            .filter(
                SearchDocument.organization_id == organization_id,
                SearchDocument.entity_type == entity_type,
                SearchDocument.entity_id == entity_id,
            )
            .first()
        )
        term_index = dict(Counter(self._terms(f"{title} {body}")))
        if doc:
            doc.user_id = user_id
            doc.source_system = source_system
            doc.title = title[:500]
            doc.body = body
            doc.term_index_json = term_index
            doc.metadata_json = metadata
            doc.updated_at = utcnow()
        else:
            doc = SearchDocument(
                organization_id=organization_id,
                user_id=user_id,
                entity_type=entity_type,
                entity_id=entity_id,
                source_system=source_system,
                title=title[:500],
                body=body,
                term_index_json=term_index,
                metadata_json=metadata,
            )
            db.add(doc)
        db.flush()
        return doc

    def _terms(self, text: str | None) -> list[str]:
        return [match.group(0).lower() for match in TOKEN_RE.finditer(text or "")]


from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import String, or_
from sqlalchemy.orm import Session

from app.models.communication import Commitment, CommunicationTask, Person, TaskSource, TaskStatusSnapshot
from app.models.item import WorkItem
from app.services.search import SearchIndexService
from app.utils.idempotency import extract_issue_keys


logger = logging.getLogger(__name__)


class RetrievalService:
    def __init__(self) -> None:
        self.search = SearchIndexService()

    def retrieve(
        self,
        db: Session,
        *,
        organization_id: str,
        user_id: str,
        person: str | None = None,
        jira_key: str | None = None,
        task_query: str | None = None,
        project: str | None = None,
        slack_thread: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        commitment_status: str | None = None,
        limit: int = 12,
    ) -> dict[str, Any]:
        person_record = self._find_person(db, organization_id, person) if person else None
        detected_keys = extract_issue_keys(task_query or "")
        key = (jira_key or (detected_keys[0] if detected_keys else None) or "").upper() or None
        logger.info(
            "Retrieval started org=%s user=%s person=%s jira_key=%s task_query=%s project=%s slack_thread=%s commitment_status=%s limit=%d",
            organization_id,
            user_id,
            person,
            key,
            task_query,
            project,
            slack_thread,
            commitment_status,
            limit,
        )

        task_query_obj = db.query(CommunicationTask).filter(CommunicationTask.organization_id == organization_id)
        if person_record:
            task_query_obj = task_query_obj.filter(CommunicationTask.owner_person_id == person_record.id)
        if key:
            task_query_obj = task_query_obj.filter(CommunicationTask.jira_key == key)
        elif task_query:
            like = f"%{task_query}%"
            task_query_obj = task_query_obj.filter(
                or_(CommunicationTask.title.ilike(like), CommunicationTask.canonical_key.ilike(like))
            )
        if project:
            task_query_obj = task_query_obj.filter(CommunicationTask.project.ilike(f"%{project}%"))
        tasks = task_query_obj.order_by(CommunicationTask.updated_at.desc()).limit(limit).all()

        if slack_thread:
            source_task_ids = [
                source.task_id
                for source in db.query(TaskSource)
                .filter(TaskSource.slack_thread_id == slack_thread)
                .order_by(TaskSource.created_at.desc())
                .limit(limit)
                .all()
            ]
            if source_task_ids:
                tasks = (
                    db.query(CommunicationTask)
                    .filter(CommunicationTask.id.in_(source_task_ids), CommunicationTask.organization_id == organization_id)
                    .all()
                )

        search_trace: list[dict[str, Any]] = []
        if not tasks and task_query:
            search_hits = self.search.search(db, organization_id=organization_id, query=task_query, limit=limit)
            search_trace = [
                {
                    "entity_type": hit["entity_type"],
                    "entity_id": hit["entity_id"],
                    "title": hit["title"],
                    "score": hit["score"],
                    "metadata": hit["metadata"],
                }
                for hit in search_hits
            ]
            task_ids_from_search = [hit["entity_id"] for hit in search_hits if hit["entity_type"] == "task"]
            work_item_ids_from_search = [hit["entity_id"] for hit in search_hits if hit["entity_type"] == "work_item"]
            if task_ids_from_search:
                tasks = (
                    db.query(CommunicationTask)
                    .filter(CommunicationTask.organization_id == organization_id, CommunicationTask.id.in_(task_ids_from_search))
                    .all()
                )
            if work_item_ids_from_search and not key:
                item_query = db.query(WorkItem).filter(WorkItem.user_id == user_id, WorkItem.id.in_(work_item_ids_from_search))

        task_ids = [task.id for task in tasks]
        commitment_query = db.query(Commitment).filter(Commitment.organization_id == organization_id)
        if task_ids:
            commitment_query = commitment_query.filter(Commitment.task_id.in_(task_ids))
        if person_record:
            commitment_query = commitment_query.filter(Commitment.owner_person_id == person_record.id)
        if commitment_status:
            commitment_query = commitment_query.filter(Commitment.status == commitment_status)
        commitments = commitment_query.order_by(Commitment.updated_at.desc()).limit(limit).all()

        item_query = locals().get("item_query") or db.query(WorkItem).filter(WorkItem.user_id == user_id)
        if key:
            item_query = item_query.filter(WorkItem.dedupe_key == f"issue:{key}")
        elif task_query:
            item_query = item_query.filter(or_(WorkItem.title.ilike(f"%{task_query}%"), WorkItem.content.ilike(f"%{task_query}%")))
        if person:
            item_query = item_query.filter(WorkItem.people_json.cast(String).ilike(f"%{person}%"))
        if start_at:
            item_query = item_query.filter(WorkItem.timestamp >= start_at)
        if end_at:
            item_query = item_query.filter(WorkItem.timestamp <= end_at)
        items = item_query.order_by(WorkItem.timestamp.desc()).limit(limit).all()

        snapshots = []
        if task_ids:
            snapshots = (
                db.query(TaskStatusSnapshot)
                .filter(TaskStatusSnapshot.task_id.in_(task_ids))
                .order_by(TaskStatusSnapshot.created_at.desc())
                .limit(limit)
                .all()
            )

        citations = self.citations_for(tasks, items)
        logger.info(
            "Retrieval finished org=%s tasks=%d commitments=%d snapshots=%d items=%d citations=%d search_hits=%d",
            organization_id,
            len(tasks),
            len(commitments),
            len(snapshots),
            len(items),
            len(citations),
            len(search_trace),
        )
        return {
            "person": person_record,
            "tasks": tasks,
            "commitments": commitments,
            "snapshots": snapshots,
            "items": items,
            "citations": citations,
            "search_trace": search_trace,
        }

    def citations_for(self, tasks: list[CommunicationTask], items: list[WorkItem]) -> list[dict[str, Any]]:
        citations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for task in tasks:
            for citation in task.source_citations_json or []:
                url = citation.get("url") or citation.get("source_url") or citation.get("external_id")
                if url and url not in seen:
                    seen.add(url)
                    citations.append(citation)
        for item in items:
            metadata = item.metadata_json or {}
            url = metadata.get("source_url") or f"{item.source}:{item.external_id}"
            if url in seen:
                continue
            seen.add(url)
            citations.append(
                {
                    "source": item.source,
                    "title": item.title,
                    "url": metadata.get("source_url"),
                    "external_id": item.external_id,
                    "timestamp": item.timestamp.isoformat() if item.timestamp else None,
                }
            )
        return citations

    def _find_person(self, db: Session, organization_id: str, person: str | None) -> Person | None:
        if not person:
            return None
        cleaned = person.strip().strip("@")
        return (
            db.query(Person)
            .filter(
                Person.organization_id == organization_id,
                or_(
                    Person.display_name.ilike(cleaned),
                    Person.display_name.ilike(f"%{cleaned}%"),
                    Person.email.ilike(cleaned),
                    Person.aliases_json.cast(String).ilike(f"%{cleaned}%"),
                    Person.source_ids_json.cast(String).ilike(f"%{cleaned}%"),
                ),
            )
            .first()
        )

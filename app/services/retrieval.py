from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.communication import Commitment, CommunicationTask, Person, TaskSource, TaskStatusSnapshot
from app.models.item import WorkItem
from app.llm.service import LLMService
from app.services.search import SearchIndexService
from app.utils.idempotency import extract_issue_keys


logger = logging.getLogger(__name__)


class RetrievalService:
    def __init__(self) -> None:
        self.search = SearchIndexService()
        self.llm = LLMService()
        self.settings = get_settings()

    def smart_retrieve(
        self,
        db: Session,
        *,
        organization_id: str,
        user_id: str,
        raw_query: str,
        limit: int = 12,
    ) -> dict:
        """Intent-aware retrieval: parse the free-form query, then delegate to retrieve().

        The LLM extracts person, jira_key, task_query, project, and commitment_status
        from ``raw_query``.  If intent parsing fails the raw query is used as task_query
        so the caller always gets a best-effort result.
        """
        bounded_query = raw_query[: self.settings.retrieval_intent_max_chars]
        try:
            intent = self.llm.extract_retrieval_intent(bounded_query)
        except Exception:
            logger.warning("smart_retrieve: intent parsing failed, using raw query")
            intent = {"intent": "general", "task_query": bounded_query}

        logger.info(
            "smart_retrieve org=%s intent=%s person=%s jira_key=%s task_query=%s",
            organization_id,
            intent.get("intent"),
            intent.get("person"),
            intent.get("jira_key"),
            intent.get("task_query"),
        )
        return self.retrieve(
            db,
            organization_id=organization_id,
            user_id=user_id,
            person=intent.get("person"),
            jira_key=intent.get("jira_key"),
            task_query=intent.get("task_query") or bounded_query,
            project=intent.get("project"),
            commitment_status=intent.get("commitment_status"),
            limit=limit,
        )

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
        retrieval_trace: list[dict[str, Any]] = [
            {
                "stage": "intent",
                "person": person,
                "resolved_person_id": person_record.id if person_record else None,
                "jira_key": key,
                "task_query": task_query,
                "project": project,
                "commitment_status": commitment_status,
            }
        ]
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
        retrieval_trace.append({"stage": "structured_task_query", "result_count": len(tasks), "used_jira_key": bool(key)})

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
            retrieval_trace.append(
                {
                    "stage": "slack_thread_lookup",
                    "slack_thread": slack_thread,
                    "source_task_count": len(source_task_ids),
                    "result_count": len(tasks),
                }
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
            retrieval_trace.append({"stage": "lexical_fallback", "result_count": len(search_hits), "hits": search_trace})

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
        budgeted_items, budget_trace = self._apply_context_budget(items)
        items = budgeted_items
        retrieval_trace.append(budget_trace)

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
        retrieval_trace.append({"stage": "citation_ranking", "result_count": len(citations)})
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
            "retrieval_trace": retrieval_trace,
        }

    def citations_for(self, tasks: list[CommunicationTask], items: list[WorkItem]) -> list[dict[str, Any]]:
        citations: list[dict[str, Any]] = []
        seen: set[str] = set()
        for task in tasks:
            for citation in task.source_citations_json or []:
                url = citation.get("url") or citation.get("source_url") or citation.get("external_id")
                if url and url not in seen:
                    seen.add(url)
                    citations.append({**citation, "task_match": True})
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
                    "task_match": False,
                }
            )
        return self._rank_citations(citations)

    def calibrate_confidence(
        self,
        *,
        task: CommunicationTask | None,
        latest_snapshot: TaskStatusSnapshot | None,
        citations: list[dict[str, Any]],
        items: list[WorkItem],
    ) -> float:
        base = max([task.confidence if task else 0.0, latest_snapshot.confidence if latest_snapshot else 0.0, 0.45])
        if not citations:
            return min(base, 0.55)
        source_bonus = 0.0
        citation_sources = {str(citation.get("source") or "").lower() for citation in citations}
        if "jira" in citation_sources:
            source_bonus += 0.15
        if "slack" in citation_sources:
            source_bonus += 0.08
        if any(citation.get("verified") for citation in citations):
            source_bonus += 0.10
        newest_age_days = self._newest_citation_age_days(citations)
        if newest_age_days is not None and newest_age_days <= 3:
            source_bonus += 0.05
        elif newest_age_days is not None and newest_age_days > 14:
            source_bonus -= 0.10
        if not task and items:
            base = min(base, 0.60)
        return round(max(0.0, min(0.97, base + source_bonus)), 2)

    def _apply_context_budget(self, items: list[WorkItem]) -> tuple[list[WorkItem], dict[str, Any]]:
        budget = self.settings.retrieval_context_max_chars
        total = 0
        kept: list[WorkItem] = []
        dropped = 0
        for item in items:
            item_size = len(item.title or "") + len(item.short_summary or "") + len(item.content or "")
            if kept and total + item_size > budget:
                dropped += 1
                continue
            kept.append(item)
            total += item_size
        return kept, {"stage": "context_budget", "max_chars": budget, "used_chars": total, "dropped_items": dropped}

    def _rank_citations(self, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        source_weights = {"jira": 40, "slack": 30, "github": 30, "gmail": 20, "notion": 15}

        def score(citation: dict[str, Any]) -> tuple[int, str]:
            source = str(citation.get("source") or "").lower()
            value = source_weights.get(source, 10)
            if citation.get("task_match"):
                value += 20
            if citation.get("verified"):
                value += 20
            if citation.get("url") or citation.get("source_url"):
                value += 5
            return value, str(citation.get("timestamp") or "")

        return sorted(citations, key=score, reverse=True)

    def _newest_citation_age_days(self, citations: list[dict[str, Any]]) -> float | None:
        timestamps: list[datetime] = []
        for citation in citations:
            raw = citation.get("timestamp")
            if not raw:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamps.append(parsed.astimezone(timezone.utc))
        if not timestamps:
            return None
        newest = max(timestamps)
        return (datetime.now(timezone.utc) - newest).total_seconds() / 86400

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

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.communication import MemoryEvent
from app.utils.datetime import utcnow


logger = logging.getLogger(__name__)


class AgentRunRecorder:
    """Records narrow agent job decisions without adding a new persistence model.

    MemoryEvent is the current audit surface for internal coordination memory.
    These records intentionally avoid external writes; they only document what an
    agent job considered and what it decided.
    """

    def start(
        self,
        db: Session,
        *,
        organization_id: str,
        user_id: str,
        agent_name: str,
        input_payload: dict[str, Any],
        task_id: str | None = None,
        person_id: str | None = None,
        source_system: str | None = None,
        source_url: str | None = None,
    ) -> MemoryEvent:
        return self._record(
            db,
            organization_id=organization_id,
            user_id=user_id,
            agent_name=agent_name,
            status="started",
            task_id=task_id,
            person_id=person_id,
            source_system=source_system,
            source_url=source_url,
            payload={"input": input_payload},
            confidence=1.0,
        )

    def finish(
        self,
        db: Session,
        run: MemoryEvent,
        *,
        output_payload: dict[str, Any],
        confidence: float = 1.0,
    ) -> MemoryEvent:
        return self._record(
            db,
            organization_id=run.organization_id,
            user_id=run.user_id,
            agent_name=str((run.payload_json or {}).get("agent_name") or "unknown"),
            status="finished",
            task_id=run.task_id,
            person_id=run.person_id,
            source_system=run.source_system,
            source_url=run.source_url,
            payload={
                "parent_event_id": run.id,
                "input": (run.payload_json or {}).get("input") or {},
                "output": output_payload,
            },
            confidence=confidence,
        )

    def fail(
        self,
        db: Session,
        run: MemoryEvent,
        *,
        error: str,
        output_payload: dict[str, Any] | None = None,
    ) -> MemoryEvent:
        return self._record(
            db,
            organization_id=run.organization_id,
            user_id=run.user_id,
            agent_name=str((run.payload_json or {}).get("agent_name") or "unknown"),
            status="failed",
            task_id=run.task_id,
            person_id=run.person_id,
            source_system=run.source_system,
            source_url=run.source_url,
            payload={
                "parent_event_id": run.id,
                "input": (run.payload_json or {}).get("input") or {},
                "output": output_payload or {},
                "error": error,
            },
            confidence=0.0,
        )

    def _record(
        self,
        db: Session,
        *,
        organization_id: str,
        user_id: str,
        agent_name: str,
        status: str,
        payload: dict[str, Any],
        task_id: str | None,
        person_id: str | None,
        source_system: str | None,
        source_url: str | None,
        confidence: float,
    ) -> MemoryEvent:
        event = MemoryEvent(
            organization_id=organization_id,
            user_id=user_id,
            task_id=task_id,
            person_id=person_id,
            event_type=f"agent.{agent_name}.{status}",
            payload_json={
                "agent_name": agent_name,
                "status": status,
                "recorded_at": utcnow().isoformat(),
                **payload,
            },
            source_system=source_system,
            source_url=source_url,
            confidence=confidence,
        )
        db.add(event)
        db.flush()
        logger.info("Recorded agent run event agent=%s status=%s event=%s", agent_name, status, event.id)
        return event

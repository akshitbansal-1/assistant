from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.llm.service import LLMService
from app.models.summary import DailySummary

_AUTOMATED_SENDER_RE = re.compile(
    r"(no.?reply|noreply|do.not.reply|mailer.daemon|postmaster|bounce|"
    r"notifications?@|alerts?@|support@|help@|info@|newsletter|automated)",
    re.IGNORECASE,
)


class SummaryService:
    def __init__(self) -> None:
        self.llm = LLMService()

    def build_summary_payload(
        self,
        deduped_items: list[dict[str, Any]],
        already_tracked: list[dict[str, Any]],
        user_email: str = "",
    ) -> dict[str, Any]:
        blockers = [self._view(item) for item in deduped_items if item["classification"] == "blocker"][:7]
        priority = [
            self._view(item)
            for item in deduped_items
            if item["needs_action"] or item["classification"] in {"task", "follow_up", "blocker"}
        ][:7]
        people_set = []
        seen_people = set()
        for item in priority:
            for person in item["people"]:
                if not person:
                    continue
                if person in seen_people:
                    continue
                if user_email and person.lower() == user_email.lower():
                    continue
                if _AUTOMATED_SENDER_RE.search(person):
                    continue
                seen_people.add(person)
                people_set.append(
                    {
                        "title": person,
                        "summary": f"Reach out regarding {item['title']}",
                        "source": item["source"],
                        "people": [person],
                        "needs_action": True,
                        "metadata": {"related_title": item["title"]},
                    }
                )
        payload = {
            "priority_actions": priority,
            "people_to_talk_to": people_set[:7],
            "blockers": blockers,
            "already_tracked_tasks": [self._view(item) for item in already_tracked][:7],
            "narrative": self._render_narrative(priority, blockers, people_set, already_tracked),
        }
        llm_payload = {"items": deduped_items, **payload}
        llm_result = self.llm.summarize(llm_payload)
        if llm_result.get("priority_actions"):
            return llm_result
        return payload

    def store_summary(
        self,
        db: Session,
        *,
        user_id: str,
        summary_date: date,
        period_start,
        period_end,
        summary_payload: dict[str, Any],
        human_readable: str,
        delivery_channel: str,
    ) -> DailySummary:
        summary = (
            db.query(DailySummary)
            .filter(DailySummary.user_id == user_id, DailySummary.summary_date == summary_date)
            .first()
        )
        if summary:
            summary.period_start = period_start
            summary.period_end = period_end
            summary.summary_json = summary_payload
            summary.human_readable = human_readable
            summary.delivery_channel = delivery_channel
            db.flush()
            return summary

        summary = DailySummary(
            user_id=user_id,
            summary_date=summary_date,
            period_start=period_start,
            period_end=period_end,
            summary_json=summary_payload,
            human_readable=human_readable,
            delivery_channel=delivery_channel,
        )
        db.add(summary)
        db.flush()
        return summary

    def render_human_readable(self, summary_payload: dict[str, Any]) -> str:
        lines = ["Daily Work Intelligence Summary", ""]
        sections = [
            ("Priority actions", summary_payload.get("priority_actions", [])),
            ("People to talk to", summary_payload.get("people_to_talk_to", [])),
            ("Blockers", summary_payload.get("blockers", [])),
            ("Already tracked tasks", summary_payload.get("already_tracked_tasks", [])),
        ]
        for heading, items in sections:
            lines.append(f"{heading}:")
            if not items:
                lines.append("- None")
                lines.append("")
                continue
            for item in items:
                lines.append(f"- {item['title']}: {item['summary']}")
            lines.append("")
        narrative = summary_payload.get("narrative")
        if narrative:
            lines.append("Narrative:")
            lines.append(narrative)
        return "\n".join(lines).strip()

    def _render_narrative(
        self,
        priority: list[dict[str, Any]],
        blockers: list[dict[str, Any]],
        people: list[dict[str, Any]],
        already_tracked: list[dict[str, Any]],
    ) -> str:
        return (
            f"{len(priority)} priority actions, "
            f"{len(blockers)} blockers, "
            f"{len(people)} people to talk to, "
            f"{len(already_tracked)} already tracked tasks."
        )

    def _view(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": item["title"],
            "summary": item.get("summary") or item.get("short_summary") or item["title"],
            "source": item["source"],
            "people": item.get("people", []),
            "needs_action": bool(item.get("needs_action")),
            "metadata": item.get("metadata", {}),
        }

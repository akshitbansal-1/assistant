from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.llm.service import LLMService
from app.models.summary import DailySummary
from app.services.identity import IdentityService

_AUTOMATED_SENDER_RE = re.compile(
    r"(no.?reply|noreply|do.not.reply|mailer.daemon|postmaster|bounce|"
    r"notifications?@|alerts?@|support@|help@|info@|newsletter|automated)",
    re.IGNORECASE,
)


class SummaryService:
    def __init__(self) -> None:
        self.llm = LLMService()
        self.identity = IdentityService()

    def build_summary_payload(
        self,
        deduped_items: list[dict[str, Any]],
        already_tracked: list[dict[str, Any]],
        user_email: str = "",
        self_aliases: set[str] | None = None,
    ) -> dict[str, Any]:
        self_aliases = self_aliases or ({user_email.lower()} if user_email else set())
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
                identity = self.identity.normalize_person(person)
                if identity.global_id in seen_people:
                    continue
                if any(alias in self_aliases for alias in identity.aliases):
                    continue
                if _AUTOMATED_SENDER_RE.search(person):
                    continue
                seen_people.add(identity.global_id)
                people_set.append(
                    {
                        "title": self.identity.display_label(identity),
                        "summary": f"Reach out regarding {item['title']}",
                        "source": item["source"],
                        "people": [self.identity.display_label(identity)],
                        "needs_action": True,
                        "metadata": {"related_title": item["title"], **self.identity.metadata(identity)},
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
            return self._sanitize_payload(llm_result, self_aliases)
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
        title = self._display_title(item)
        return {
            "title": title,
            "summary": item.get("summary") or item.get("short_summary") or title,
            "source": item["source"],
            "people": item.get("people", []),
            "needs_action": bool(item.get("needs_action")),
            "metadata": item.get("metadata", {}),
        }

    def _sanitize_payload(self, payload: dict[str, Any], self_aliases: set[str]) -> dict[str, Any]:
        sanitized = {**payload}
        for section in ("priority_actions", "blockers", "new_items", "already_tracked_tasks"):
            sanitized[section] = [self._sanitize_item(item, self_aliases) for item in payload.get(section, [])]

        people: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in payload.get("people_to_talk_to", []):
            candidates = item.get("people") or [item.get("title")]
            for candidate in candidates:
                if not candidate:
                    continue
                identity = self.identity.normalize_person(candidate)
                if identity.global_id in seen or any(alias in self_aliases for alias in identity.aliases):
                    continue
                if _AUTOMATED_SENDER_RE.search(candidate):
                    continue
                seen.add(identity.global_id)
                people.append(
                    {
                        **item,
                        "title": self.identity.display_label(identity),
                        "people": [self.identity.display_label(identity)],
                        "metadata": {**(item.get("metadata") or {}), **self.identity.metadata(identity)},
                    }
                )
        sanitized["people_to_talk_to"] = people[:7]
        sanitized["narrative"] = self._render_narrative(
            sanitized.get("priority_actions", []),
            sanitized.get("blockers", []),
            sanitized.get("people_to_talk_to", []),
            sanitized.get("already_tracked_tasks", []),
        )
        return sanitized

    def _sanitize_item(self, item: dict[str, Any], self_aliases: set[str]) -> dict[str, Any]:
        clean = {**item}
        clean["title"] = self._display_title(clean)
        clean["summary"] = clean.get("summary") or clean.get("short_summary") or clean["title"]
        clean["people"] = [
            person
            for person in clean.get("people", [])
            if not self.identity.is_self_reference(person, self_aliases)
        ]
        return clean

    def _display_title(self, item: dict[str, Any]) -> str:
        title = " ".join(str(item.get("title") or "").strip().split())
        if title.lower() not in {"", "(untitled)", "untitled", "(no subject)", "no subject"}:
            return title
        for key in ("summary", "short_summary", "content"):
            value = " ".join(str(item.get(key) or "").strip().split())
            if value:
                return re.split(r"(?<=[.!?])\s+", value, maxsplit=1)[0][:160]
        return "Work item"

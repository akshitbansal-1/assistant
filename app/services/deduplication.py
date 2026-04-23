from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.models.item import WorkItem


class DeduplicationService:
    PRIORITY = {"blocker": 0, "task": 1, "follow_up": 2, "decision": 3, "info": 4}

    def merge(self, items: list[WorkItem]) -> list[dict[str, Any]]:
        groups: dict[str, list[WorkItem]] = defaultdict(list)
        for item in items:
            groups[item.dedupe_key].append(item)

        merged: list[dict[str, Any]] = []
        for dedupe_key, bucket in groups.items():
            ordered = sorted(
                bucket,
                key=lambda item: (
                    self.PRIORITY.get(item.classification or "info", 99),
                    -item.timestamp.timestamp(),
                ),
            )
            primary = ordered[0]
            people = list(dict.fromkeys(person for item in bucket for person in (item.people_json or [])))
            sources = sorted(set(item.source for item in bucket))
            source_refs = [
                {"source": item.source, "external_id": item.external_id, "account_id": item.account_id}
                for item in bucket
            ]
            merged.append(
                {
                    "dedupe_key": dedupe_key,
                    "title": primary.title,
                    "summary": primary.short_summary or primary.title,
                    "content": "\n".join(item.content for item in bucket if item.content).strip(),
                    "classification": primary.classification or "info",
                    "needs_action": any(item.needs_action for item in bucket),
                    "who_should_act": primary.who_should_act or "you",
                    "people": people,
                    "source": "+".join(sources),
                    "thread_id": primary.thread_id,
                    "metadata": {
                        "issue_keys": sorted(
                            set(
                                issue
                                for item in bucket
                                for issue in (item.metadata_json or {}).get("issue_keys", [])
                            )
                        ),
                        "sources": source_refs,
                    },
                    "timestamp": max(item.timestamp for item in bucket),
                    "items": bucket,
                }
            )
        return sorted(merged, key=lambda item: item["timestamp"], reverse=True)

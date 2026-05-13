from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.models.account import LinkedAccount
from app.services.identity import IdentityService
from app.utils.idempotency import extract_issue_keys, fingerprint_for_text


class NormalizationService:
    def __init__(self) -> None:
        self.identity = IdentityService()

    def normalize_item(self, account: LinkedAccount, raw_item: dict[str, Any]) -> dict[str, Any]:
        timestamp = raw_item.get("timestamp")
        parsed_timestamp = self._parse_timestamp(timestamp)
        content = raw_item.get("content") or ""
        title = self._title(raw_item.get("title"), content)
        people = self.identity.normalize_people(raw_item.get("people") or [])
        metadata = raw_item.get("metadata") or {}
        people_identities = [
            self.identity.metadata(self.identity.normalize_person(person))
            for person in people
        ]
        issue_keys = extract_issue_keys(f"{title}\n{content}")
        dedupe_key = self._dedupe_key(account.source, raw_item.get("thread_id"), issue_keys, title, content)
        return {
            "source": account.source,
            "account_id": account.id,
            "external_id": str(raw_item["external_id"]),
            "timestamp": parsed_timestamp,
            "title": title,
            "content": content,
            "people": people,
            "thread_id": raw_item.get("thread_id"),
            "metadata": {**metadata, "issue_keys": issue_keys, "people_identities": people_identities},
            "fingerprint": fingerprint_for_text(title, content),
            "dedupe_key": dedupe_key,
        }

    def _title(self, raw_title: str | None, content: str) -> str:
        title = " ".join(str(raw_title or "").strip().split())
        if title and title.lower() not in {"(untitled)", "untitled", "(no subject)", "no subject"}:
            return title[:500]
        fallback = self._title_from_content(content)
        return fallback or "Untitled work item"

    def _title_from_content(self, content: str) -> str | None:
        cleaned = " ".join(str(content or "").strip().split())
        if not cleaned:
            return None
        issue_keys = extract_issue_keys(cleaned)
        sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0]
        if issue_keys and issue_keys[0] not in sentence:
            return f"{issue_keys[0]}: {sentence}"[:500]
        return sentence[:500]

    def _parse_timestamp(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        return datetime.now(timezone.utc)

    def _dedupe_key(
        self,
        source: str,
        thread_id: str | None,
        issue_keys: list[str],
        title: str,
        content: str,
    ) -> str:
        if issue_keys:
            return f"issue:{issue_keys[0]}"
        if thread_id:
            return f"thread:{source}:{thread_id}"
        return f"fingerprint:{fingerprint_for_text(title[:120], content[:240])[:24]}"

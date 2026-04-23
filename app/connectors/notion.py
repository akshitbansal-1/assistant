from __future__ import annotations

from datetime import datetime
from typing import Any

from app.connectors.base import BaseConnector
from app.models.account import LinkedAccount


class NotionConnector(BaseConnector):
    source = "notion"

    def fetch_recent_items(self, account: LinkedAccount, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        if self.use_sample_data(account):
            return self.sample_items()

        token = self.get_access_token(account)
        metadata = account.metadata_json or {}
        database_ids = metadata.get("database_ids", [])
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        items: list[dict[str, Any]] = []
        for database_id in database_ids:
            payload = self._request(
                "POST",
                f"https://api.notion.com/v1/databases/{database_id}/query",
                headers=headers,
                json={
                    "page_size": 100,
                    "filter": {
                        "or": [
                            {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": start_at.isoformat()}},
                            {"property": metadata.get("status_property", "Status"), "status": {"does_not_equal": "Done"}},
                        ]
                    }
                },
            )
            for page in payload.get("results", []):
                items.append(self._normalize_page(page))
        return items

    def _normalize_page(self, page: dict[str, Any]) -> dict[str, Any]:
        properties = page.get("properties", {})
        title = self._extract_title(properties) or "Notion task"
        people = self._extract_people(properties)
        status = self._extract_status(properties)
        content = f"Status: {status or 'unknown'}"
        return {
            "external_id": page["id"],
            "timestamp": page.get("last_edited_time"),
            "title": title,
            "content": content,
            "people": people,
            "thread_id": None,
            "metadata": {
                "url": page.get("url"),
                "status": status,
                "archived": page.get("archived", False),
            },
        }

    def _extract_title(self, properties: dict[str, Any]) -> str | None:
        for value in properties.values():
            if value.get("type") == "title":
                return "".join(part.get("plain_text", "") for part in value.get("title", []))
        return None

    def _extract_people(self, properties: dict[str, Any]) -> list[str]:
        people: list[str] = []
        for value in properties.values():
            if value.get("type") == "people":
                people.extend(person.get("name", person.get("id")) for person in value.get("people", []))
        return people

    def _extract_status(self, properties: dict[str, Any]) -> str | None:
        for value in properties.values():
            if value.get("type") == "status" and value.get("status"):
                return value["status"].get("name")
            if value.get("type") == "select" and value.get("select"):
                return value["select"].get("name")
        return None

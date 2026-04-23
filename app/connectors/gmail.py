from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from app.connectors.base import BaseConnector
from app.models.account import LinkedAccount


class GmailConnector(BaseConnector):
    source = "gmail"

    def fetch_recent_items(self, account: LinkedAccount, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        if self.use_sample_data(account):
            return self.sample_items()

        token = self.get_access_token(account)
        headers = {"Authorization": f"Bearer {token}"}
        query = f"after:{int(start_at.timestamp())} before:{int(end_at.timestamp())}"
        message_index = self._request(
            "GET",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={"q": query, "maxResults": 50},
        )
        items: list[dict[str, Any]] = []
        for message in message_index.get("messages", []):
            payload = self._request(
                "GET",
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message['id']}",
                headers=headers,
                params={"format": "full"},
            )
            items.append(self._normalize_message(payload))
        return items

    def _normalize_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {entry["name"].lower(): entry["value"] for entry in payload.get("payload", {}).get("headers", [])}
        internal_date = payload.get("internalDate")
        timestamp = datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc) if internal_date else datetime.now(timezone.utc)
        date_header = headers.get("date")
        if date_header:
            timestamp = parsedate_to_datetime(date_header).astimezone(timezone.utc)
        snippet = payload.get("snippet", "")
        subject = headers.get("subject", "(No subject)")
        people = [headers.get(name) for name in ("from", "to", "cc") if headers.get(name)]
        return {
            "external_id": payload["id"],
            "timestamp": timestamp.isoformat(),
            "title": subject,
            "content": snippet,
            "people": people,
            "thread_id": payload.get("threadId"),
            "metadata": {
                "labels": payload.get("labelIds", []),
                "headers": headers,
                "source_url": f"https://mail.google.com/mail/u/0/#all/{payload['id']}",
            },
        }

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.connectors.base import BaseConnector
from app.models.account import LinkedAccount


class SlackConnector(BaseConnector):
    source = "slack"

    def fetch_recent_items(self, account: LinkedAccount, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        if self.use_sample_data(account):
            return self.sample_items()

        token = self.get_access_token(account)
        metadata = account.metadata_json or {}
        headers = {"Authorization": f"Bearer {token}"}
        channel_ids = metadata.get("channel_ids", [])
        user_id = metadata.get("user_id")
        if not channel_ids:
            conversations = self._request(
                "GET",
                "https://slack.com/api/users.conversations",
                headers=headers,
                params={"types": "im,mpim,private_channel,public_channel", "limit": 100},
            )
            channel_ids = [channel["id"] for channel in conversations.get("channels", [])]

        items: list[dict[str, Any]] = []
        for channel_id in channel_ids[:25]:
            history = self._request(
                "GET",
                "https://slack.com/api/conversations.history",
                headers=headers,
                params={
                    "channel": channel_id,
                    "oldest": str(start_at.timestamp()),
                    "latest": str(end_at.timestamp()),
                    "inclusive": True,
                    "limit": 100,
                },
            )
            for message in history.get("messages", []):
                if not self._is_relevant(message, user_id, channel_id.startswith("D")):
                    continue
                items.append(self._normalize_message(message, channel_id))
        return items

    def _is_relevant(self, message: dict[str, Any], user_id: str | None, is_dm: bool) -> bool:
        text = message.get("text", "")
        if is_dm:
            return True
        if user_id and f"<@{user_id}>" in text:
            return True
        return bool(message.get("thread_ts"))

    def _normalize_message(self, message: dict[str, Any], channel_id: str) -> dict[str, Any]:
        timestamp = datetime.fromtimestamp(float(message["ts"]), tz=timezone.utc)
        return {
            "external_id": f"{channel_id}:{message['ts']}",
            "timestamp": timestamp.isoformat(),
            "title": message.get("text", "")[:120] or f"Slack message in {channel_id}",
            "content": message.get("text", ""),
            "people": [message.get("user")] if message.get("user") else [],
            "thread_id": message.get("thread_ts") or message.get("ts"),
            "metadata": {
                "channel_id": channel_id,
                "subtype": message.get("subtype"),
                "source_url": f"https://slack.com/app_redirect?channel={channel_id}&message_ts={message['ts']}",
            },
        }

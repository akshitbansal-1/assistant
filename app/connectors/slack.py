from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.connectors.base import BaseConnector
from app.models.account import LinkedAccount

_SLACK_MENTION_RE = re.compile(r"^<@([A-Z0-9]+)(?:\|[^>]+)?>$")
_SLACK_USER_ID_RE = re.compile(r"^@?(U[A-Z0-9]+|W[A-Z0-9]+)$")
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


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
            conversations = self._slack_request(
                "GET",
                "https://slack.com/api/users.conversations",
                headers=headers,
                params={"types": "im,mpim,private_channel,public_channel", "limit": 100},
            )
            channel_ids = [channel["id"] for channel in conversations.get("channels", [])]

        items: list[dict[str, Any]] = []
        for channel_id in channel_ids[:25]:
            history = self._slack_request(
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

    def resolve_user(self, account: LinkedAccount, reference: str) -> dict[str, Any] | None:
        cleaned = str(reference or "").strip()
        if not cleaned:
            return None
        if self.use_sample_data(account):
            handle = cleaned.strip("<@>").strip("@")
            return {"id": handle, "display_name": handle, "email": None, "aliases": [cleaned, handle]}

        token = self.get_access_token(account)
        headers = {"Authorization": f"Bearer {token}"}
        user_id = self._extract_user_id(cleaned)
        if user_id:
            payload = self._slack_request("GET", "https://slack.com/api/users.info", headers=headers, params={"user": user_id})
            return self._profile_from_member(payload.get("user") or {}, cleaned)

        if _EMAIL_RE.match(cleaned):
            try:
                payload = self._slack_request(
                    "GET",
                    "https://slack.com/api/users.lookupByEmail",
                    headers=headers,
                    params={"email": cleaned.strip("@")},
                )
                return self._profile_from_member(payload.get("user") or {}, cleaned)
            except ValueError as exc:
                if "users_not_found" not in str(exc):
                    raise

        lookup = cleaned.strip("@").lower()
        cursor = None
        for _ in range(10):
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = self._slack_request("GET", "https://slack.com/api/users.list", headers=headers, params=params)
            for member in payload.get("members", []):
                if member.get("deleted") or member.get("is_bot"):
                    continue
                profile = member.get("profile") or {}
                candidates = {
                    str(member.get("id") or "").lower(),
                    str(member.get("name") or "").lower(),
                    str(member.get("real_name") or "").lower(),
                    str(profile.get("display_name") or "").lower(),
                    str(profile.get("real_name") or "").lower(),
                    str(profile.get("email") or "").lower(),
                }
                if lookup in {candidate.strip("@") for candidate in candidates if candidate}:
                    return self._profile_from_member(member, cleaned)
            cursor = (payload.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return None

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

    def _slack_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        payload = self._request(method, url, **kwargs)
        if payload.get("ok") is False:
            raise ValueError(payload.get("error") or f"Slack API call failed: {url}")
        return payload

    def _extract_user_id(self, reference: str) -> str | None:
        mention = _SLACK_MENTION_RE.match(reference)
        if mention:
            return mention.group(1)
        user_id = _SLACK_USER_ID_RE.match(reference)
        if user_id:
            return user_id.group(1)
        return None

    def _profile_from_member(self, member: dict[str, Any], original: str) -> dict[str, Any] | None:
        user_id = member.get("id")
        if not user_id:
            return None
        profile = member.get("profile") or {}
        display_name = (
            profile.get("display_name")
            or profile.get("real_name")
            or member.get("real_name")
            or member.get("name")
            or user_id
        )
        aliases = [item for item in {original, user_id, member.get("name"), display_name, profile.get("email")} if item]
        return {
            "id": user_id,
            "display_name": display_name,
            "email": profile.get("email"),
            "aliases": aliases,
            "team_id": member.get("team_id") or profile.get("team"),
        }

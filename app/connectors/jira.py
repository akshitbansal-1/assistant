from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.connectors.base import BaseConnector
from app.models.account import LinkedAccount


class JiraConnector(BaseConnector):
    source = "jira"

    def fetch_recent_items(self, account: LinkedAccount, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        if self.use_sample_data(account):
            return self.sample_items()

        token = self.get_access_token(account)
        metadata = account.metadata_json or {}
        base_url = metadata.get("base_url")
        if not base_url:
            raise ValueError("Jira account metadata must include base_url")

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        jql = (
            f"(assignee = currentUser() OR updated >= '{start_at.strftime('%Y-%m-%d %H:%M')}') "
            f"AND updated >= '{start_at.strftime('%Y-%m-%d %H:%M')}' ORDER BY updated DESC"
        )
        payload = self._request(
            "GET",
            f"{base_url.rstrip('/')}/rest/api/3/search/jql",
            headers=headers,
            params={
                "jql": jql,
                "maxResults": 100,
                "fields": "summary,description,assignee,comment,updated,status",
            },
        )
        return [self._normalize_issue(issue, base_url) for issue in payload.get("issues", [])]

    def _normalize_issue(self, issue: dict[str, Any], base_url: str) -> dict[str, Any]:
        fields = issue.get("fields", {})
        assignee = fields.get("assignee") or {}
        comments = fields.get("comment", {}).get("comments", [])
        comment_text = "\n".join(comment.get("body", {}).get("content", [{}])[0].get("content", [{}])[0].get("text", "") for comment in comments[:3])
        description = self._extract_description(fields.get("description"))
        content = "\n".join(part for part in [description, comment_text] if part).strip()
        updated = fields.get("updated")
        timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else datetime.now(timezone.utc)
        return {
            "external_id": issue["id"],
            "timestamp": timestamp.isoformat(),
            "title": f"{issue['key']}: {fields.get('summary', '')}",
            "content": content,
            "people": [assignee.get("displayName")] if assignee.get("displayName") else [],
            "thread_id": issue["key"],
            "metadata": {
                "issue_key": issue["key"],
                "status": (fields.get("status") or {}).get("name"),
                "source_url": f"{base_url.rstrip('/')}/browse/{issue['key']}",
            },
        }

    def _extract_description(self, description: dict[str, Any] | None) -> str:
        if not description:
            return ""
        texts: list[str] = []
        for block in description.get("content", []):
            for piece in block.get("content", []):
                if piece.get("type") == "text":
                    texts.append(piece.get("text", ""))
        return " ".join(texts)

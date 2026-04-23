from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.gmail import GmailConnector
from app.connectors.jira import JiraConnector
from app.connectors.notion import NotionConnector
from app.connectors.slack import SlackConnector
from app.models.account import LinkedAccount
from app.models.item import WorkItem


class IngestionService:
    def __init__(self) -> None:
        self.connectors = {
            "gmail": GmailConnector(),
            "slack": SlackConnector(),
            "notion": NotionConnector(),
            "jira": JiraConnector(),
        }

    def fetch_raw_items(
        self,
        account: LinkedAccount,
        start_at: datetime,
        end_at: datetime,
    ) -> list[dict[str, Any]]:
        connector = self.connectors[account.source]
        return connector.fetch_recent_items(account, start_at, end_at)

    def upsert_item(self, db: Session, user_id: str, item: dict[str, Any]) -> WorkItem:
        existing = (
            db.query(WorkItem)
            .filter(
                WorkItem.source == item["source"],
                WorkItem.account_id == item["account_id"],
                WorkItem.external_id == item["external_id"],
            )
            .first()
        )
        if existing:
            existing.timestamp = item["timestamp"]
            existing.title = item["title"]
            existing.content = item["content"]
            existing.people_json = item["people"]
            existing.thread_id = item["thread_id"]
            existing.metadata_json = item["metadata"]
            existing.fingerprint = item["fingerprint"]
            existing.dedupe_key = item["dedupe_key"]
            return existing

        record = WorkItem(
            user_id=user_id,
            source=item["source"],
            account_id=item["account_id"],
            external_id=item["external_id"],
            timestamp=item["timestamp"],
            title=item["title"],
            content=item["content"],
            people_json=item["people"],
            thread_id=item["thread_id"],
            metadata_json=item["metadata"],
            fingerprint=item["fingerprint"],
            dedupe_key=item["dedupe_key"],
        )
        db.add(record)
        db.flush()
        return record

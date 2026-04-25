from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil.parser import parse as parse_dt
from sqlalchemy.orm import Session

from app.connectors.gmail import GmailConnector
from app.connectors.jira import JiraConnector
from app.connectors.notion import NotionConnector
from app.connectors.slack import SlackConnector
from app.models.account import LinkedAccount
from app.models.item import WorkItem
from app.services.oauth import OAuthService, TokenCipher


logger = logging.getLogger(__name__)


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
        db: Session,
        account: LinkedAccount,
        start_at: datetime,
        end_at: datetime,
    ) -> list[dict[str, Any]]:
        account = self._refresh_if_expired(db, account)
        connector = self.connectors[account.source]
        return connector.fetch_recent_items(account, start_at, end_at)

    def _refresh_if_expired(self, db: Session, account: LinkedAccount) -> LinkedAccount:
        if not account.refresh_token or not account.expires_at:
            return account

        now = datetime.now(timezone.utc)
        expires_at = account.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at > now + timedelta(minutes=5):
            return account

        cipher = TokenCipher()
        raw_refresh = cipher.decrypt(account.refresh_token)
        if not raw_refresh:
            return account

        try:
            new_payload = OAuthService().refresh_token(account.source, raw_refresh)
        except Exception as exc:
            logger.warning("Token refresh failed for account %s (%s): %s", account.id, account.source, exc)
            return account

        new_access = new_payload.get("access_token")
        if new_access:
            account.access_token = cipher.encrypt(new_access) or new_access
        if new_payload.get("refresh_token"):
            account.refresh_token = cipher.encrypt(new_payload["refresh_token"]) or new_payload["refresh_token"]
        raw_exp = new_payload.get("expires_at")
        if raw_exp:
            account.expires_at = parse_dt(raw_exp) if isinstance(raw_exp, str) else raw_exp
        db.flush()
        logger.info("Refreshed access token for account %s (%s)", account.id, account.source)
        return account

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

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
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


class AccountAuthError(ValueError):
    """Raised when a linked account needs user re-authentication."""


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
        account = self._refresh_if_needed(db, account, reason="pre_fetch")
        connector = self.connectors[account.source]
        try:
            return connector.fetch_recent_items(account, start_at, end_at)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
            logger.warning(
                "Connector returned 401; refreshing token and retrying once account=%s source=%s",
                account.id,
                account.source,
            )
            account = self._refresh_if_needed(db, account, reason="401_retry", force=True)
            return connector.fetch_recent_items(account, start_at, end_at)

    def _refresh_if_expired(self, db: Session, account: LinkedAccount) -> LinkedAccount:
        return self._refresh_if_needed(db, account, reason="legacy_expiry_check")

    def _refresh_if_needed(
        self,
        db: Session,
        account: LinkedAccount,
        *,
        reason: str,
        force: bool = False,
    ) -> LinkedAccount:
        if not account.refresh_token:
            if force:
                raise AccountAuthError(
                    f"{account.source} account '{account.label}' needs reconnect: no refresh token is available."
                )
            return account

        now = datetime.now(timezone.utc)
        expires_at = account.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at is None:
            logger.info(
                "Refreshing token because expiry is missing account=%s source=%s reason=%s",
                account.id,
                account.source,
                reason,
            )

        should_refresh = force or expires_at is None or expires_at <= now + timedelta(minutes=5)
        if not should_refresh:
            return account

        cipher = TokenCipher()
        try:
            raw_refresh = cipher.decrypt(account.refresh_token)
        except Exception as exc:
            logger.warning("Refresh token decrypt failed account=%s source=%s error=%s", account.id, account.source, exc)
            if force:
                raise AccountAuthError(
                    f"{account.source} account '{account.label}' needs reconnect: stored refresh token could not be read."
                ) from exc
            return account
        if not raw_refresh:
            if force:
                raise AccountAuthError(
                    f"{account.source} account '{account.label}' needs reconnect: stored refresh token could not be read."
                )
            return account

        try:
            logger.info(
                "Refreshing access token account=%s source=%s reason=%s force=%s",
                account.id,
                account.source,
                reason,
                force,
            )
            new_payload = OAuthService().refresh_token(account.source, raw_refresh)
        except Exception as exc:
            logger.warning(
                "Token refresh failed account=%s source=%s reason=%s error=%s",
                account.id,
                account.source,
                reason,
                exc,
            )
            if force:
                raise AccountAuthError(
                    f"{account.source} account '{account.label}' needs reconnect: token refresh failed."
                ) from exc
            return account

        new_access = new_payload.get("access_token")
        if not new_access:
            logger.warning("Token refresh response missing access_token account=%s source=%s", account.id, account.source)
            if force:
                raise AccountAuthError(
                    f"{account.source} account '{account.label}' needs reconnect: token refresh returned no access token."
                )
            return account
        account.access_token = cipher.encrypt(new_access) or new_access
        if new_payload.get("refresh_token"):
            account.refresh_token = cipher.encrypt(new_payload["refresh_token"]) or new_payload["refresh_token"]
        raw_exp = new_payload.get("expires_at")
        if raw_exp:
            account.expires_at = parse_dt(raw_exp) if isinstance(raw_exp, str) else raw_exp
        db.flush()
        logger.info("Refreshed access token account=%s source=%s reason=%s", account.id, account.source, reason)
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

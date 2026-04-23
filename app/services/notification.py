from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from app.models.account import LinkedAccount
from app.models.summary import DailySummary
from app.services.oauth import TokenCipher


logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self) -> None:
        self.cipher = TokenCipher()

    def deliver(self, db: Session, summary: DailySummary, channel: str) -> None:
        if channel == "db":
            return
        if channel == "slack":
            self._deliver_to_slack(db, summary)
            return
        if channel == "email":
            logger.info("Email delivery requested for summary %s; storing in DB only for MVP", summary.id)

    def _deliver_to_slack(self, db: Session, summary: DailySummary) -> None:
        slack_account = (
            db.query(LinkedAccount)
            .filter(LinkedAccount.user_id == summary.user_id, LinkedAccount.source == "slack", LinkedAccount.is_active.is_(True))
            .first()
        )
        if not slack_account:
            logger.warning("Slack delivery skipped because no active Slack account is linked")
            return
        channel_id = (slack_account.metadata_json or {}).get("delivery_channel_id")
        if not channel_id:
            logger.warning("Slack delivery skipped because delivery_channel_id is not configured")
            return
        token = self.cipher.decrypt(slack_account.access_token)
        with httpx.Client(timeout=20) as client:
            response = client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"channel": channel_id, "text": summary.human_readable},
            )
            response.raise_for_status()

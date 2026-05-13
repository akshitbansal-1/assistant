from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.account import User
from app.models.item import WorkItem
from app.services.account import AccountService
from app.services.commitments import CommitmentExtractionService
from app.services.deduplication import DeduplicationService
from app.services.ingestion import IngestionService
from app.services.intelligence import IntelligenceService
from app.services.memory import MemoryService
from app.services.normalization import NormalizationService
from app.services.notification import NotificationService
from app.services.summary import SummaryService
from app.utils.datetime import lookback_window


logger = logging.getLogger(__name__)


class DailyWorkPipeline:
    def __init__(self) -> None:
        self.accounts = AccountService()
        self.ingestion = IngestionService()
        self.normalization = NormalizationService()
        self.intelligence = IntelligenceService()
        self.commitments = CommitmentExtractionService()
        self.deduplication = DeduplicationService()
        self.memory = MemoryService()
        self.summary = SummaryService()
        self.notifications = NotificationService()

    def ingest_data(self, db: Session, user_email: str, lookback_hours: int = 1, force_fetch: bool = False) -> list[WorkItem]:
        user = db.query(User).filter(User.email == user_email).first()
        if not user:
            raise ValueError(f"User not found: {user_email}")
        _, end_at = lookback_window(lookback_hours)
        records: list[WorkItem] = []
        accounts = self.accounts.active_accounts_for_user(db, user_email)
        logger.info(
            "Starting ingestion user=%s accounts=%d lookback_hours=%d force_fetch=%s",
            user_email,
            len(accounts),
            lookback_hours,
            force_fetch,
        )
        for account in accounts:
            if not force_fetch and account.last_fetched_at is not None:
                start_at = account.last_fetched_at
            else:
                start_at, _ = lookback_window(lookback_hours)
            logger.info("Fetching source=%s account_id=%s start=%s end=%s", account.source, account.id, start_at, end_at)
            raw_items = self.ingestion.fetch_raw_items(db, account, start_at, end_at)
            logger.info("Fetched source=%s account_id=%s raw_items=%d", account.source, account.id, len(raw_items))
            for raw_item in raw_items:
                normalized = self.normalization.normalize_item(account, raw_item)
                records.append(self.ingestion.upsert_item(db, user.id, normalized))
            account.last_fetched_at = end_at
            db.flush()
        db.flush()
        logger.info("Finished ingestion user=%s stored_items=%d", user_email, len(records))
        return records

    def normalize_data(self, items: list[WorkItem]) -> list[WorkItem]:
        return items

    def classify_items(self, db: Session, items: list[WorkItem]) -> list[WorkItem]:
        logger.info("Classifying work items count=%d", len(items))
        self.intelligence.classify_all(db, items)
        return items

    def deduplicate_items(self, items: list[WorkItem]) -> list[dict]:
        deduped = self.deduplication.merge(items)
        logger.info("Deduplicated work items input=%d output=%d", len(items), len(deduped))
        return deduped

    def generate_summary(self, db: Session, user_email: str, lookback_hours: int = 24, delivery_channel: str = "db") -> dict:
        user = db.query(User).filter(User.email == user_email).first()
        if not user:
            raise ValueError(f"User not found: {user_email}")

        items = (
            db.query(WorkItem)
            .filter(
                WorkItem.user_id == user.id,
                (WorkItem.needs_action.is_(True)) | (WorkItem.classification.in_(["task", "follow_up", "blocker", "decision"])),
            )
            .order_by(WorkItem.timestamp.desc())
            .all()
        )
        window_start, window_end = lookback_window(lookback_hours)
        recent_items = [
            item for item in items
            if self._as_utc(item.timestamp) >= window_start and self._as_utc(item.timestamp) <= window_end
        ]
        deduped_items = self.deduplicate_items(recent_items)
        self.accounts.ensure_owner_metadata(db, user)
        self_aliases = self.accounts.identity.owner_aliases(db, user)
        self.memory.update_entities(db, user.id, deduped_items, user_email=user_email, self_aliases=self_aliases)
        already_tracked = self.memory.update_tasks(db, user.id, deduped_items, self_aliases=self_aliases)
        summary_payload = self.summary.build_summary_payload(
            deduped_items,
            already_tracked,
            user_email=user_email,
            self_aliases=self_aliases,
        )
        human_readable = self.summary.render_human_readable(summary_payload)
        summary_record = self.summary.store_summary(
            db,
            user_id=user.id,
            summary_date=window_end.date(),
            period_start=window_start,
            period_end=window_end,
            summary_payload=summary_payload,
            human_readable=human_readable,
            delivery_channel=delivery_channel,
        )
        self.notifications.deliver(db, summary_record, delivery_channel)
        db.flush()
        logger.info(
            "Generated summary user=%s summary_id=%s items=%d deduped_items=%d priority_actions=%d blockers=%d",
            user_email,
            summary_record.id,
            len(recent_items),
            len(deduped_items),
            len(summary_payload.get("priority_actions", [])),
            len(summary_payload.get("blockers", [])),
        )
        return {
            "summary_id": summary_record.id,
            "summary_date": summary_record.summary_date.isoformat(),
            "counts": {
                "items": len(recent_items),
                "deduped_items": len(deduped_items),
                "priority_actions": len(summary_payload.get("priority_actions", [])),
                "blockers": len(summary_payload.get("blockers", [])),
            },
            "summary": summary_payload,
        }

    def _as_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _prune_info_items(self, db: Session, items: list[WorkItem]) -> list[WorkItem]:
        actionable = []
        for item in items:
            if not item.needs_action and item.classification == "info":
                db.delete(item)
            else:
                actionable.append(item)
        db.flush()
        logger.info("Pruned info items input=%d actionable=%d removed=%d", len(items), len(actionable), len(items) - len(actionable))
        return actionable

    def run(self, db: Session, user_email: str, lookback_hours: int = 1, delivery_channel: str = "db", force_fetch: bool = False) -> dict:
        logger.info(
            "Pipeline run started user=%s lookback_hours=%d delivery_channel=%s force_fetch=%s",
            user_email,
            lookback_hours,
            delivery_channel,
            force_fetch,
        )
        user = db.query(User).filter(User.email == user_email).first()
        if not user:
            raise ValueError(f"User not found: {user_email}")
        items = self.ingest_data(db, user_email, lookback_hours=lookback_hours, force_fetch=force_fetch)
        normalized_items = self.normalize_data(items)
        self.classify_items(db, normalized_items)
        actionable_items = self._prune_info_items(db, normalized_items)
        self.commitments.extract_and_store(db, user, actionable_items)
        result = self.generate_summary(db, user_email, lookback_hours=lookback_hours, delivery_channel=delivery_channel)
        logger.info("Pipeline run finished user=%s summary_id=%s", user_email, result["summary_id"])
        return result

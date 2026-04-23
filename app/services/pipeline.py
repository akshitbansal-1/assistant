from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.account import User
from app.models.item import WorkItem
from app.services.account import AccountService
from app.services.deduplication import DeduplicationService
from app.services.ingestion import IngestionService
from app.services.intelligence import IntelligenceService
from app.services.memory import MemoryService
from app.services.normalization import NormalizationService
from app.services.notification import NotificationService
from app.services.summary import SummaryService
from app.utils.datetime import lookback_window


class DailyWorkPipeline:
    def __init__(self) -> None:
        self.accounts = AccountService()
        self.ingestion = IngestionService()
        self.normalization = NormalizationService()
        self.intelligence = IntelligenceService()
        self.deduplication = DeduplicationService()
        self.memory = MemoryService()
        self.summary = SummaryService()
        self.notifications = NotificationService()

    def ingest_data(self, db: Session, user_email: str, lookback_hours: int = 24) -> list[WorkItem]:
        user = db.query(User).filter(User.email == user_email).first()
        if not user:
            raise ValueError(f"User not found: {user_email}")
        start_at, end_at = lookback_window(lookback_hours)
        records: list[WorkItem] = []
        for account in self.accounts.active_accounts_for_user(db, user_email):
            raw_items = self.ingestion.fetch_raw_items(account, start_at, end_at)
            for raw_item in raw_items:
                normalized = self.normalization.normalize_item(account, raw_item)
                records.append(self.ingestion.upsert_item(db, user.id, normalized))
        db.flush()
        return records

    def normalize_data(self, items: list[WorkItem]) -> list[WorkItem]:
        return items

    def classify_items(self, db: Session, items: list[WorkItem]) -> list[WorkItem]:
        for item in items:
            self.intelligence.classify(db, item)
        db.flush()
        return items

    def deduplicate_items(self, items: list[WorkItem]) -> list[dict]:
        return self.deduplication.merge(items)

    def generate_summary(self, db: Session, user_email: str, lookback_hours: int = 24, delivery_channel: str = "db") -> dict:
        user = db.query(User).filter(User.email == user_email).first()
        if not user:
            raise ValueError(f"User not found: {user_email}")

        items = (
            db.query(WorkItem)
            .filter(WorkItem.user_id == user.id)
            .order_by(WorkItem.timestamp.desc())
            .all()
        )
        window_start, window_end = lookback_window(lookback_hours)
        recent_items = [
            item for item in items
            if self._as_utc(item.timestamp) >= window_start and self._as_utc(item.timestamp) <= window_end
        ]
        deduped_items = self.deduplicate_items(recent_items)
        self.memory.update_entities(db, user.id, deduped_items)
        already_tracked = self.memory.update_tasks(db, user.id, deduped_items)
        summary_payload = self.summary.build_summary_payload(deduped_items, already_tracked)
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

    def run(self, db: Session, user_email: str, lookback_hours: int = 24, delivery_channel: str = "db") -> dict:
        items = self.ingest_data(db, user_email, lookback_hours=lookback_hours)
        normalized_items = self.normalize_data(items)
        self.classify_items(db, normalized_items)
        return self.generate_summary(db, user_email, lookback_hours=lookback_hours, delivery_channel=delivery_channel)

from __future__ import annotations

from app.db import SessionLocal
from app.models.account import User
from app.services.jira_hygiene import JiraHygieneService
from app.services.pipeline import DailyWorkPipeline
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.tasks.generate_daily_summaries")
def generate_daily_summaries() -> dict[str, int]:
    db = SessionLocal()
    pipeline = DailyWorkPipeline()
    processed = 0
    try:
        users = db.query(User).all()
        for user in users:
            pipeline.run(db, user.email, lookback_hours=24, delivery_channel="db")
            processed += 1
        db.commit()
    finally:
        db.close()
    return {"processed_users": processed}


@celery_app.task(name="app.workers.tasks.run_stale_alert_agent")
def run_stale_alert_agent() -> dict[str, int]:
    db = SessionLocal()
    processed = 0
    proposals = 0
    service = JiraHygieneService()
    try:
        users = db.query(User).all()
        for user in users:
            created = service.detect_stale_tickets(db, user)
            proposals += len(created)
            processed += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"processed_users": processed, "proposal_count": proposals}

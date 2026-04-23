from __future__ import annotations

from app.db import SessionLocal
from app.models.account import User
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

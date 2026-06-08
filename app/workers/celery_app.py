from celery import Celery
from celery.schedules import crontab
from datetime import timedelta

from app.config import get_settings


settings = get_settings()

celery_app = Celery("daily_work_intelligence")
celery_app.conf.update(
    broker_url=settings.redis_url,
    result_backend=settings.redis_url,
    timezone="UTC",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    beat_schedule={
        "run-daily-summary": {
            "task": "app.workers.tasks.generate_daily_summaries",
            "schedule": crontab(hour=settings.daily_summary_hour, minute=settings.daily_summary_minute),
        },
        "run-stale-alert-agent": {
            "task": "app.workers.tasks.run_stale_alert_agent",
            "schedule": timedelta(minutes=settings.stale_agent_interval_minutes),
        }
    },
)

celery_app.autodiscover_tasks(["app.workers"])

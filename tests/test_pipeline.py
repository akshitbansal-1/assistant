from app.db import SessionLocal
from app.schemas.account import AccountCreate
from app.services.account import AccountService
from app.services.pipeline import DailyWorkPipeline


def test_pipeline_end_to_end_idempotent():
    db = SessionLocal()
    accounts = AccountService()
    user_email = "demo@example.com"
    try:
        for source in ("gmail", "slack", "notion", "jira"):
            accounts.upsert_linked_account(
                db,
                AccountCreate(
                    user_email=user_email,
                    source=source,
                    label=f"{source} sample",
                    account_identifier=f"{source}-1",
                    metadata={"sample_mode": True},
                ),
            )
        db.commit()

        pipeline = DailyWorkPipeline()
        # Use 168h (1 week) so that sample data timestamps (a few days old) fall in the window
        result_one = pipeline.run(db, user_email=user_email, lookback_hours=168, delivery_channel="db")
        db.commit()
        result_two = pipeline.run(db, user_email=user_email, lookback_hours=168, delivery_channel="db")
        db.commit()

        # Jira sample has actionable items (blocker + task keywords) — at least one should survive pruning
        assert result_one["counts"]["items"] > 0
        # Idempotency: second run must produce the same summary counts
        assert result_two["counts"]["items"] == result_one["counts"]["items"]
        assert len(result_one["summary"]["priority_actions"]) <= 7
        assert "blockers" in result_one["summary"]
    finally:
        db.close()

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db import SessionLocal
from app.schemas.account import AccountCreate
from app.services.account import AccountService
from app.services.ingestion import AccountAuthError, IngestionService
from app.services.memory import MemoryService
from app.services.normalization import NormalizationService
from app.services.pipeline import DailyWorkPipeline
from app.services.summary import SummaryService
from app.models.memory import KnownEntity


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
        # Use 1000h so that sample data timestamps (from May 11, 2026) consistently fall in the window
        result_one = pipeline.run(db, user_email=user_email, lookback_hours=1000, delivery_channel="db")
        db.commit()
        result_two = pipeline.run(db, user_email=user_email, lookback_hours=1000, delivery_channel="db")
        db.commit()

        # Jira sample has actionable items (blocker + task keywords) — at least one should survive pruning
        assert result_one["counts"]["items"] > 0
        # Idempotency: second run must produce the same summary counts
        assert result_two["counts"]["items"] == result_one["counts"]["items"]
        assert len(result_one["summary"]["priority_actions"]) <= 7
        assert "blockers" in result_one["summary"]
    finally:
        db.close()


class _FakeConnector:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen_tokens: list[str | None] = []

    def fetch_recent_items(self, account, start_at, end_at):
        self.seen_tokens.append(account.access_token)
        next_response = self.responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response


def _linked_gmail_account(
    db,
    *,
    access_token="old-access",
    refresh_token="refresh-token",
    expires_at=None,
    account_identifier="gmail-demo",
):
    return AccountService().upsert_linked_account(
        db,
        AccountCreate(
            user_email="demo@example.com",
            source="gmail",
            label="Gmail",
            account_identifier=account_identifier,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        ),
    )


def test_ingestion_refreshes_when_expiry_is_missing(monkeypatch):
    db = SessionLocal()
    try:
        account = _linked_gmail_account(db, expires_at=None)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        def fake_refresh(self, provider, refresh_token):
            assert provider == "gmail"
            assert refresh_token == "refresh-token"
            return {"access_token": "new-access", "expires_at": expiry}

        monkeypatch.setattr("app.services.ingestion.OAuthService.refresh_token", fake_refresh)
        service = IngestionService()
        connector = _FakeConnector([[]])
        service.connectors["gmail"] = connector

        service.fetch_raw_items(db, account, datetime.now(timezone.utc), datetime.now(timezone.utc))

        assert connector.seen_tokens == ["new-access"]
        assert account.access_token == "new-access"
        assert account.expires_at is not None
    finally:
        db.close()


def test_ingestion_retries_once_after_401(monkeypatch):
    db = SessionLocal()
    try:
        account = _linked_gmail_account(
            db,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        request = httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages")
        response = httpx.Response(401, request=request)
        unauthorized = httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        monkeypatch.setattr(
            "app.services.ingestion.OAuthService.refresh_token",
            lambda self, provider, refresh_token: {
                "access_token": "retried-access",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            },
        )
        service = IngestionService()
        connector = _FakeConnector([unauthorized, [{"external_id": "ok"}]])
        service.connectors["gmail"] = connector

        items = service.fetch_raw_items(db, account, datetime.now(timezone.utc), datetime.now(timezone.utc))

        assert items == [{"external_id": "ok"}]
        assert connector.seen_tokens == ["old-access", "retried-access"]
    finally:
        db.close()


def test_ingestion_401_without_refresh_token_requires_reconnect():
    db = SessionLocal()
    try:
        account = _linked_gmail_account(db, refresh_token=None)
        request = httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages")
        response = httpx.Response(401, request=request)
        unauthorized = httpx.HTTPStatusError("Unauthorized", request=request, response=response)
        service = IngestionService()
        service.connectors["gmail"] = _FakeConnector([unauthorized])

        with pytest.raises(AccountAuthError, match="needs reconnect"):
            service.fetch_raw_items(db, account, datetime.now(timezone.utc), datetime.now(timezone.utc))
    finally:
        db.close()


def test_normalization_derives_titles_and_normalizes_email_people():
    db = SessionLocal()
    try:
        account = _linked_gmail_account(db, access_token=None, refresh_token=None)
        item = NormalizationService().normalize_item(
            account,
            {
                "external_id": "email-1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "title": "(Untitled)",
                "content": "Merge PR 288 by today as requested by AB. Please confirm.",
                "people": [
                    "Akshit Bansal <akshitbansal.private@gmail.com>, Other Person <other@example.com>",
                    "Other Person <other@example.com>",
                ],
            },
        )

        assert item["title"] == "Merge PR 288 by today as requested by AB."
        assert item["people"] == [
            "Akshit Bansal <akshitbansal.private@gmail.com>",
            "Other Person <other@example.com>",
        ]
        assert item["metadata"]["people_identities"][0]["person_global_id"] == "email:akshitbansal.private@gmail.com"
    finally:
        db.close()


def test_summary_filters_owner_aliases_and_adds_person_global_ids():
    db = SessionLocal()
    try:
        account = _linked_gmail_account(
            db,
            access_token=None,
            refresh_token=None,
            account_identifier="akshitbansal.private@gmail.com",
        )
        db.commit()
        owner_aliases = AccountService().identity.owner_aliases(db, account.user)
        payload = SummaryService().build_summary_payload(
            [
                {
                    "dedupe_key": "task:1",
                    "title": "(Untitled)",
                    "summary": "Merge PR 288 by today as requested by AB.",
                    "content": "Merge PR 288 by today as requested by AB.",
                    "classification": "task",
                    "needs_action": True,
                    "people": [
                        "Akshit Bansal <akshitbansal.private@gmail.com>",
                        "Teammate <teammate@example.com>",
                    ],
                    "source": "gmail",
                    "metadata": {},
                }
            ],
            [],
            user_email="demo@example.com",
            self_aliases=owner_aliases,
        )

        assert payload["priority_actions"][0]["title"] == "Merge PR 288 by today as requested by AB."
        assert [person["title"] for person in payload["people_to_talk_to"]] == ["Teammate <teammate@example.com>"]
        assert payload["people_to_talk_to"][0]["metadata"]["person_global_id"] == "email:teammate@example.com"
    finally:
        db.close()


def test_memory_removes_owner_aliases_from_known_entities():
    db = SessionLocal()
    try:
        account = _linked_gmail_account(
            db,
            access_token=None,
            refresh_token=None,
            account_identifier="akshitbansal2828@gmail.com",
        )
        stale_self = KnownEntity(
            user_id=account.user_id,
            name="Akshit Bansal <akshitbansal2828@gmail.com>",
            entity_type="person",
        )
        db.add(stale_self)
        db.flush()
        owner_aliases = AccountService().identity.owner_aliases(db, account.user)

        MemoryService().update_entities(
            db,
            account.user_id,
            [
                {
                    "classification": "task",
                    "needs_action": True,
                    "people": [
                        "Akshit Bansal <akshitbansal2828@gmail.com>",
                        "Teammate <teammate@example.com>",
                    ],
                }
            ],
            user_email="demo@example.com",
            self_aliases=owner_aliases,
        )

        names = [entity.name for entity in db.query(KnownEntity).filter(KnownEntity.user_id == account.user_id).all()]
        assert names == ["Teammate <teammate@example.com>"]
    finally:
        db.close()


def test_ingestion_refreshes_slack_user_token(monkeypatch):
    db = SessionLocal()
    try:
        # Create a linked Slack account where user_expires_at is expired (past)
        account = AccountService().upsert_linked_account(
            db,
            AccountCreate(
                user_email="demo@example.com",
                source="slack",
                label="Slack Workspace",
                account_identifier="slack-user-refresh-test",
                user_access_token="old-user-access",
                user_refresh_token="old-user-refresh",
                user_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            ),
        )
        db.commit()

        def fake_refresh(self, provider, refresh_token):
            assert provider == "slack"
            assert refresh_token == "old-user-refresh"
            return {
                "access_token": "new-user-access",
                "refresh_token": "new-user-refresh",
                "expires_in": 3600,
            }

        monkeypatch.setattr("app.services.ingestion.OAuthService.refresh_token", fake_refresh)
        service = IngestionService()
        
        # Mock connector.fetch_recent_items to see if the fresh token was passed down
        class SlackFakeConnector:
            def __init__(self):
                self.seen_tokens = []
            def fetch_recent_items(self, account, start_at, end_at):
                self.seen_tokens.append(account.user_access_token)
                return []

        connector = SlackFakeConnector()
        service.connectors["slack"] = connector

        service.fetch_raw_items(db, account, datetime.now(timezone.utc), datetime.now(timezone.utc))

        # Assertions
        assert len(connector.seen_tokens) == 1
        # Decrypted token passed to connector must be the new token
        from app.services.oauth import TokenCipher
        cipher = TokenCipher()
        assert cipher.decrypt(connector.seen_tokens[0]) == "new-user-access"
        assert cipher.decrypt(account.user_access_token) == "new-user-access"
        assert cipher.decrypt(account.user_refresh_token) == "new-user-refresh"
        assert account.user_expires_at is not None
    finally:
        db.close()

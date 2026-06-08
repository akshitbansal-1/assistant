from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.connectors.slack import SlackConnector
from app.db import SessionLocal
from app.main import app
from app.models.account import LinkedAccount, User
from app.models.communication import ActionProposal, FollowUpMessage
from app.schemas.account import AccountCreate
from app.services.account import AccountService
from app.services.communication import CommunicationLoopService
from app.services.ingestion import IngestionService
from app.services.oauth import OAuthService


def test_slack_and_jira_oauth_scopes_cover_phase8_operations(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "slack-client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "slack-secret")
    monkeypatch.setenv("SLACK_REDIRECT_URI", "https://example.com/slack/callback")
    monkeypatch.setenv("JIRA_CLIENT_ID", "jira-client")
    monkeypatch.setenv("JIRA_CLIENT_SECRET", "jira-secret")
    monkeypatch.setenv("JIRA_REDIRECT_URI", "https://example.com/jira/callback")
    get_settings.cache_clear()

    try:
        service = OAuthService()
        slack = service._provider_config("slack")
        jira = service._provider_config("jira")
    finally:
        get_settings.cache_clear()

    assert {"chat:write", "commands", "im:write", "users:read", "users:read.email"}.issubset(set(slack["scopes"]))
    assert "users:read.email" in slack["authorize_params"]["user_scope"]
    assert {"read:jira-work", "read:jira-user", "write:jira-work", "offline_access"}.issubset(set(jira["scopes"]))


def test_jira_oauth_identity_returns_all_accessible_sites(monkeypatch):
    monkeypatch.setenv("JIRA_CLIENT_ID", "jira-client")
    monkeypatch.setenv("JIRA_CLIENT_SECRET", "jira-secret")
    monkeypatch.setenv("JIRA_REDIRECT_URI", "https://example.com/jira/callback")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.atlassian.com/oauth/token/accessible-resources"
        assert request.headers["authorization"] == "Bearer jira-access"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "cloud-1",
                    "name": "Engineering Jira",
                    "url": "https://eng.atlassian.net",
                    "scopes": ["read:jira-work", "read:jira-user", "write:jira-work"],
                },
                {
                    "id": "cloud-2",
                    "name": "Support Jira",
                    "url": "https://support.atlassian.net",
                    "scopes": ["read:jira-work", "read:jira-user"],
                },
            ],
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)

    try:
        identities = OAuthService().fetch_account_identities("jira", {"access_token": "jira-access"})
    finally:
        get_settings.cache_clear()

    assert [identity["account_identifier"] for identity in identities] == ["cloud-1", "cloud-2"]
    assert identities[1]["extra_metadata"]["base_url"] == "https://api.atlassian.com/ex/jira/cloud-2"


def test_slack_oauth_identity_separates_bot_and_user_tokens():
    identity = OAuthService().fetch_account_identity(
        "slack",
        {
            "access_token": "xoxb-bot",
            "scope": "chat:write,commands",
            "team": {"id": "T123", "name": "Team One"},
            "authed_user": {
                "id": "U123",
                "access_token": "xoxp-user",
                "scope": "channels:history,users:read",
            },
        },
    )

    assert identity["account_identifier"] == "T123:U123"
    assert identity["user_access_token"] == "xoxp-user"
    assert identity["extra_metadata"]["team_id"] == "T123"


def test_slack_user_lookup_resolves_handle_to_stable_profile(monkeypatch):
    monkeypatch.setenv("ENABLE_MOCK_CONNECTORS", "false")
    get_settings.cache_clear()
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        assert request.headers["authorization"] == "Bearer slack-token"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "members": [
                    {
                        "id": "U123",
                        "name": "bob",
                        "real_name": "Bob Smith",
                        "team_id": "T123",
                        "profile": {"display_name": "Bob", "real_name": "Bob Smith", "email": "bob@example.com"},
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    import app.connectors.base as base_module

    original_client = base_module.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(base_module.httpx, "Client", client_factory)
    account = LinkedAccount(
        user_id="user-1",
        source="slack",
        label="Slack",
        account_identifier="T123",
        access_token="slack-token",
        metadata_json={},
    )

    try:
        profile = SlackConnector().resolve_user(account, "@bob")
    finally:
        get_settings.cache_clear()

    assert profile["id"] == "U123"
    assert profile["display_name"] == "Bob"
    assert profile["email"] == "bob@example.com"
    assert seen_urls[0].startswith("https://slack.com/api/users.list")


def test_slack_history_reads_use_user_token_when_available(monkeypatch):
    monkeypatch.setenv("ENABLE_MOCK_CONNECTORS", "false")
    get_settings.cache_clear()
    seen_auth_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth_headers.append(request.headers["authorization"])
        if str(request.url).startswith("https://slack.com/api/users.conversations"):
            return httpx.Response(200, json={"ok": True, "channels": [{"id": "D123"}]})
        if str(request.url).startswith("https://slack.com/api/conversations.history"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "messages": [{"ts": "1713800000.000100", "text": "Need update", "user": "U234"}],
                },
            )
        raise AssertionError(f"Unexpected URL {request.url}")

    transport = httpx.MockTransport(handler)
    import app.connectors.base as base_module

    original_client = base_module.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(base_module.httpx, "Client", client_factory)
    account = LinkedAccount(
        user_id="user-1",
        source="slack",
        label="Slack",
        account_identifier="T123:U123",
        access_token="bot-token",
        user_access_token="user-token",
        metadata_json={"user_id": "U123"},
    )

    try:
        items = SlackConnector().fetch_recent_items(
            account,
            datetime.fromtimestamp(1713799900, tz=timezone.utc),
            datetime.fromtimestamp(1713800100, tz=timezone.utc),
        )
    finally:
        get_settings.cache_clear()

    assert len(items) == 1
    assert seen_auth_headers == ["Bearer user-token", "Bearer user-token"]


def test_jira_refresh_rotation_updates_sibling_site_accounts(monkeypatch):
    db = SessionLocal()
    try:
        svc = AccountService()
        first = svc.upsert_linked_account(
            db,
            AccountCreate(
                user_email="demo@example.com",
                source="jira",
                label="Engineering Jira",
                account_identifier="cloud-1",
                access_token="old-access",
                refresh_token="shared-refresh",
                metadata={"cloud_id": "cloud-1", "base_url": "https://api.atlassian.com/ex/jira/cloud-1"},
            ),
        )
        second = svc.upsert_linked_account(
            db,
            AccountCreate(
                user_email="demo@example.com",
                source="jira",
                label="Support Jira",
                account_identifier="cloud-2",
                access_token="old-access",
                refresh_token="shared-refresh",
                metadata={"cloud_id": "cloud-2", "base_url": "https://api.atlassian.com/ex/jira/cloud-2"},
            ),
        )
        db.flush()

        monkeypatch.setattr(
            "app.services.ingestion.OAuthService.refresh_token",
            lambda self, provider, refresh_token: {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_at": datetime(2026, 5, 16, tzinfo=timezone.utc),
            },
        )

        IngestionService()._refresh_if_needed(db, first, reason="test", force=True)
        db.refresh(first)
        db.refresh(second)

        assert first.access_token == "new-access"
        assert first.refresh_token == "new-refresh"
        assert second.access_token == "new-access"
        assert second.refresh_token == "new-refresh"
    finally:
        db.close()


def test_followup_uses_resolved_slack_user_id(monkeypatch):
    client = TestClient(app)
    client.post(
        "/api/v1/accounts",
        json={
            "user_email": "demo@example.com",
            "source": "slack",
            "label": "Slack",
            "account_identifier": "T123",
            "access_token": "slack-token",
            "metadata": {"team_id": "T123"},
        },
    )

    monkeypatch.setattr(
        "app.services.communication.SlackConnector.resolve_user",
        lambda self, account, reference: {
            "id": "U123",
            "display_name": "Bob",
            "email": "bob@example.com",
            "aliases": [reference, "bob", "U123"],
            "team_id": "T123",
        },
    )

    response = client.post(
        "/api/v1/communication/followups",
        json={
            "user_email": "demo@example.com",
            "person": "@bob",
            "task": "JIRA-123",
            "question": "What is the current ETA?",
        },
    )

    assert response.status_code == 200
    with SessionLocal() as db:
        proposal = db.query(ActionProposal).filter(ActionProposal.id == response.json()["proposal_id"]).one()
        assert proposal.payload_json["target_slack_user_id"] == "U123"


def test_slack_event_dedupes_by_event_id(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    get_settings.cache_clear()
    client = TestClient(app)
    client.post(
        "/api/v1/accounts",
        json={
            "user_email": "demo@example.com",
            "source": "slack",
            "label": "Slack",
            "account_identifier": "T123",
            "metadata": {"sample_mode": True},
        },
    )
    followup = client.post(
        "/api/v1/communication/followups",
        json={
            "user_email": "demo@example.com",
            "person": "<@U123>",
            "task": "JIRA-123",
            "question": "Any update?",
        },
    )
    assert followup.status_code == 200

    payload = {
        "type": "event_callback",
        "event_id": "Ev123",
        "team_id": "T123",
        "event": {
            "type": "message",
            "channel_type": "im",
            "user": "U123",
            "text": "The fix is ready.",
            "channel": "D123",
            "ts": "1713800000.000100",
        },
    }

    first = client.post("/api/v1/slack/events", json=payload)
    second = client.post("/api/v1/slack/events", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    with SessionLocal() as db:
        assert db.query(FollowUpMessage).filter(FollowUpMessage.direction == "inbound").count() == 1


def test_jira_issue_refresh_by_key_updates_task_memory(monkeypatch):
    monkeypatch.setenv("ENABLE_MOCK_CONNECTORS", "false")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        AccountService().upsert_linked_account(
            db,
            AccountCreate(
                user_email="demo@example.com",
                source="jira",
                label="Jira",
                account_identifier="cloud-1",
                access_token="jira-token",
                metadata={"base_url": "https://api.atlassian.example/ex/jira/cloud-1"},
            ),
        )
        user = db.query(User).filter(User.email == "demo@example.com").one()
        loop = CommunicationLoopService()
        org = loop.get_or_create_organization_for_user(db, user)
        db.commit()
    finally:
        db.close()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://api.atlassian.example/ex/jira/cloud-1/rest/api/3/issue/JIRA-123")
        assert request.headers["authorization"] == "Bearer jira-token"
        return httpx.Response(
            200,
            json={
                "id": "10001",
                "key": "JIRA-123",
                "fields": {
                    "summary": "Fix webhook retries",
                    "updated": "2026-05-14T10:00:00.000+0000",
                    "status": {"name": "In Progress"},
                    "assignee": {"displayName": "Bob"},
                    "description": {
                        "content": [
                            {"content": [{"type": "text", "text": "Retry handling is being hardened."}]}
                        ]
                    },
                    "comment": {"comments": []},
                },
            },
        )

    transport = httpx.MockTransport(handler)
    import app.connectors.base as base_module

    original_client = base_module.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(base_module.httpx, "Client", client_factory)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "demo@example.com").one()
        org = loop.get_or_create_organization_for_user(db, user)
        record = loop.refresh_jira_issue_context(db, user, org.id, "JIRA-123")
        db.commit()

        assert record is not None
        result = loop.answer_whereis(db, user, "Bob", "JIRA-123")
        assert result["task_id"]
        assert result["citations"]
    finally:
        db.close()
        get_settings.cache_clear()

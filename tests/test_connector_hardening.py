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

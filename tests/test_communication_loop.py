from fastapi.testclient import TestClient

from app.main import app
from app.models.communication import ActionProposal, Commitment
from app.models.account import User
from app.db import SessionLocal
from app.models.account import LinkedAccount
from app.services.commitments import CommitmentExtractionService
from app.services.communication import CommunicationLoopService


def _seed_pipeline(client: TestClient) -> None:
    for source in ("slack", "jira"):
        client.post(
            "/api/v1/accounts",
            json={
                "user_email": "demo@example.com",
                "source": source,
                "label": f"{source} sample",
                "account_identifier": f"{source}-1",
                "metadata": {"sample_mode": True},
            },
        )
    response = client.post(
        "/api/v1/pipeline/run",
        json={"user_email": "demo@example.com", "lookback_hours": 168, "delivery_channel": "db", "force_fetch": True},
    )
    assert response.status_code == 200


def test_whereis_returns_source_backed_task_memory():
    client = TestClient(app)
    _seed_pipeline(client)

    response = client.post(
        "/api/v1/communication/whereis",
        json={"user_email": "demo@example.com", "person": "bob", "task": "JIRA-123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "Status:" in body["answer"]
    assert body["confidence"] > 0
    assert body["citations"]

    retrieval = client.post(
        "/api/v1/communication/retrieve",
        json={"user_email": "demo@example.com", "person": "bob", "jira_key": "JIRA-123", "commitment_status": "open"},
    )
    assert retrieval.status_code == 200
    retrieved = retrieval.json()
    assert retrieved["tasks"]
    assert retrieved["citations"]


def test_followup_reply_updates_memory_and_jira_draft_is_approval_gated():
    client = TestClient(app)
    _seed_pipeline(client)

    followup = client.post(
        "/api/v1/communication/followups",
        json={
            "user_email": "demo@example.com",
            "person": "bob",
            "task": "JIRA-123",
            "question": "What is the current ETA?",
            "requester": "manager@example.com",
        },
    )
    assert followup.status_code == 200
    followup_body = followup.json()
    assert followup_body["approval_required"] is True
    assert followup_body["proposal_status"] == "pending_approval"

    reply = client.post(
        "/api/v1/communication/followups/reply",
        json={
            "slack_user_id": "bob",
            "text": "Merge logic is fixed. I can update JIRA-123 after QA signs off today.",
            "channel_id": "D123",
            "message_ts": "1713800000.000100",
        },
    )
    assert reply.status_code == 200

    draft = client.post(f"/api/v1/communication/followups/{followup_body['follow_up_id']}/draft-jira")
    assert draft.status_code == 200
    draft_body = draft.json()
    assert draft_body["status"] == "pending_approval"
    assert draft_body["payload"]["jira_key"] == "JIRA-123"

    memory = client.post(
        "/api/v1/communication/whereis",
        json={"user_email": "demo@example.com", "person": "bob", "task": "JIRA-123"},
    ).json()
    assert "QA signs off" in memory["answer"]

    blocked = client.post(f"/api/v1/actions/{draft_body['proposal_id']}/execute")
    assert blocked.status_code == 400


def test_low_confidence_commitments_are_suggestions_not_facts():
    client = TestClient(app)
    client.get("/api/v1/users/default")
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "demo@example.com").first()
        loop = CommunicationLoopService()
        org = loop.get_or_create_organization_for_user(db, user)
        result = loop.store_extracted_commitment(
            db,
            org.id,
            user.id,
            {
                "owner": "Akshit",
                "requester": "Manager",
                "task_title": "Maybe follow up",
                "commitment_text": "Might handle this later",
                "source_system": "slack",
                "source_url": "https://slack.example/msg",
                "source_message_id": "m1",
                "status": "suggestion",
                "confidence": 0.4,
            },
        )
        db.commit()

        assert isinstance(result, ActionProposal)
        assert db.query(Commitment).count() == 0


def test_commitment_extraction_validation_drops_malformed_items():
    service = CommitmentExtractionService()

    validated = service._validate_extraction(
        {
            "commitments": [
                {
                    "owner": "Akshit",
                    "requester": "Manager",
                    "task_title": "JIRA-123",
                    "commitment_text": "I will update the ticket today",
                    "source_system": "slack",
                    "status": "nonsense",
                    "confidence": 1.7,
                },
                {"owner": "No task title"},
            ]
        }
    )

    assert validated == {"commitments": []}


def test_stale_jira_detection_creates_approval_gated_draft():
    client = TestClient(app)
    _seed_pipeline(client)

    response = client.post("/api/v1/communication/stale-jira/demo@example.com")

    assert response.status_code == 200
    body = response.json()
    assert body["proposals"]
    assert body["proposals"][0]["status"] == "pending_approval"
    assert body["proposals"][0]["payload"]["operation"] == "add_comment"


def test_tenant_scoped_memory_does_not_cross_users():
    client = TestClient(app)
    _seed_pipeline(client)
    client.post(
        "/api/v1/accounts",
        json={
            "user_email": "other@different.example",
            "source": "slack",
            "label": "other slack",
            "account_identifier": "other-slack",
            "metadata": {"sample_mode": True},
        },
    )

    other = client.post(
        "/api/v1/communication/whereis",
        json={"user_email": "other@different.example", "person": "bob", "task": "JIRA-123"},
    )

    assert other.status_code == 200
    assert other.json()["status"] == "unknown"


def test_approved_jira_update_can_execute_after_human_approval(monkeypatch):
    client = TestClient(app)
    _seed_pipeline(client)
    followup = client.post(
        "/api/v1/communication/followups",
        json={
            "user_email": "demo@example.com",
            "person": "bob",
            "task": "JIRA-123",
            "question": "What should I post?",
            "requester": "manager@example.com",
        },
    ).json()
    client.post(
        "/api/v1/communication/followups/reply",
        json={
            "slack_user_id": "bob",
            "text": "QA passed. Please add a Jira comment.",
            "channel_id": "D123",
            "message_ts": "1713800001.000100",
        },
    )
    draft = client.post(f"/api/v1/communication/followups/{followup['follow_up_id']}/draft-jira").json()

    with SessionLocal() as db:
        account = (
            db.query(LinkedAccount)
            .join(User, User.id == LinkedAccount.user_id)
            .filter(User.email == "demo@example.com", LinkedAccount.source == "jira")
            .first()
        )
        account.access_token = "jira-token"
        account.metadata_json = {"base_url": "https://api.atlassian.example/ex/jira/cloud-1"}
        db.commit()

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"self": "https://api.atlassian.example/rest/api/3/issue/JIRA-123/comment/1"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("app.services.actions.httpx.Client", FakeClient)

    approved = client.post(f"/api/v1/actions/{draft['proposal_id']}/approve", json={"execute": True})

    assert approved.status_code == 200
    assert approved.json()["status"] == "executed"
    assert calls[0]["url"].endswith("/rest/api/3/issue/JIRA-123/comment")
    assert calls[0]["json"]["body"]["type"] == "doc"

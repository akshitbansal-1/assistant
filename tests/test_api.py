import re

from fastapi.testclient import TestClient

from app.main import app


def test_api_pipeline_run():
    client = TestClient(app)
    client.post(
        "/api/v1/accounts",
        json={
            "user_email": "demo@example.com",
            "source": "gmail",
            "label": "Gmail sample",
            "account_identifier": "gmail-1",
            "metadata": {"sample_mode": True},
        },
    )
    response = client.post(
        "/api/v1/pipeline/run",
        json={"user_email": "demo@example.com", "lookback_hours": 24, "delivery_channel": "db"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "summary_id" in body
    latest = client.get("/api/v1/summaries/demo@example.com/latest")
    assert latest.status_code == 200


def test_ui_pages_render():
    client = TestClient(app)
    # /ui redirects to the default user dashboard in local (no-auth) mode
    response = client.get("/ui")
    assert response.status_code == 200
    assert "Work Intelligence" in response.text

    detail = client.get("/ui/dashboard")
    assert detail.status_code == 200
    assert "Run Pipeline" in detail.text
    assert "Connector health" in detail.text
    assert "Coordination console" in detail.text
    assert "whereis-person" in detail.text
    assert "followup-question" in detail.text
    assert "retrieve-jira" in detail.text
    assert "memory-task" in detail.text


def test_admin_invite_and_onboarding_flow_renders_user_list():
    client = TestClient(app)
    dashboard = client.get("/ui/dashboard")
    assert dashboard.status_code == 200

    invite = client.post(
        "/ui/admin/invites",
        data={"email": "new.user@example.com", "name": "New User", "role": "member", "manager_email": "demo@example.com"},
        follow_redirects=False,
    )
    assert invite.status_code == 303

    admin = client.get("/ui/admin")
    assert admin.status_code == 200
    assert "new.user@example.com" in admin.text
    token = re.search(r"/onboard/([A-Za-z0-9_-]+)", admin.text).group(1)

    onboard = client.get(f"/onboard/{token}")
    assert onboard.status_code == 200
    assert "new.user@example.com" in onboard.text

    accepted = client.post(f"/onboard/{token}", data={"name": "New User"}, follow_redirects=False)
    assert accepted.status_code == 303

    users = client.get("/ui/admin")
    assert users.status_code == 200
    assert "New User" in users.text

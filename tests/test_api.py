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

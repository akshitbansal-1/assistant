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
    response = client.get("/ui")
    assert response.status_code == 200
    assert "Stored workspace intelligence" in response.text

    detail = client.get("/ui/users/demo@example.com")
    assert detail.status_code == 200
    assert "Add Google account" in detail.text

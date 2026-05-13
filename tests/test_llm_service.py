import json

import httpx

from app.config import get_settings
from app.llm import service as llm_service
from app.llm.service import LLMService


def test_openrouter_json_uses_chat_completion_api(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_SITE_URL", "https://example.com")
    monkeypatch.setenv("OPENROUTER_APP_NAME", "Work Intel Test")
    get_settings.cache_clear()

    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(
            {
                "url": str(request.url),
                "headers": request.headers,
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "```json\n{\"classification\":\"task\"}\n```"},
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    original_client = llm_service.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(llm_service.httpx, "Client", client_factory)

    try:
        result = LLMService()._complete_json("system", "{\"items\":[]}", max_tokens=123)
    finally:
        get_settings.cache_clear()

    assert result == {"classification": "task"}
    assert len(seen_requests) == 1
    request = seen_requests[0]
    assert request["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert request["headers"]["authorization"] == "Bearer openrouter-key"
    assert request["headers"]["http-referer"] == "https://example.com"
    assert request["headers"]["x-title"] == "Work Intel Test"
    assert request["body"] == {
        "model": "google/gemma-4-26b-a4b-it:free",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "{\"items\":[]}"},
        ],
        "temperature": 0.1,
        "max_tokens": 123,
        "response_format": {"type": "json_object"},
    }

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.llm.prompts import CLASSIFICATION_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT


logger = logging.getLogger(__name__)


class LLMService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def classify_item(self, item: dict[str, Any]) -> dict[str, Any]:
        prompt = json.dumps(item, default=str)
        try:
            return self._complete_json(
                system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
                user_prompt=prompt,
            )
        except Exception:
            logger.exception("LLM classification failed, falling back to heuristics")
            return self._heuristic_classification(item)

    def summarize(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = json.dumps(payload, default=str)
        try:
            return self._complete_json(
                system_prompt=SUMMARY_SYSTEM_PROMPT,
                user_prompt=prompt,
            )
        except Exception:
            logger.exception("LLM summary failed, returning heuristic summary payload")
            return payload

    def _complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        provider = self.settings.llm_provider.lower()
        if provider == "gemini":
            return self._gemini_json(system_prompt, user_prompt)
        if provider == "mock":
            return self._mock_json(system_prompt, user_prompt)
        raise ValueError(f"Unsupported LLM provider: {provider}")

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3), reraise=True)
    def _gemini_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        api_key = self.settings.llm_api_key or getattr(self.settings, "gemini_api_key", None)
        if not api_key:
            raise ValueError("GEMINI_API_KEY or LLM_API_KEY must be configured when llm_provider=gemini")
        model = self.settings.llm_model or "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "maxOutputTokens": 2048,
            },
        }
        with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
            response = client.post(
                url,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        text = (
            payload.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "{}")
        )
        return json.loads(text)

    def _mock_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        try:
            item = json.loads(user_prompt)
        except json.JSONDecodeError:
            return {}
        if "items" in item or "priority_actions" in item:
            return item
        return self._heuristic_classification(item)

    def _heuristic_classification(self, item: dict[str, Any]) -> dict[str, Any]:
        text = f"{item.get('title', '')} {item.get('content', '')}".lower()
        classification = "info"
        if any(token in text for token in ["blocked", "waiting on", "stuck", "dependency"]):
            classification = "blocker"
        elif any(token in text for token in ["todo", "please", "need to", "action", "follow up"]):
            classification = "task"
        elif any(token in text for token in ["decision", "approve", "approved"]):
            classification = "decision"
        elif "?" in text:
            classification = "follow_up"
        needs_action = classification in {"task", "follow_up", "blocker"}
        people = item.get("people", []) or []
        who_should_act = people[0] if people else "you" if needs_action else ""
        short_summary = (item.get("content") or item.get("title") or "")[:160]
        return {
            "classification": classification,
            "needs_action": needs_action,
            "who_should_act": who_should_act,
            "people": people,
            "short_summary": short_summary,
        }

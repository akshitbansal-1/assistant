from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.llm.prompts import CLASSIFICATION_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT


logger = logging.getLogger(__name__)

_AUTOMATED_SENDER_RE = re.compile(
    r"(no.?reply|noreply|do.not.reply|mailer.daemon|postmaster|bounce|"
    r"notifications?@|alert(s)?@|automated@|newsletter@|digest@)",
    re.IGNORECASE,
)

_AUTOMATED_TITLE_RE = re.compile(
    r"\b(confirmation code|verify your|verification code|one.time (password|code)|"
    r"\botp\b|sign.?in code|login code|access code|security code|"
    r"reset your password|password reset|reset password link|"
    r"unsubscribe|newsletter|your receipt|invoice #|order confirm(ed|ation)|"
    r"subscription confirm|welcome to|you.?ve been (added|invited)|"
    r"account (created|confirmed|activated)|email confirmed|"
    r"thank you for (signing up|registering|your (purchase|order)))\b",
    re.IGNORECASE,
)


class LLMService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def classify_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not items:
            return []

        results: list[dict[str, Any] | None] = [None] * len(items)
        llm_indices: list[int] = []

        for i, item in enumerate(items):
            pre = self._pre_classify(item)
            if pre is not None:
                results[i] = pre
            else:
                llm_indices.append(i)

        if llm_indices:
            llm_items = [items[i] for i in llm_indices]
            trimmed = [
                {**item, "content": (item.get("content") or "")[:500]}
                for item in llm_items
            ]
            prompt = json.dumps(trimmed, default=str)
            try:
                result = self._complete_json(
                    system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    max_tokens=max(4096, len(llm_items) * 300),
                )
                if isinstance(result, list):
                    classifications = result
                else:
                    for key in ("items", "classifications", "results"):
                        if key in result and isinstance(result[key], list):
                            classifications = result[key]
                            break
                    else:
                        raise ValueError("Unexpected batch response shape")
                while len(classifications) < len(llm_items):
                    classifications.append(self._heuristic_classification(llm_items[len(classifications)]))
                for idx, classification in zip(llm_indices, classifications):
                    results[idx] = classification
            except Exception:
                logger.exception("Batch LLM classification failed, falling back to heuristics")
                for i in llm_indices:
                    results[i] = self._heuristic_classification(items[i])

        return results  # type: ignore[return-value]

    def classify_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return self.classify_items([item])[0]

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

    def _complete_json(self, system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> dict[str, Any]:
        provider = self.settings.llm_provider.lower()
        if provider == "gemini":
            return self._gemini_json(system_prompt, user_prompt, max_tokens)
        if provider == "mock":
            return self._mock_json(system_prompt, user_prompt)
        raise ValueError(f"Unsupported LLM provider: {provider}")

    @retry(wait=wait_exponential(multiplier=2, min=5, max=60), stop=stop_after_attempt(4), reraise=True)
    def _gemini_json(self, system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> dict[str, Any]:
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
                "maxOutputTokens": max_tokens,
            },
        }
        print(body)
        with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
            response = client.post(
                url,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        candidate = payload.get("candidates", [{}])[0]
        finish_reason = candidate.get("finishReason", "")
        if finish_reason == "MAX_TOKENS":
            raise ValueError(f"Gemini response truncated (MAX_TOKENS); increase max_tokens beyond {max_tokens}")
        text = (
            candidate.get("content", {})
            .get("parts", [{}])[0]
            .get("text", "{}")
        )
        # Strip markdown code fences Gemini occasionally wraps around JSON
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1]
            stripped = stripped.rsplit("```", 1)[0]
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            logger.error("Gemini returned invalid JSON (finish_reason=%s): %s", finish_reason, stripped[:500])
            raise

    def _mock_json(self, _system_prompt: str, user_prompt: str) -> dict[str, Any]:
        try:
            item = json.loads(user_prompt)
        except json.JSONDecodeError:
            return {}
        if "items" in item or "priority_actions" in item:
            return item
        return self._heuristic_classification(item)

    def _pre_classify(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Return a confident classification without calling the LLM, or None if uncertain."""
        title = item.get("title") or ""
        people = item.get("people") or []
        headers = (item.get("metadata") or {}).get("headers") or {}
        reply_to = headers.get("reply-to", "")
        sender = headers.get("from", "") or (people[0] if people else "")

        is_automated = (
            _AUTOMATED_SENDER_RE.search(sender)
            or _AUTOMATED_SENDER_RE.search(reply_to)
            or "list-unsubscribe" in headers
            or _AUTOMATED_TITLE_RE.search(title)
        )
        if is_automated:
            return {
                "classification": "info",
                "needs_action": False,
                "who_should_act": "",
                "people": [],
                "short_summary": title[:160],
            }
        return None

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

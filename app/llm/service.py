from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.llm.prompts import (
    CLASSIFICATION_SYSTEM_PROMPT,
    COMMITMENT_EXTRACTION_SYSTEM_PROMPT,
    RETRIEVAL_INTENT_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    VERIFICATION_SYSTEM_PROMPT,
    build_correction_block,
)


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

    def classify_items(
        self,
        items: list[dict[str, Any]],
        corrections: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
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
            system_prompt = self._build_corrected_system_prompt(
                CLASSIFICATION_SYSTEM_PROMPT,
                corrections or [],
            )
            try:
                result = self._complete_json(
                    system_prompt=system_prompt,
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

    def classify_item(
        self,
        item: dict[str, Any],
        corrections: list[dict] | None = None,
    ) -> dict[str, Any]:
        return self.classify_items([item], corrections=corrections)[0]

    def summarize(
        self,
        payload: dict[str, Any],
        corrections: list[dict] | None = None,
    ) -> dict[str, Any]:
        prompt = json.dumps(payload, default=str)
        system_prompt = self._build_corrected_system_prompt(
            SUMMARY_SYSTEM_PROMPT,
            corrections or [],
        )
        try:
            return self._complete_json(
                system_prompt=system_prompt,
                user_prompt=prompt,
            )
        except Exception:
            logger.exception("LLM summary failed, returning heuristic summary payload")
            return payload

    def extract_commitments(
        self,
        items: list[dict[str, Any]],
        corrections: list[dict] | None = None,
    ) -> dict[str, Any]:
        if not items:
            return {"commitments": []}
        logger.info("Extracting commitments from %d item(s)", len(items))
        trimmed = [
            {
                "id": item.get("id") or item.get("external_id"),
                "source": item.get("source"),
                "title": item.get("title"),
                "content": (item.get("content") or "")[:1200],
                "people": item.get("people") or [],
                "timestamp": item.get("timestamp"),
                "metadata": item.get("metadata") or {},
            }
            for item in items
        ]
        system_prompt = self._build_corrected_system_prompt(
            COMMITMENT_EXTRACTION_SYSTEM_PROMPT,
            corrections or [],
        )
        try:
            result = self._complete_json(
                system_prompt=system_prompt,
                user_prompt=json.dumps({"items": trimmed}, default=str),
                max_tokens=max(2048, len(trimmed) * 500),
            )
            if isinstance(result, dict) and isinstance(result.get("commitments"), list):
                logger.info("Commitment extraction returned %d candidate(s)", len(result["commitments"]))
                return result
            logger.warning("Commitment extraction returned unexpected shape: %s", type(result).__name__)
            return {"commitments": []}
        except Exception:
            logger.exception("LLM commitment extraction failed, falling back to heuristics")
            return {"commitments": self._heuristic_commitments(trimmed)}

    def _complete_json(self, system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> dict[str, Any]:
        provider = self.settings.llm_provider.lower()
        if provider == "gemini":
            return self._gemini_json(system_prompt, user_prompt, max_tokens)
        if provider == "openrouter":
            return self._openrouter_json(system_prompt, user_prompt, max_tokens)
        if provider == "mock":
            return self._mock_json(system_prompt, user_prompt)
        raise ValueError(f"Unsupported LLM provider: {provider}")

    def extract_retrieval_intent(self, query: str) -> dict[str, Any]:
        """Parse a free-form user query into structured retrieval parameters.

        Returns a dict with optional keys: person, jira_key, task_query, project,
        commitment_status, intent.  Falls back to a minimal dict on error.
        """
        if not query or not query.strip():
            return {"intent": "general"}
        try:
            result = self._complete_json(
                system_prompt=RETRIEVAL_INTENT_SYSTEM_PROMPT,
                user_prompt=query.strip(),
                max_tokens=512,
            )
            if isinstance(result, dict):
                result.setdefault("intent", "general")
                logger.info(
                    "Retrieval intent parsed intent=%s person=%s jira_key=%s",
                    result.get("intent"),
                    result.get("person"),
                    result.get("jira_key"),
                )
                return result
        except Exception:
            logger.warning("Retrieval intent parsing failed, falling back to raw query")
        return {"intent": "general", "task_query": query.strip()}

    def verify_claim(
        self,
        claimed_status: str,
        external_data: dict[str, Any],
        source_context: str = "",
    ) -> dict[str, Any]:
        """Cross-check a human status claim against external source data (e.g. Jira issue).

        Returns a dict with: verified, confidence, discrepancy, suggested_status, reasoning.
        """
        prompt = json.dumps(
            {
                "claimed_status": claimed_status[:600],
                "external_data": external_data,
                "source_context": source_context[:300],
            },
            default=str,
        )
        try:
            result = self._complete_json(
                system_prompt=VERIFICATION_SYSTEM_PROMPT,
                user_prompt=prompt,
                max_tokens=512,
            )
            if isinstance(result, dict) and "verified" in result:
                logger.info(
                    "Claim verification completed verified=%s confidence=%.2f",
                    result.get("verified"),
                    float(result.get("confidence") or 0),
                )
                return result
        except Exception:
            logger.warning("Claim verification failed, returning unverified result")
        return {
            "verified": False,
            "confidence": 0.3,
            "discrepancy": None,
            "suggested_status": None,
            "reasoning": "Verification could not be completed.",
        }

    def _build_corrected_system_prompt(
        self,
        base_prompt: str,
        corrections: list[dict],
        max_corrections: int = 5,
    ) -> str:
        """Append a CRITICAL MISTAKES TO AVOID block to a system prompt.

        Only the most recent ``max_corrections`` negative corrections are injected
        to avoid exceeding context limits.
        """
        recent = corrections[-max_corrections:] if corrections else []
        correction_block = build_correction_block(recent)
        return base_prompt + correction_block

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
        logger.info("Calling Gemini JSON completion model=%s max_tokens=%s prompt_chars=%s", model, max_tokens, len(user_prompt))
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
        return self._parse_json_text("Gemini", text, finish_reason)

    @retry(wait=wait_exponential(multiplier=2, min=5, max=60), stop=stop_after_attempt(4), reraise=True)
    def _openrouter_json(self, system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> dict[str, Any]:
        api_key = self.settings.llm_api_key or self.settings.openrouter_api_key
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY or LLM_API_KEY must be configured when llm_provider=openrouter")
        model = self._openrouter_model()
        url = f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if self.settings.openrouter_site_url:
            headers["HTTP-Referer"] = self.settings.openrouter_site_url
        if self.settings.openrouter_app_name:
            headers["X-Title"] = self.settings.openrouter_app_name
        logger.info("Calling OpenRouter JSON completion model=%s max_tokens=%s prompt_chars=%s", model, max_tokens, len(user_prompt))
        with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
            response = client.post(url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        choice = payload.get("choices", [{}])[0]
        finish_reason = choice.get("finish_reason", "")
        if finish_reason == "length":
            raise ValueError(f"OpenRouter response truncated (length); increase max_tokens beyond {max_tokens}")
        content = choice.get("message", {}).get("content", "{}")
        return self._parse_json_text("OpenRouter", self._content_to_text(content), finish_reason)

    def _openrouter_model(self) -> str:
        if self.settings.llm_model and self.settings.llm_model != "gemini-2.5-flash":
            return self.settings.llm_model
        return "google/gemma-4-26b-a4b-it:free"

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            return "\n".join(parts)
        return "{}"

    def _parse_json_text(self, provider_name: str, text: str, finish_reason: str = "") -> dict[str, Any]:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1]
            stripped = stripped.rsplit("```", 1)[0]
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            logger.error("%s returned invalid JSON (finish_reason=%s): %s", provider_name, finish_reason, stripped[:500])
            raise

    def _mock_json(self, _system_prompt: str, user_prompt: str) -> dict[str, Any]:
        try:
            parsed = json.loads(user_prompt)
        except json.JSONDecodeError:
            return {}
        # Batch classification: payload is a list of items
        if isinstance(parsed, list):
            return {"classifications": [self._heuristic_classification(item) for item in parsed]}
        if isinstance(parsed, dict) and "items" in parsed and "commitment" in _system_prompt.lower():
            return {"commitments": self._heuristic_commitments(parsed.get("items") or [])}
        if "items" in parsed or "priority_actions" in parsed:
            return parsed
        return self._heuristic_classification(parsed)

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

    def _heuristic_commitments(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        commitments: list[dict[str, Any]] = []
        issue_re = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
        commitment_re = re.compile(
            r"\b(i'?ll|i will|we will|will|can you|please|todo|follow up|eta|blocked|stuck|waiting on)\b",
            re.IGNORECASE,
        )
        for item in items:
            text = f"{item.get('title', '')}\n{item.get('content', '')}".strip()
            if not commitment_re.search(text):
                continue
            people = item.get("people") or []
            issue = issue_re.search(text)
            status = "blocked" if re.search(r"\b(blocked|stuck|waiting on)\b", text, re.I) else "open"
            commitments.append(
                {
                    "owner": people[0] if people else None,
                    "requester": people[1] if len(people) > 1 else None,
                    "task_title": item.get("title") or (issue.group(1) if issue else "Follow-up task"),
                    "jira_key": issue.group(1) if issue else None,
                    "project": None,
                    "commitment_text": (item.get("content") or item.get("title") or "")[:500],
                    "due_date": None,
                    "status": status,
                    "source_system": item.get("source"),
                    "source_url": (item.get("metadata") or {}).get("source_url"),
                    "source_message_id": item.get("id"),
                    "needs_follow_up": bool(re.search(r"\b(follow up|eta|blocked|stuck|waiting on)\b", text, re.I)),
                    "jira_appears_stale": bool(issue and re.search(r"\b(stale|not updated|jira)\b", text, re.I)),
                    "confidence": 0.7,
                }
            )
        return commitments

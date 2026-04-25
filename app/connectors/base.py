from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.models.account import LinkedAccount
from app.services.oauth import TokenCipher
from app.utils.sample_data import load_sample


logger = logging.getLogger(__name__)


class BaseConnector(ABC):
    source: str

    def __init__(self) -> None:
        self.settings = get_settings()
        self.timeout = self.settings.request_timeout_seconds
        self.cipher = TokenCipher()

    def use_sample_data(self, account: LinkedAccount) -> bool:
        metadata = account.metadata_json or {}
        return bool(
            metadata.get("sample_mode")
            or self.settings.enable_mock_connectors
            or not account.access_token
        )

    def get_access_token(self, account: LinkedAccount) -> str | None:
        if not account.access_token:
            return None
        return self.cipher.decrypt(account.access_token)

    def get_refresh_token(self, account: LinkedAccount) -> str | None:
        if not account.refresh_token:
            return None
        return self.cipher.decrypt(account.refresh_token)

    def sample_items(self) -> list[dict[str, Any]]:
        return load_sample(self.source)

    @retry(wait=wait_exponential(multiplier=1, min=1, max=8), stop=stop_after_attempt(3), reraise=True)
    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(method, url, headers=headers, params=params, json=json, data=data)
            if response.is_error:
                logger.warning("API error %s %s: %s", response.status_code, url, response.text[:400])
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    @abstractmethod
    def fetch_recent_items(
        self,
        account: LinkedAccount,
        start_at: datetime,
        end_at: datetime,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

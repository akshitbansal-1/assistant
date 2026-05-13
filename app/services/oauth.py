from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from cryptography.fernet import Fernet

from app.config import get_settings


logger = logging.getLogger(__name__)


class TokenCipher:
    def __init__(self) -> None:
        settings = get_settings()
        self.key = settings.token_encryption_key
        self.cipher = Fernet(self._normalize_key(self.key)) if self.key else None

    def _normalize_key(self, raw_key: str) -> bytes:
        candidate = str(raw_key).encode("utf-8")
        if len(candidate) == 44:
            return candidate
        digest = hashlib.sha256(str(raw_key).encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    def encrypt(self, value: str | None) -> str | None:
        if not value:
            return value
        if not self.cipher:
            return value
        return self.cipher.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return value
        if not self.cipher:
            return value
        return self.cipher.decrypt(value.encode("utf-8")).decode("utf-8")


class OAuthService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def build_authorization_url(self, provider: str, state: str | None = None) -> tuple[str, str]:
        state = state or secrets.token_urlsafe(24)
        config = self._provider_config(provider)
        params = {**config.get("authorize_params", {})}
        params["client_id"] = config["client_id"]
        params["redirect_uri"] = config["redirect_uri"]
        params["response_type"] = "code"
        params["state"] = state
        if config["scopes"]:
            params["scope"] = " ".join(config["scopes"])
        return f"{config['authorize_url']}?{urlencode(params)}", state

    def exchange_code(self, provider: str, code: str) -> dict[str, Any]:
        import httpx

        config = self._provider_config(provider)
        data = {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "redirect_uri": config["redirect_uri"],
            "code": code,
            "grant_type": "authorization_code",
        }
        auth = None
        headers: dict[str, str] = {}
        if provider == "notion":
            token = base64.b64encode(f"{config['client_id']}:{config['client_secret']}".encode("utf-8")).decode("utf-8")
            headers["Authorization"] = f"Basic {token}"
            data = {"grant_type": "authorization_code", "code": code, "redirect_uri": config["redirect_uri"]}
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.post(config["token_url"], data=data, headers=headers, auth=auth)
            response.raise_for_status()
            payload = response.json()
        expires_in = payload.get("expires_in")
        if expires_in:
            payload["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()
        return payload

    def refresh_token(self, provider: str, refresh_token: str) -> dict[str, Any]:
        import httpx

        config = self._provider_config(provider)
        data = {
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            logger.info("Refreshing OAuth token provider=%s token_url=%s", provider, config["token_url"])
            response = client.post(config["token_url"], data=data)
            response.raise_for_status()
            payload = response.json()
        expires_in = payload.get("expires_in")
        if expires_in:
            payload["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()
        return payload

    def encode_state(self, payload: dict[str, Any]) -> str:
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = hmac.new(self.settings.secret_key.encode("utf-8"), data, hashlib.sha256).hexdigest()
        blob = {"payload": payload, "sig": signature}
        return base64.urlsafe_b64encode(json.dumps(blob, separators=(",", ":")).encode("utf-8")).decode("utf-8")

    def decode_state(self, state: str | None) -> dict[str, Any]:
        if not state:
            return {}
        raw = base64.urlsafe_b64decode(state.encode("utf-8")).decode("utf-8")
        blob = json.loads(raw)
        payload = blob["payload"]
        expected = hmac.new(
            self.settings.secret_key.encode("utf-8"),
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, blob["sig"]):
            raise ValueError("Invalid OAuth state")
        return payload

    def fetch_account_identity(self, provider: str, token_payload: dict[str, Any]) -> dict[str, str]:
        import httpx

        provider = provider.lower()
        if provider == "google_login":
            access_token = token_payload.get("access_token")
            with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                response = client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                profile = response.json()
            return {
                "email": profile.get("email", ""),
                "name": profile.get("name") or profile.get("email", ""),
            }
        if provider == "gmail":
            access_token = token_payload.get("access_token")
            with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                response = client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                profile = response.json()
            return {
                "account_identifier": profile.get("email", profile.get("id", "gmail")),
                "label": profile.get("email", "Google account"),
            }
        if provider == "slack":
            team = token_payload.get("team") or {}
            authed_user = token_payload.get("authed_user") or {}
            enterprise = token_payload.get("enterprise") or {}
            return {
                "account_identifier": team.get("id") or authed_user.get("id") or "slack",
                "label": team.get("name") or "Slack workspace",
                "extra_metadata": {
                    "team_id": team.get("id"),
                    "team_name": team.get("name"),
                    "enterprise_id": enterprise.get("id"),
                    "authed_user_id": authed_user.get("id"),
                    "bot_user_id": token_payload.get("bot_user_id"),
                    "user_id": authed_user.get("id"),
                },
            }
        if provider == "notion":
            workspace_name = token_payload.get("workspace_name") or "Notion workspace"
            workspace_id = token_payload.get("workspace_id") or token_payload.get("workspace_icon") or "notion"
            access_token = token_payload.get("access_token")
            database_ids: list[str] = []
            if access_token:
                with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                    response = client.post(
                        "https://api.notion.com/v1/search",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Notion-Version": "2022-06-28",
                        },
                        json={"filter": {"property": "object", "value": "database"}, "page_size": 100},
                    )
                    if response.is_success:
                        database_ids = [r["id"] for r in response.json().get("results", [])]
            return {
                "account_identifier": workspace_id,
                "label": workspace_name,
                "extra_metadata": {"database_ids": database_ids},
            }
        if provider == "jira":
            access_token = token_payload.get("access_token")
            with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
                response = client.get(
                    "https://api.atlassian.com/oauth/token/accessible-resources",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                )
                response.raise_for_status()
                resources = response.json()
            if not resources:
                raise ValueError("No accessible Jira resources found for this account")
            resource = resources[0]
            cloud_id = resource["id"]
            site_url = resource.get("url")
            return {
                "account_identifier": cloud_id,
                "label": resource.get("name", "Jira workspace"),
                "extra_metadata": {
                    "cloud_id": cloud_id,
                    "site_url": site_url,
                    "base_url": f"https://api.atlassian.com/ex/jira/{cloud_id}",
                },
            }
        raise ValueError(f"Unsupported provider: {provider}")

    def _provider_config(self, provider: str) -> dict[str, Any]:
        provider = provider.lower()
        mapping = {
            "gmail": {
                "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_url": "https://oauth2.googleapis.com/token",
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": self.settings.google_redirect_uri,
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/userinfo.email",
                ],
                "authorize_params": {"access_type": "offline", "prompt": "consent"},
            },
            "slack": {
                "authorize_url": "https://slack.com/oauth/v2/authorize",
                "token_url": "https://slack.com/api/oauth.v2.access",
                "client_id": self.settings.slack_client_id,
                "client_secret": self.settings.slack_client_secret,
                "redirect_uri": self.settings.slack_redirect_uri,
                "scopes": [
                    "chat:write",
                    "commands",
                    "im:read",
                    "im:write",
                    "users:read",
                    "users:read.email",
                ],
                "authorize_params": {
                    "user_scope": "channels:history,groups:history,im:history,mpim:history,channels:read,groups:read,users:read,users:read.email",
                },
            },
            "notion": {
                "authorize_url": "https://api.notion.com/v1/oauth/authorize",
                "token_url": "https://api.notion.com/v1/oauth/token",
                "client_id": self.settings.notion_client_id,
                "client_secret": self.settings.notion_client_secret,
                "redirect_uri": self.settings.notion_redirect_uri,
                "scopes": [],
                "authorize_params": {"owner": "user"},
            },
            "jira": {
                "authorize_url": "https://auth.atlassian.com/authorize",
                "token_url": "https://auth.atlassian.com/oauth/token",
                "client_id": self.settings.jira_client_id,
                "client_secret": self.settings.jira_client_secret,
                "redirect_uri": self.settings.jira_redirect_uri,
                "scopes": ["read:jira-work", "read:jira-user", "write:jira-work", "offline_access"],
                "authorize_params": {"audience": "api.atlassian.com", "prompt": "consent"},
            },
            "google_login": {
                "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_url": "https://oauth2.googleapis.com/token",
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": self.settings.auth_google_redirect_uri,
                "scopes": [
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile",
                ],
                "authorize_params": {"access_type": "online"},
            },
        }
        config = mapping.get(provider)
        if not config:
            raise ValueError(f"Unsupported provider: {provider}")
        if not config["client_id"] or not config["redirect_uri"]:
            raise ValueError(f"OAuth is not fully configured for provider: {provider}")
        return config

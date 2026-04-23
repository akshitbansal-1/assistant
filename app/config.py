from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_url: str = "sqlite:///./work_intel.db"
    redis_url: str = "redis://localhost:6379/0"

    secret_key: str = "change-me"
    token_encryption_key: str | None = None
    enable_mock_connectors: bool = True
    default_user_email: str = "demo@example.com"

    daily_summary_hour: int = 8
    daily_summary_minute: int = 0

    llm_provider: str = "mock"
    llm_model: str = "gemini-2.5-flash"
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    gemini_api_key: str | None = None

    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None

    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    slack_redirect_uri: str | None = None

    notion_client_id: str | None = None
    notion_client_secret: str | None = None
    notion_redirect_uri: str | None = None

    jira_client_id: str | None = None
    jira_client_secret: str | None = None
    jira_redirect_uri: str | None = None

    request_timeout_seconds: int = Field(default=20, ge=1, le=120)
    llm_timeout_seconds: int = Field(default=30, ge=1, le=120)
    llm_max_retries: int = Field(default=3, ge=1, le=10)


@lru_cache
def get_settings() -> Settings:
    return Settings()

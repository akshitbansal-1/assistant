# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Setup

```bash
# Start PostgreSQL + Redis
docker compose up -d

# Create virtualenv and install deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Copy and fill environment variables
cp .env.example .env
```

## Common Commands

| Task | Command |
|------|---------|
| Run API server | `.venv/bin/uvicorn app.main:app --reload` |
| Run Celery worker | `.venv/bin/celery -A app.workers.celery_app.celery_app worker --loglevel=info` |
| Run Celery beat scheduler | `.venv/bin/celery -A app.workers.celery_app.celery_app beat --loglevel=info` |
| Run all tests | `.venv/bin/python -m pytest -q` |
| Run a single test file | `.venv/bin/python -m pytest tests/test_pipeline.py -q` |

## Architecture

The app is a **multi-stage data pipeline** orchestrated by `DailyWorkPipeline` (`app/services/pipeline.py`):

1. **Ingestion** (`app/services/ingestion.py`) — fetches raw events via provider connectors
2. **Normalization** (`app/services/normalization.py`) — maps provider payloads to the unified `WorkItem` schema
3. **Classification** (`app/services/intelligence.py`) — LLM labels actionability, ownership, and category
4. **Deduplication** (`app/services/deduplication.py`) — merges threads and issue-linked items across sources
5. **Memory** (`app/services/memory.py`) — persists known people and already-tracked tasks across runs
6. **Summarization** (`app/services/summary.py`) — builds structured daily output
7. **Notification** (`app/services/notification.py`) — delivers result to DB or Slack

### Key directories

```
app/
  api/          # FastAPI route handlers
  connectors/   # Provider fetchers (Gmail, Slack, Notion, Jira) — all extend BaseConnector
  llm/          # LLM service (Gemini JSON mode + MockLLMService for tests)
  models/       # SQLAlchemy ORM models
  schemas/      # Pydantic request/response schemas
  services/     # Pipeline business logic (one file per stage above)
  workers/      # Celery tasks for scheduled runs
  config.py     # pydantic-settings config (reads from .env)
  db.py         # SQLAlchemy engine + session factory
  main.py       # App entry point with lifespan hooks
tests/
  conftest.py   # SQLite test DB, mock connectors, mock LLM fixtures
```

### Connector pattern

Every data source lives in `app/connectors/` and inherits from `BaseConnector`. Connectors:
- Fall back to JSON files in `sample_data/` when `USE_MOCK_CONNECTORS=true` (set automatically in tests)
- Support OAuth token refresh via a `refresh_token` hook
- Use tenacity for retries

### LLM layer

`app/llm/` exposes a swappable `LLMService` interface. `GeminiLLMService` is used in production; `MockLLMService` is injected by `conftest.py`. All classification and summarization prompts are defined as system prompts inside the service methods.

### Testing

Tests use SQLite in-memory (via `conftest.py`) and mock connectors/LLM — no external API calls required. `test_pipeline.py` verifies end-to-end idempotency: running the pipeline twice must not duplicate `WorkItem` rows.

## Configuration

All settings live in `app/config.py` (pydantic-settings). Key env vars:

- `DATABASE_URL` — defaults to SQLite locally, set PostgreSQL for production
- `REDIS_URL` — required for Celery
- `GEMINI_API_KEY` — required in production; omit to use mock LLM
- `USE_MOCK_CONNECTORS` — set `true` to load sample data instead of hitting provider APIs
- `ENCRYPTION_KEY` — optional; used to encrypt stored OAuth tokens

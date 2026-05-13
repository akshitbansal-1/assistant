# AGENTS.md

Guidance for tests.

## Test Environment

- Use `.venv/bin/python -m pytest -q`.
- Tests run with SQLite and mock connector/LLM settings from `tests/conftest.py`.
- Do not require live Gmail, Slack, Notion, Jira, Gemini, Redis, or PostgreSQL for unit tests.
- Keep fixture timestamps inside the lookback windows used by tests.

## What To Cover

- Ingestion idempotency and deduplication.
- OAuth refresh behavior, including missing expiry and 401 retry.
- Commitment extraction parsing and low-confidence handling.
- Source citation preservation.
- Follow-up creation and reply capture.
- Jira stale detection and draft proposal creation.
- Approval-gated action execution.
- Organization or tenant isolation when touching shared data paths.

## Test Style

- Prefer focused service tests for business logic.
- Use API tests for request/response contracts and route-level error handling.
- Avoid sleeping, live network calls, and brittle wall-clock assumptions.
- Clean up runtime artifacts when a test creates local files.

# Daily Work Intelligence Agent

Production-minded MVP for aggregating Gmail, Slack, Notion, and Jira activity into a daily actionable summary.

## What It Does

- Supports multiple linked Gmail and Slack accounts per user, plus one or more Notion and Jira integrations
- Ingests the last 24 hours of activity
- Normalizes all sources into a unified item schema
- Uses an LLM layer for classification and summarization
- Deduplicates threads and Jira-linked references across sources
- Persists memory for known people and already-tracked tasks
- Generates a daily summary in JSON plus human-readable text
- Can store summaries in the database and optionally deliver to Slack

## Stack

- Backend: FastAPI
- Database: PostgreSQL in production, SQLite-friendly for local tests
- Queue/Scheduler: Redis + Celery + Celery Beat
- LLM: Gemini or OpenRouter via API key, with a mock fallback for local testing

## Repo Layout

```text
app/
  api/
  connectors/
  llm/
  models/
  schemas/
  services/
  utils/
  workers/
sample_data/
tests/
```

## Architecture

Detailed design docs:

- [High-level design](docs/architecture-hld.md)
- [Low-level design](docs/architecture-lld.md)

### Flow

1. `ingest_data()` fetches raw source data per linked account.
2. `normalize_data()` maps provider payloads into a unified schema.
3. `classify_items()` uses the LLM service to label actionability and ownership.
4. `deduplicate_items()` merges threads and issue-linked work.
5. `generate_summary()` builds the final daily output and updates memory.

### Unified Item Schema

Each normalized work item includes:

```json
{
  "id": "internal-db-id",
  "source": "gmail|slack|notion|jira",
  "account_id": "linked-account-id",
  "external_id": "provider-item-id",
  "timestamp": "2026-04-22T09:10:00+00:00",
  "title": "Item title",
  "content": "Body or summary text",
  "people": ["Alice", "Bob"],
  "thread_id": "provider-thread-or-issue-id",
  "metadata": {}
}
```

## Local Setup

### 1. Start infrastructure

```bash
docker compose up -d
```

This starts PostgreSQL and Redis locally.

### 2. Create the Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Minimum useful settings:

- `DATABASE_URL`
- `REDIS_URL`
- `LLM_PROVIDER=gemini`
- `LLM_MODEL=gemini-2.5-flash`
- `GEMINI_API_KEY=...`

To run the intelligence layer through OpenRouter instead:

- `LLM_PROVIDER=openrouter`
- `LLM_MODEL=google/gemma-4-26b-a4b-it:free`
- `OPENROUTER_API_KEY=...`

If you want to run without real provider tokens first, leave:

- `ENABLE_MOCK_CONNECTORS=true`

That uses the JSON files in [`sample_data/`](./sample_data).

## Run Locally

### API server

```bash
.venv/bin/uvicorn app.main:app --reload
```

Then open [http://localhost:8000/ui](http://localhost:8000/ui).

### Celery worker

```bash
.venv/bin/celery -A app.workers.celery_app.celery_app worker --loglevel=info
```

### Celery beat scheduler

```bash
.venv/bin/celery -A app.workers.celery_app.celery_app beat --loglevel=info
```

## Main API Endpoints

- `GET /api/v1/health`
- `POST /api/v1/accounts`
- `GET /api/v1/accounts/{user_email}`
- `GET /api/v1/oauth/{provider}/start`
- `POST /api/v1/oauth/{provider}/callback`
- `POST /api/v1/pipeline/run`
- `GET /api/v1/summaries/{user_email}`
- `GET /api/v1/summaries/{user_email}/latest`

## Built-in UI

- `GET /ui` shows all known users and high-level stored counts
- `GET /ui/users/{user_email}` shows linked accounts, summaries, tracked tasks, entities, and stored work items
- The user page includes SSO buttons for Google, Slack, and Notion
- After the provider redirects back, the app stores the linked account automatically and returns you to the user page

To make the SSO buttons work, set the provider client IDs, secrets, and redirect URIs in `.env` to point back to:

- `http://localhost:8000/api/v1/oauth/gmail/callback`
- `http://localhost:8000/api/v1/oauth/slack/callback`
- `http://localhost:8000/api/v1/oauth/notion/callback`

## Example: Link Sample Accounts

```bash
curl -X POST http://localhost:8000/api/v1/accounts \
  -H "Content-Type: application/json" \
  -d '{
    "user_email": "demo@example.com",
    "source": "gmail",
    "label": "Gmail sample",
    "account_identifier": "gmail-1",
    "metadata": {"sample_mode": true}
  }'
```

Repeat for `slack`, `notion`, and `jira`.

## Example: Run the Daily Pipeline

```bash
curl -X POST http://localhost:8000/api/v1/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{
    "user_email": "demo@example.com",
    "lookback_hours": 24,
    "delivery_channel": "db"
  }'
```

## Real Integrations

### Gmail

- Uses Gmail API search with `after:` and `before:` epoch-based queries
- Supports multiple Google accounts per user

### Slack

- Supports multiple workspaces per user
- Prioritizes DMs, mentions, and thread activity
- Optional Slack summary delivery via `chat.postMessage`

### Notion

- Queries configured task databases
- Filters recently edited or incomplete work

### Jira

- Pulls assigned and updated issues plus recent comment context

## OAuth / Account Linking

The MVP includes:

- linked account persistence
- encrypted token storage when `TOKEN_ENCRYPTION_KEY` is provided
- refresh-token support hooks
- provider-specific authorization URL generation

For immediate local use, manual token linking via `POST /api/v1/accounts` is the fastest path.

## Testing

```bash
.venv/bin/python -m pytest -q
```

The test suite runs against SQLite with mock connectors and mock LLM mode.

## Notes On Gemini

Gemini is the default LLM provider in `.env.example`. The implementation uses the `generateContent` REST endpoint with an API key and JSON-mode response config. Official references:

- [Gemini API docs](https://ai.google.dev/docs)
- [Gemini REST API reference](https://ai.google.dev/api)

## Next MVP Extensions

- replace simple Jira/Slack cross-linking with entity graph joins
- add vector memory for people/projects/tasks
- add a frontend dashboard for summary review and account linking
- add better delivery support for email and richer Slack blocks

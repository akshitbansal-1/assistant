# Communication Loop Tracker Runbook

## Fast Local Run

Use this path for development and demo work. It uses SQLite, mock connectors, and the built-in UI.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 3000 --reload
```

Open:

- UI: http://localhost:3000/ui
- API docs: http://localhost:3000/docs
- Default dashboard: http://localhost:3000/ui/dashboard

## Seed Sample Accounts

Run these while the API server is up:

```bash
for source in gmail slack notion jira; do
  curl -X POST http://localhost:3000/api/v1/accounts \
    -H "Content-Type: application/json" \
    -d "{
      \"user_email\": \"demo@example.com\",
      \"source\": \"$source\",
      \"label\": \"$source sample\",
      \"account_identifier\": \"$source-1\",
      \"metadata\": {\"sample_mode\": true}
    }"
done
```

Then ingest and build memory:

```bash
curl -X POST http://localhost:3000/api/v1/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{
    "user_email": "demo@example.com",
    "lookback_hours": 168,
    "delivery_channel": "db",
    "force_fetch": true
  }'
```

## Navigate The Product

- Linked accounts: connect or inspect Gmail, Slack, Notion, and Jira accounts.
- Run Pipeline: ingest recent activity, classify items, extract commitments, update task memory.
- Task memory: current status, blocker, ETA, confidence, and source-backed citations.
- Pending follow-ups: manager-requested nudges waiting for response.
- Action proposals: approval queue for Slack DMs and Jira updates.
- Jira hygiene: creates approval-gated Jira update drafts for stale or conflicting tickets.
- Audit log: records proposal creation, approval, execution, and failure.

## Logs

Application logs are written to stdout and to:

```bash
logs/app.log
```

Useful tails:

```bash
tail -f logs/app.log
tail -f logs/celery-worker.log
tail -f logs/celery-beat.log
```

The app logs coordination-loop events such as pipeline runs, connector fetch counts, commitment extraction, retrieval results, `/whereis`, follow-up creation, reply capture, action approval/execution, and Jira hygiene scans. It logs IDs/counts/statuses instead of OAuth tokens or full message bodies.

## Useful API Calls

Ask where someone is on a task:

```bash
curl -X POST http://localhost:3000/api/v1/communication/whereis \
  -H "Content-Type: application/json" \
  -d '{
    "user_email": "demo@example.com",
    "person": "bob",
    "task": "JIRA-123"
  }'
```

Create a follow-up proposal:

```bash
curl -X POST http://localhost:3000/api/v1/communication/followups \
  -H "Content-Type: application/json" \
  -d '{
    "user_email": "demo@example.com",
    "person": "bob",
    "task": "JIRA-123",
    "question": "What is the current ETA?",
    "requester": "manager@example.com"
  }'
```

Detect stale Jira drafts:

```bash
curl -X POST http://localhost:3000/api/v1/communication/stale-jira/demo@example.com
```

Approve a proposal:

```bash
curl -X POST http://localhost:3000/api/v1/actions/PROPOSAL_ID/approve \
  -H "Content-Type: application/json" \
  -d '{"execute": false}'
```

Approve and execute a proposal:

```bash
curl -X POST http://localhost:3000/api/v1/actions/PROPOSAL_ID/approve \
  -H "Content-Type: application/json" \
  -d '{"execute": true}'
```

## Full Stack Run

Use this when you want Redis-backed workers and scheduled jobs:

```bash
./start.sh
```

The script starts Redis with Docker Compose, runs Celery worker/beat, and serves the API at http://localhost:3000.

## Tests

```bash
.venv/bin/python -m pytest -q
```

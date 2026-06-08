# Phase 11: Production Readiness

## Goal

Prepare the MVP for a small B2B pilot.

## Tasks

- [ ] Add Alembic migrations for all communication-loop tables.
- [ ] Add tenant isolation tests across API endpoints and services.
- [ ] Add token encryption enforcement for production mode.
- [ ] Add rate limiting for Slack/Jira webhook endpoints.
- [ ] Add background incremental sync jobs per linked account.
- [x] Add scheduled proactive stale-alert agent job.
- [ ] Add retry/backoff and dead-letter handling for connector failures.
- [x] Add basic agent-run observability through MemoryEvent records.
- [ ] Add broader observability: structured request IDs, worker job IDs, and dead-letter dashboards.
- [ ] Add deployment notes for SQLite dev and Postgres production.

## Acceptance Criteria

- A fresh production database can be migrated deterministically.
- Tenant data cannot leak across users/orgs.
- Webhook endpoints reject invalid signatures.
- Proactive agents record start and finish events before proposing actions.
- Failed connector jobs are visible and retryable.

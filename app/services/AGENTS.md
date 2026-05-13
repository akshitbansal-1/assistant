# AGENTS.md

Guidance for service-layer work.

## Service Responsibilities

- `pipeline.py`: orchestrates the original daily summary pipeline.
- `ingestion.py`: account token refresh, connector dispatch, and raw item fetch.
- `normalization.py`: provider payload to shared work-item format.
- `intelligence.py`: classification and extraction coordination.
- `deduplication.py`: thread and ticket-aware duplicate control.
- `memory.py`: original known-people and tracked-task memory.
- `communication.py`: communication-loop workflows.
- `retrieval.py`: task/person/ticket/project/source retrieval.
- `commitments.py`: commitment extraction persistence.
- `actions.py`: proposal, approval, and execution boundary.
- `jira_hygiene.py`: stale Jira detection and draft proposals.

## Trust Boundaries

- `ActionProposalService` is the gate for all external writes.
- Slack DMs, Jira updates, and future GitHub writes must be proposals first unless the user explicitly changes the MVP rule.
- Do not let LLM output directly mutate external systems.
- Audit meaningful proposal, approval, follow-up, and memory events.

## Retrieval Rules

- Query structured fields first: person, Jira key, task title, project, Slack thread, date range, and commitment status.
- Use fuzzy or embedding fallback only after structured lookup is insufficient.
- Keep retrieved context small and citation-rich.
- Preserve source citations in answers, snapshots, proposals, and tests.

## Extraction Rules

- Store confident commitments as facts.
- Store low-confidence commitments as suggestions or draft memory, not canonical truth.
- Track owner, requester, task/ticket/project relation, due date, status, confidence, source system, source URL, and source message ID.
- Keep timestamps timezone-aware where possible.

## Testing Expectations

Add or update tests when modifying:

- OAuth refresh and ingestion failure handling.
- Commitment extraction parsing.
- Citation preservation.
- Follow-up creation or DM reply capture.
- Jira stale detection.
- Approval-gated action execution.
- Tenant or organization isolation.

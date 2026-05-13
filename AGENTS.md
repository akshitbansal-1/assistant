# AGENTS.md

Guidance for AI coding agents working in this repository.

## Collaboration Style

- Challenge assumptions before coding when the request depends on an unverified hypothesis.
- Ask a concise clarifying question first only when local context cannot answer the ambiguity and a wrong assumption would create real risk.
- Validate time-sensitive or external-platform behavior with online primary sources when it matters.
- Keep answers concise and factual unless asked for deeper detail.

## Product Direction

This project started as a Daily Work Intelligence Agent and is being converted into a B2B Communication Loop Tracker.

Core promise:

- Remember commitments, follow-ups, ticket updates, stale work, blockers, and owners across Slack, Jira, Gmail, and Notion.
- Help managers ask coordination questions like "where is Akshit on task X?" without turning the product into employee surveillance.
- Treat the product as coordination memory, not monitoring.

MVP priorities:

- Slack-first and Jira-aware.
- Source-backed task status answers with citations.
- Follow-up creation and DM response capture.
- Jira/Slack update drafts only after explicit approval.
- Cheap incremental sync, deduplication, cached summaries, and small-context retrieval.

Hard product constraints:

- All connectors must remain independent adapters.
- All external writes must go through the common action proposal and approval layer.
- Never autonomously post Slack/Jira updates in the MVP.
- Every AI claim about task status must carry source citations or be labeled low-confidence.
- Use Gemini by default for classification, extraction, and summarization unless provider abstraction already cleanly supports another model.
- Keep the system modular enough to add GitHub later as another connector.

## Architecture Map

- `app/main.py`: FastAPI app setup and lifespan.
- `app/api/routes.py`: API endpoints, Slack command/event ingress, OAuth callbacks.
- `app/ui/routes.py`: server-rendered UI routes.
- `app/connectors/`: source-specific read adapters for Gmail, Slack, Notion, and Jira.
- `app/services/pipeline.py`: original daily pipeline orchestration.
- `app/services/ingestion.py`: account fetch path, OAuth token refresh, connector dispatch.
- `app/services/communication.py`: communication-loop orchestration.
- `app/services/retrieval.py`: structured retrieval before fuzzy fallback.
- `app/services/actions.py`: action proposal and approval trust boundary.
- `app/services/jira_hygiene.py`: stale Jira detection and draft proposal creation.
- `app/llm/service.py` and `app/llm/prompts.py`: Gemini/mock LLM calls and prompts.
- `app/models/communication.py`: organization, person, task, commitment, follow-up, memory, proposal, and audit models.
- `templates/` and `static/`: built-in Jinja UI.
- `docs/plans/`: remaining phased product work.

## Local Commands

Use the virtualenv first.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall app tests
.venv/bin/uvicorn app.main:app --reload
```

Useful runtime checks:

```bash
tail -f logs/app.log
curl http://localhost:8000/api/v1/health
```

## Change Discipline

- Prefer small, working increments over broad rewrites.
- Preserve existing user or generated changes in the worktree.
- Do not revert unrelated dirty files.
- Use structured APIs and parsers instead of string hacks when reasonable.
- Keep external credentials and OAuth tokens out of logs, tests, docs, and final answers.
- Add tests when changing ingestion, OAuth, action approval, Slack command handling, retrieval, LLM parsing, or tenant isolation.
- If changing user-visible UI, verify the page manually or with a local browser when feasible.

## Data And Cost Rules

- Use structured database retrieval first.
- Use embeddings only as fuzzy fallback.
- Do not send large raw histories to the LLM.
- Preserve source URLs, message IDs, Jira keys, Slack thread IDs, and timestamps through every extraction and summary step.
- Store low-confidence extraction results as suggestions, not facts.

## Runtime Notes

- Tests use SQLite, mock connectors, and mock LLM mode via `tests/conftest.py`.
- Sample data lives in `sample_data/`.
- Runtime files such as logs, SQLite scratch DBs, and Celery beat schedules should not become product artifacts.

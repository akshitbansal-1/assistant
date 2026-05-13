# AGENTS.md

Guidance for changes under `app/`.

## Backend Rules

- Keep FastAPI route handlers thin. Put business behavior in `app/services/`.
- Keep SQLAlchemy models in `app/models/` and Pydantic request/response contracts in `app/schemas/`.
- Register new ORM models through `app/models/__init__.py` so `Base.metadata.create_all()` sees them.
- Prefer explicit service methods over cross-module side effects.
- Use `logging.getLogger(__name__)`; never use `print()` for runtime diagnostics.
- Do not log access tokens, refresh tokens, OAuth authorization codes, request signatures, or provider secrets.

## Error Handling

- Convert expected user-fixable failures into clear API errors.
- OAuth/account failures should tell the caller to reconnect the linked account rather than returning a generic 500.
- Preserve original exceptions with `from exc` when raising a domain-specific error.

## UI Rules

- The built-in UI is Jinja plus `static/ui.css`; do not introduce a frontend framework without explicit approval.
- Add controls for real workflows, not marketing copy.
- Keep pages focused on coordination: task memory, follow-ups, stale Jira, action proposals, citations, and audit trail.

## LLM Rules

- Use the existing LLM abstraction.
- Gemini is the default production provider; mock LLM is for tests.
- Validate structured JSON outputs before storing them.
- Any task-status answer must include citations or clearly lower its confidence.

## Verification

Run at least:

```bash
.venv/bin/python -m pytest -q
```

Run compile checks after touching imports, models, routes, or service wiring:

```bash
.venv/bin/python -m compileall app tests
```

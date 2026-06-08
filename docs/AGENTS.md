# AGENTS.md

Guidance for documentation work.

## Documentation Scope

- Keep docs operational and implementation-facing.
- Prefer runbooks, phase plans, endpoint notes, and connector setup details over broad product essays.
- Update docs when commands, routes, env vars, or setup steps change.

## Product Language

- Frame the product as coordination memory.
- Avoid surveillance language.
- Make approval-gated external writes explicit.
- Make source citations part of correctness, not a bonus feature.

## Plans

- Store remaining phased work in `docs/plans/`.
- Keep each plan concrete: goal, current status, tasks, acceptance checks, and verification.
- When work is completed, mark the plan rather than deleting useful implementation history.

## Commands In Docs

- Use `.venv/bin/python` and `.venv/bin/uvicorn` examples.
- Mention mock connector mode for local demos.
- Avoid documenting real secrets, tokens, or personal account identifiers.

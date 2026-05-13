# AGENTS.md

Guidance for connector work.

## Connector Boundary

- Each provider must stay an independent adapter.
- Connectors should fetch and normalize provider payload shape only enough for ingestion; product decisions belong in services.
- All connectors should inherit or follow `BaseConnector` behavior.
- Reads are allowed when credentials are valid. External writes are not allowed directly from connectors in the MVP.

## OAuth And Auth

- Use stored linked-account tokens through the account/ingestion path.
- Refresh access tokens before fetch when expiry is near or missing.
- Retry once after provider 401 only through the shared refresh flow.
- If refresh is unavailable, revoked, or unreadable, surface a reconnect-needed error.
- Never log raw tokens, authorization headers, cookies, or provider secrets.

## Cost And Scale

- Prefer incremental provider queries by timestamp, cursor, thread, ticket key, or page token.
- Cap fetched results with settings or method parameters.
- Preserve source identifiers and URLs so later AI answers can cite them.
- Keep sample-data fallback working for local tests and demos.

## Adding A New Connector

- Add the connector class under `app/connectors/`.
- Wire it through `app/services/ingestion.py`.
- Add source constants/schema support if needed.
- Add sample data and tests before relying on live credentials.

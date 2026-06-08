# Phase 8: Real Connector Hardening

## Goal

Make Slack and Jira reliable enough for a real pilot workspace, while preserving Gmail and Notion as secondary context sources.

## Tasks

- [x] Verify Slack OAuth scopes for commands, events, DM open, DM post, and user lookup.
- [x] Add Slack user profile lookup so `@person` resolves to stable Slack user IDs and display names.
- [x] Store Slack team/workspace IDs on linked accounts.
- [x] Add Slack event deduplication by event ID and message timestamp.
- [x] Verify Jira OAuth scopes for issue read and comment write.
- [x] Persist Jira cloud ID, site URL, and base API URL during OAuth linking.
- [x] Add Jira issue refresh by key for `/whereis` and stale checks.
- [x] Add connector health status in the UI.
- [x] Add integration tests with mocked Slack/Jira HTTP responses.

## Acceptance Criteria

- `/whereis @person JIRA-123` works with stable Slack user identity.
- `/followup @person JIRA-123 "question"` creates a proposal targeting the correct Slack user ID.
- Approved Jira comment proposals post only after approval.
- Connector failures surface as clear UI/API errors without losing task memory.

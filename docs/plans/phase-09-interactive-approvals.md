# Phase 9: Interactive Approvals

## Goal

Make approval workflows usable from Slack and the dashboard without weakening the approval gate.

## Tasks

- [x] Add Slack Block Kit approval messages for action proposals.
- [x] Add `/approve proposal_id` or interactive button callback handling.
- [x] Verify Slack request signature for interactive payloads.
- [x] Add proposal detail view in the UI with citations and payload preview.
- [x] Add reject/cancel flow with reason capture.
- [x] Add edit-before-send for Jira comments and Slack DMs.
- [x] Add audit records for approve, reject, edit, execute, and failure.

## Acceptance Criteria

- A manager can approve or reject a proposal from Slack.
- A manager can inspect citations before approval.
- Edited proposals preserve the original draft in audit history.
- Rejected proposals never execute.

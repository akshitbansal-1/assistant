# Phase 10: Retrieval Quality And Cost Control

## Goal

Improve answer quality without sending large histories to the LLM.

## Tasks

- [ ] Add cached per-task context summaries.
- [ ] Add cached per-thread Slack summaries.
- [x] Add explicit citation ranking by recency, source type, and task match.
- [x] Add lexical search-index fallback after structured lookup misses.
- [ ] Add fuzzy fallback using embeddings only after lexical lookup misses.
- [x] Add retrieval trace output for debugging.
- [x] Add token budget guards before retrieval LLM calls and retrieved context assembly.
- [x] Add confidence calibration rules by source and recency.

## Acceptance Criteria

- `/whereis` uses structured DB queries first.
- Lexical search index is used before embeddings to keep MVP search cheap and explainable.
- LLM prompts contain compact context, not raw histories.
- Answers include ranked citations.
- Retrieval traces explain why each source was used.

# Second Brain vault

This repository is a knowledge-base vault. Before doing anything, read and follow:

1. [AGENTS.md](../AGENTS.md) — the rules (evidence classification, decisions, what you may and may not do)
2. [INGEST.md](../INGEST.md) — the ingest procedure
3. [PLAN-SCHEMA.md](../PLAN-SCHEMA.md) — the write-plan format

You never edit `wiki/`, `raw/`, `state/` or `tools/`. All wiki changes are written as a plan JSON in
`plans/pending/` and applied with `tools/second_brain.py apply-plan` only after the human reviewed it: the human
runs it, or tells you to and confirms the prompt (AGENTS.md §8). A PreToolUse hook
(`.github/hooks/second-brain-guard.json`) denies anything else.

---
name: second-brain-ingest
description: >
  Ingest a source from raw/ into this Second Brain vault by writing a reviewed write plan.
  Use when the user says "ingest <source_id>", "ingest raw/...", "process this source",
  or adds a new file to raw/ and wants it in the wiki.
---

# Second Brain — Ingest

This skill is a thin entry point. The rules live in the vault and are the single source of truth:

1. Read [AGENTS.md](../../../AGENTS.md) (evidence classification, decision lifecycle, prohibitions).
2. Follow [INGEST.md](../../../INGEST.md) step by step.
3. Write the plan in the format of [PLAN-SCHEMA.md](../../../PLAN-SCHEMA.md).

Hard limits (also enforced by a PreToolUse hook and by `tools/second_brain.py`):

- Write only `plans/pending/<plan_id>.json`. Never edit `wiki/`, `raw/`, `state/`, `tools/`.
- Run only `python tools/second_brain.py status|source|trace|hash|validate-plan|render-plan`, one per call, no pipes.
- Never run `apply-plan`; the human applies after reviewing the render.

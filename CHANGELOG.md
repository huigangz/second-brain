# Changelog

## 0.1.0 — unreleased

First public version.

- `tools/second_brain.py`: write-plan applier (schema v2) with section addressing, hash-bound approval
  (`apply-plan --approve <sha256>`), source registry with secret preflight, transactions with rollback,
  human-edit protection, `maintain` / `maintain --fix` / `maintain --revert`, `trace`, deletion and
  source retraction.
- `tools/agent_guard.py`: one PreToolUse guard for Claude Code, GitHub Copilot (VS Code / CLI) and Codex.
- `vault-template/`: agent rules (`AGENTS.md`, `INGEST.md`, `PLAN-SCHEMA.md`), the ingest skill and the
  per-agent permission and hook configuration.
- `install` / `upgrade`: create a vault from this project and keep its rule files and tools in sync.
- Reviewed in twelve rounds of code review before release; 149 tests.

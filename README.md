# Second Brain

A personal knowledge wiki that an AI agent keeps up to date from your documents, meetings and work sessions,
without being allowed to write to it directly.

The agent reads a new source and proposes a **write plan**: a JSON list of section-level edits to wiki pages.
A small standard-library tool validates the plan, renders it as a diff for you to review, and applies it only
with your approval, bound to the plan's SHA-256. Every apply is a transaction you can roll back.

Inspired by [NicholasSpisak/second-brain](https://github.com/NicholasSpisak/second-brain). That project
gives agents written instructions only; this one adds enforcement around them.

## Why a plan, not direct edits

- **Human approval is the safety layer.** In the evaluation that shaped this design, human review of plans
  caught every agent misjudgement that the rules alone did not.
- **What you approve is what gets applied.** `apply-plan --approve <sha256>` refuses a plan changed after you
  rendered it. Every page, source and registry file the plan read is checked again before and during the write.
  If anything changed on disk, for example an edit you made in Obsidian meanwhile, nothing is written.
- **Your edits are protected.** A section you edited by hand cannot be overwritten without your explicit
  `human_override`. `maintain` reports edits made outside a plan, and `maintain --revert` undoes them.
- **Decisions have a lifecycle.** A decision is proposed, then active, then superseded or revoked, with a
  temporal check, so an older source can never overturn a newer decision.
- **Sources are evidence.** They are registered with a content hash and a secret preflight. A plan is bound
  to the exact version it was written from. Sources can be traced and retracted.

## Requirements

- Python 3.10+ (standard library only; tested on 3.12)
- One of: Claude Code, GitHub Copilot (VS Code agent or CLI), Codex
- Optional: Obsidian as the reader and editor for `wiki/`

## Quick start

```bash
git clone https://github.com/huigangz/second-brain.git
cd second-brain
python -m unittest discover -s tests          # 150 tests

python tools/second_brain.py install ../my-vault
cd ../my-vault
# put files into raw/documents, raw/meetings or raw/sessions, then:
python tools/second_brain.py discover
python tools/second_brain.py status
```

Then start your agent in `my-vault` and say `ingest <source_id>`. The agent writes
`plans/pending/<plan_id>.json`. You review and apply it:

```bash
python tools/second_brain.py render-plan plans/pending/<plan_id>.json   # first line: PLAN SHA256
python tools/second_brain.py apply-plan  plans/pending/<plan_id>.json --approve <sha256>
# or: python tools/second_brain.py reject-plan plans/pending/<plan_id>.json --reason "..."
```

The vault's own [README](vault-template/README.md) covers daily use: reviewing, rollback, `maintain`,
deletion and retraction.

## Keeping a vault up to date

The project is the single source of each vault's rule files and tools. After pulling a new version, run:

```bash
python tools/second_brain.py upgrade ../my-vault --dry-run
python tools/second_brain.py upgrade ../my-vault
```

`upgrade` touches only the files `install` put there. `raw/`, `wiki/` and `plans/` are never changed, and
in `state/` it only rewrites `state/kit.json` (the record of what it installed) and appends one event to
`state/events.jsonl`. A rule file you edited inside the vault is reported as `CONFLICT` and is not
overwritten unless you pass `--force`. If a file is edited while the upgrade runs, the upgrade stops, puts
back what it had already written and records nothing. Changes you want to keep belong in this project's
`vault-template/`.

## Agent support and its limits

A single PreToolUse guard, `tools/agent_guard.py`, serves all agents:

- Files can be written only to `plans/pending/*.json`.
- Shell commands must be read-only.
- Of the tool's commands, the agent may run only `status`, `source`, `trace`, `hash`, `validate-plan` and
  `render-plan`.

| Agent | Enforcement |
|---|---|
| Claude Code | Permission rules plus the guard hook. Accept the workspace-trust dialog once, or the allow rules are ignored. |
| GitHub Copilot | Workspace hook plus VS Code settings. Not yet smoke-tested. |
| Codex | Read-only sandbox, rules and the hook. **In testing, Codex's `apply_patch` could still write to `wiki/`** (openai/codex#27833). Prefer Claude Code for ingest. If you use Codex, run `maintain` after each session. |

## Layout

```
tools/second_brain.py    the applier and vault CLI (install, discover, validate/render/apply, maintain, rollback, ...)
tools/agent_guard.py     PreToolUse guard shared by all agents
vault-template/          what `install` copies into a vault: agent rules, ingest skill, agent configuration
tests/                   unit tests (stdlib unittest)
```

The agent rules and the vault README are written in Chinese. Agents follow them without problems, but
you will need to read them to review plans well.

## License

[MIT](LICENSE)

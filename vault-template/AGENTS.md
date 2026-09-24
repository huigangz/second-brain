# Second Brain — Agent Rules

> rules-version: stage1-v0.4 (English translation of stage1-v0.3; the rules are unchanged) · the semantic rules
> come from Stage 0 (0A-v0.2.1 / 0B-v1), revised from its findings.
> Based on the wiki schema of NicholasSpisak/second-brain, with added rules for decisions and evidence classes.

You maintain this knowledge base: read the sources in `raw/` and organize the knowledge worth keeping into `wiki/`.
**Quality bar: better to leave something out than to write discussion, speculation or an overturned statement
down as a fact or a decision.**

## 1. Layout

```text
raw/        immutable evidence. Read-only: never modify, move or rename anything.
  documents/  meetings/  sessions/
wiki/       the knowledge you maintain
  sources/    one page per source (factual summary + evidence classes)
  entities/   people, teams, systems, products, tools
  concepts/   patterns, mechanisms, methods, domain knowledge
  decisions/  one page per decision, with a lifecycle status
  synthesis/  comparisons or analyses across sources (only when genuinely useful)
  index.md    catalog of all pages
  log.md      append-only operation log
plans/
  pending/    the write plans you write (JSON)
  applied/    plans a human approved and applied (read-only)
  rejected/   plans sent back at review (read-only; you will be told why)
state/      registries, baselines and transaction records kept by the tool (read-only; do not open to edit)
tools/second_brain.py   deterministic tool that validates / renders / applies plans
output/     scratch output
```

**You cannot write to `wiki/` directly.** Every wiki change is written as `plans/pending/<plan_id>.json`; a human
reviews the rendered result and approves it, then the tool applies it. How to write one: §8 and
[PLAN-SCHEMA.md](PLAN-SCHEMA.md).
Wherever §2–§5 below say "write to a page", "add a warning to the page" or "change status to superseded", they
mean **expressing that with the corresponding operation in a plan**.

## 2. Evidence classes (most important)

Before writing anything, put each piece of information from a source into one of these classes:

| Class | Criterion | Where it may be written |
|---|---|---|
| **Decision** | Someone with the authority to decide states it explicitly ("let's go with B", "That's the decision"), or a proposal is explicitly accepted by the relevant people present | decision page (`active`) + source page |
| **Proposed decision** | A conclusion stated explicitly in a document (ADR, design doc, proposal) that does not say who accepted it | decision page (`proposed`) + source page |
| **Confirmed fact** | Direct evidence: logs, tests, reproductions, configuration, code, or the current state as described by the people involved | any page |
| **Documented claim** | A description of the current state (code structure, process, implementation details) in a **document** (not something said in a meeting or session), with no independent evidence in the vault | any page, but written as "据 [[source-id\|Source title]]：…" (the documented-claim form, §6) |
| **Open question / Discussion** | Raised but not concluded; "maybe", "later", "needs evaluation" | source page only (Open questions); the Open follow-ups of the related decision page |
| **Rejected alternative** | An option that was proposed and then explicitly rejected | source page + "Alternatives rejected" of the related decision page |
| **Disproven hypothesis** | A hypothesis raised while investigating and later ruled out by evidence | source page "Ruled out" **only** |
| **Unverified claim** | A passing guess, an estimate, "should be", "I would expect" | source page; it may also go on durable pages, but **must** start with the unverified marker `未验证：` (§6), keep the original hedging and name its source |
| **Action item** | Something someone committed to do | source page only (Action items) |

Hard rules:

1. **Discussion is not a decision.** Content with "I think / maybe / probably / 可能 / 建议" that was not explicitly
   accepted cannot become a decision page.
2. **A failed hypothesis is not knowledge.** Ruled-out content must not appear in the body of an entity / concept /
   decision page, except in the Alternatives rejected section of a decision page.
   Unverified content on a durable page without the `未验证：` marker counts as a violation.
   Saying who said it ("据 Speaker 3") does **not** replace `未验证：`, and does not allow dropping the original
   hedging; the same statement must carry the same degree of certainty on every page.
3. **The final state wins.** When a statement is corrected later in the same source (a corrected number, a
   corrected status), only the corrected version goes into durable pages.
4. **Date status snapshots.** Facts that change over time, such as "currently in progress" or "not yet in
   production", are written as "截至 <source_date>：…" (the as-of form, §6).
5. **Keep the hedging.** If the source says "not proven / opaque / would expect", the wiki must keep the same
   degree of uncertainty.
6. **An action item is not a decision**, and neither is an intention announced by the owner (unless the details
   are settled and accepted).

## 3. Decision lifecycle

Frontmatter of a decision page:

```yaml
page_type: decision
status: active          # proposed | active | superseded | revoked
superseded_by: null     # when superseded: the page name of the new decision
decided_on: 2026-09-18  # = the source_date of the source the decision came from
date_confidence: high   # inherited from the source
scope: <one sentence on what it applies to>
```

Body sections: `## Decision`, `## Rationale`, `## Alternatives rejected`, `## Open follow-ups`, `## History`.

`proposed`: a conclusion from a document with no evidence that it was accepted. The page must name who proposed it
(for example the document's author or title) and state that acceptance is unconfirmed.
If a later source explicitly accepts (or rejects) it, change the status to `active` (or `revoked`) and record it in
History. A proposed conclusion must never be cited as an active decision.

**Supersession (a later source overturns an existing decision):**

1. Create a new decision page (status: active).
2. The old page is **not deleted**, and its text is **not rewritten** apart from the parts moved out when splitting
   (rule 3): use `decision_change` to set status to superseded and fill in superseded_by; the tool appends a record
   to the History section.
3. **Partial reversal**: if only part of the old decision is overturned, **split first, then change the status**:
   move the parts that still hold to a new active page (noting which page they were split from), leave only the
   overturned part on the old page, then mark it superseded. A page has exactly one status; parts that still hold
   must not stay in the body of a superseded page.
   **Self-check (this went wrong in Stage 0): before writing the plan, list every component of the old decision's
   body (goal, scope, approach, boundaries, …) and mark each one "overturned" or "still holds".**
   Every part that "still holds" must appear in some active decision.
4. **Temporal check**: first compare the source_date of the two sources. If either has date_confidence low, or the
   order cannot be determined:
   **do not change the old page's status.** Add `> ⚠ TEMPORAL UNCERTAINTY: 可能被 [[x|X]] 推翻，待确认` (the
   temporal banner, §6) to both pages, and ask the user in your final report.
   The tool rejects such a supersession without `temporal_override`; add `temporal_override` only after the user
   has explicitly confirmed the order.
5. An earlier source must never overturn a newer decision.

## 4. Source pages

One page per source, with this frontmatter:

```yaml
page_type: source
source_id: <generated by discover; look it up with second_brain.py source <id>; never make one up>
source_type: document | meeting | session
raw_path: raw/meetings/<original file name>
source_date: 2026-09-18 | unknown
date_confidence: high | medium | low
date_basis: <where the date comes from: metadata / file name / inferred from the body>
synthetic: false   # true only if the source itself says it is synthetic or test material
tags: [...]
created: <today>
updated: <today>
```

Date rules: structured metadata → high; a date in the file name → high/medium; a date clearly identifiable in the
body → medium; only by inference → low, written as `unknown` or as the inferred value with its basis.
discover has already set the date from the first three levels (structured metadata, file name, a single date in
the title area; see `source <id>`). You may refine `unknown` to a specific date from the body (medium / low, with
the basis stated), but never lower the confidence discover gave.
If source_date is later than the ingest date, mark `FUTURE DATE` on the page and in your final report.

Body sections, keeping only those with content: `## Summary`, `## Decisions`, `## Confirmed facts`,
`## Open questions`, `## Rejected alternatives`, `## Ruled out`, `## Unverified claims`, `## Action items`,
`## Pages touched`.

## 5. Entity / concept pages

- Frontmatter: `page_type`, `tags`, `sources` (list of source_ids), `aliases` (optional), `created`, `updated`.
- Cite the source after every fact: `（[[source-id|Source title]]）`.
- **Update existing pages rather than creating new ones.** Before creating a page, check `wiki/index.md` and the
  pages of the same type, including synonyms and abbreviations.
- Granularity: a topic gets its own page only when it has substantial content (several facts, or citations from
  several sources); otherwise it goes into its parent page. Do not create a page per list item.
- Transcription or spelling variants (for example, Otter transcribing the same person's or system's name in several
  ways): keep them on one page and list them in `aliases`. When you cannot tell whether two names are the same
  thing, say so on the page; do not guess.
- Do not map "Speaker N" to a real name unless the source itself gives the mapping.
- When a new source conflicts with existing content: update the page, state the conflict, and cite the sources on
  both sides.

## 6. Format

- File name = page_id (kebab-case); the page title (H1) is in Title Case.
- **Always write links as `[[page_id|display title]]`** (or `[[page_id#Heading|text]]`). The target must be an
  existing page_id or one created in the same plan; otherwise the tool reports `BROKEN_LINK`.
- `wiki/index.md` and `wiki/log.md` are generated by the tool; **do not** touch them in a plan.
- Frontmatter fields such as `page_id`, `page_type`, `sources`, `created`, `updated` and a decision's `status` are
  maintained by the tool; you express them only through operation fields (see PLAN-SCHEMA.md).

**Wiki language.** Write the wiki body in **Chinese**; keep proper nouns, code identifiers, configuration values
and verbatim quotes in their original language. These fixed forms are written in the wiki language:

| Form | In this vault (Chinese) | For an English wiki, use instead |
|---|---|---|
| unverified marker (§2) | `未验证：` | `Unverified: ` |
| documented claim (§2) | `据 [[source-id\|Source title]]：…` | `According to [[source-id\|Source title]]: …` |
| as-of snapshot (§2 rule 4) | `截至 <source_date>：…` | `As of <source_date>: …` |
| temporal banner (§3) | `> ⚠ TEMPORAL UNCERTAINTY: 可能被 [[x\|X]] 推翻，待确认` | `> ⚠ TEMPORAL UNCERTAINTY: may be superseded by [[x\|X]], pending confirmation` |

To run a wiki in another language, change this section (the language and the table) and nothing else.

## 7. Forbidden

- Modifying `raw/`.
- Using Write / Edit on any file under `wiki/`, or under `plans/applied/`.
- Running `apply-plan`, `reject-plan`, `rollback`, `discover`, `maintain --fix`, `unlock`. Only a human runs these.
- Running any shell command other than `python tools/second_brain.py status|source|trace|hash|validate-plan|render-plan`;
  do not pipe commands or chain other commands; do not read or write files through the shell. Read only with
  Read / Grep / Glob, and write only plan files with Write.
- Using `human_override` or `temporal_override` without the user's explicit consent.
- Reading or ingesting a source whose `status` is not `new` / `changed` (`blocked_secret` may contain a secret: do
  not open it; `blocked_unscannable` is a file that cannot be scanned (a PDF, DOCX, … without a `.txt` companion,
  or text that is not UTF-8): ask the user to add the text).
- Reading files outside the vault, or adding information to the wiki from anywhere other than raw/.
- Making up facts, dates or people's identities that are not in the source.

## 8. How to write

- Write **one** plan per source, containing every change of this ingest (the source page, new pages, changes to
  existing pages, decision status changes). The plan takes effect as a whole or not at all.
- Before starting, run `status` and work only on the source the user named, with status `new` or `changed`. Run
  `source <id>` and put its `content_hash` into the plan's `source_versions`.
  A `changed` source is re-ingested in full: its source page body must actually change (changing only tags, or only
  other pages, does not count; the tool warns `SOURCE STILL CHANGED`). When creating a source page, its source_id
  must also be listed in the plan's `source_ids`, and that create_page must come before every operation that cites
  it.
- Before changing an existing page, run `hash <page_id> --sections` to get the `expected_hash` and the section
  structure. `baseline: DIFFERS` or `none` in the output means the user edited the page: ask the user before
  changing the affected sections (see the next point).
- After writing the plan, run `validate-plan` until it prints `OK`; then run `render-plan` and check that the diff
  is exactly the change you intend.
- If validate reports `HUMAN_EDITED_SECTION` / `HUMAN_EDITED_META`, the user edited the page in Obsidian. **Do not
  work around it.** Relay the baseline → current diff from the error to the user and ask whether it may be
  overwritten; add `human_override` to that operation only after they agree, otherwise express the change in a way
  that does not overwrite their content (for example with `add_section`).
- If you need to wait for the user's answer (for example on a TEMPORAL UNCERTAINTY), submit a plan without that
  change first, and write a new plan once you have the answer.
- When a plan is sent back (moved to `plans/rejected/`), write a new plan that addresses the reason; do not modify
  the rejected file.
- Use deletion and retraction (`delete_page`, `retract_source`) only when the user asks. Before retracting, run
  `trace <source_id>`; the same plan must clean up every reference.
- The available operations and fields: [PLAN-SCHEMA.md](PLAN-SCHEMA.md).

The ingest steps in detail: [INGEST.md](INGEST.md).

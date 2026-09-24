# Write Plan quick reference (schema v1.1, `schema_version: 2`)

A quick reference for the agent. The tool's full behaviour is defined by the checks in `tools/second_brain.py`:
always run `validate-plan` after writing a plan.

## Page structure

- File: `wiki/<sources|entities|concepts|decisions|synthesis>/<page_id>.md`. `page_id` is kebab-case
  (`^[a-z0-9]+(-[a-z0-9]+)*$`, at most 80 characters), unique across the whole wiki, with no type prefix.
- Page = frontmatter (maintained by the tool) + `# Title` (H1) + body.
- `__preamble__` = the content between the H1 and the first `##`.
- A section runs from a `##`–`######` heading to the next heading of the same or a higher level, **including its
  subsections**.
- `section_path` starts at `##` and names each heading's text level by level: `["Facts", "Detail"]` means the
  `### Detail` under `## Facts`.
- **Links**: always `[[page_id|display title]]` or `[[page_id#Heading|text]]`. The target must be an existing
  page_id or one created in this plan (otherwise `BROKEN_LINK`); content in code blocks and code spans is not
  checked.
- No two headings with the same text under the same parent; headings cannot skip a level (a `####` directly under a
  `##` is invalid).

## Plan

```json
{
  "schema_version": 2,
  "plan_id": "plan-20260922-ab12",
  "created_at": "2026-09-22T10:00:00+08:00",
  "source_ids": ["meeting-2026-09-10-widget-review"],
  "source_versions": { "meeting-2026-09-10-widget-review": "sha256:…" },
  "summary": "one sentence",
  "operations": [ ... ]
}
```

- The file goes to `plans/pending/<plan_id>.json`; the file name must be exactly the plan_id. `plan_id` has the form
  `plan-YYYYMMDD-xxxx` (4–8 lowercase letters or digits).
- `operation_id` has the form `op-001`, `op-002`, …; operations run in array order.
- Every operation carries `source_ids` (except the `create_page` of a source page itself), and they must be a
  subset of the plan-level `source_ids`.
  Every source_id must already have a source page, or have one created earlier in the same plan.
- Every operation that changes an **existing** page carries `expected_hash`: the page hash printed by the `hash`
  command when the plan was written. All operations on the same page use the **same** value. Pages created in this
  plan carry no `expected_hash`.
- Later operations address the page as it is after the earlier operations.
- `source_versions`: one entry for every source_id in the plan, taken from the `content_hash` printed by
  `source <id>`. If the raw file changes after the plan is written, the tool reports `SOURCE_STALE`.
- Any operation that changes an existing page may carry
  `"human_override": {"confirmed_by_user": true, "note": "…"}`, **only after the user has explicitly agreed to
  overwrite their own edits made in Obsidian**.

## Operations

### create_page

```json
{"operation_id": "op-001", "type": "create_page", "page_id": "widget-service", "page_type": "entity",
 "title": "Widget Service", "meta": {"tags": ["system"], "aliases": ["widgets"], "scope": "optional"},
 "body": "One-sentence introduction.\n\n## Facts\n\n- …（[[meeting-2026-09-10-widget-review|Widget Review Meeting]]）",
 "source_ids": ["meeting-2026-09-10-widget-review"]}
```

- `body` is the content after the H1, without frontmatter or H1; its headings start at `##`.
- `meta` may contain only `tags`, `aliases`, `scope`.
- A **decision page** also needs:
  `"decision": {"status": "proposed|active", "decided_on": "YYYY-MM-DD|unknown", "date_confidence": "high|medium|low"}`
- A **source page** also needs (its `page_id` must equal its `source_id`, and `source_ids` may be omitted):
  `"source": {"source_id": "…", "source_type": "document|meeting|session", "raw_path": "raw/…", "source_date": "YYYY-MM-DD|unknown", "date_confidence": "high|medium|low", "date_basis": "…", "synthetic": false}`
  `source_id`, `source_type` and `raw_path` must match the output of `source <id>`. The source must also be listed
  in the plan-level `source_ids` (and so in `source_versions`), and this create_page must come before every
  operation that cites it. Only a source with status `new` can get a source page (an ingested one reports
  `SOURCE_ALREADY_INGESTED`; a `changed` source is re-ingested by changing the body of its existing source page).

### update_section

Replaces a section's text **and all its subsections**; the heading line itself stays.
`section_path: ["__preamble__"]` replaces the preamble.
Headings in `content` must be deeper than the target section and must not skip a level; the content for the
preamble cannot contain headings.

```json
{"operation_id": "op-002", "type": "update_section", "page_id": "widget-service", "expected_hash": "sha256:…",
 "section_path": ["Facts"], "content": "- …", "source_ids": ["…"]}
```

### update_section_body

Replaces only the section's **own text** (after the heading, before the first subheading); subsections stay as
they are. `content` cannot contain headings.
Use it to change a paragraph or a table without rewriting the subsections below.

```json
{"operation_id": "op-002", "type": "update_section_body", "page_id": "widget-service", "expected_hash": "sha256:…",
 "section_path": ["Facts"], "content": "- …", "source_ids": ["…"]}
```

### append_to_section

Appends content to the **end of the section's whole subtree** (if it has subsections, the content lands in the
last one). `content` cannot contain headings.

```json
{"operation_id": "op-003", "type": "append_to_section", "page_id": "widget-service", "expected_hash": "sha256:…",
 "section_path": ["Facts"], "content": "- a new fact（[[…]]）", "source_ids": ["…"]}
```

### add_section

```json
{"operation_id": "op-004", "type": "add_section", "page_id": "widget-service", "expected_hash": "sha256:…",
 "parent_path": [], "heading": "Incidents", "position": {"after": "Facts"}, "content": "…", "source_ids": ["…"]}
```

- `parent_path: []` creates a new top-level `##`. `position` is `"start"`, `"end"` (default) or
  `{"after": "<sibling heading>"}`.
- No two headings with the same text under the same parent; a heading cannot be empty or `__preamble__`.

### update_meta

Changes only `title`, `tags`, `aliases`, `scope`; changing `title` also updates the H1.

```json
{"operation_id": "op-005", "type": "update_meta", "page_id": "widget-service", "expected_hash": "sha256:…",
 "set": {"aliases": ["widgets", "widget svc"]}, "source_ids": ["…"]}
```

### decision_change

```json
{"operation_id": "op-006", "type": "decision_change", "page_id": "use-queue-a", "expected_hash": "sha256:…",
 "new_status": "superseded", "superseded_by": "use-queue-b",
 "history_note": "superseded by …",
 "source_ids": ["…"]}
```

- Allowed transitions: proposed→active, proposed→revoked, proposed/active→superseded, active→revoked. superseded
  and revoked are final.
- `superseded_by` must be a decision page with status active (existing, or created earlier in this plan).
- The tool appends a line to the end of `## History` (`history_note` is required).
- **The tool performs the temporal check**:
  - both sides have a definite date and date_confidence is not low → decided by date: an earlier decision cannot
    overturn a newer one;
  - either date is unknown or low → rejected (`TEMPORAL_UNCERTAINTY`), unless the operation carries
    `"temporal_override": {"confirmed_by_user": true, "note": "how the user confirmed it"}`.
    **Add this field only after the user has explicitly answered.**

### delete_page (only when the user asks)

```json
{"operation_id": "op-007", "type": "delete_page", "page_id": "old-topic", "expected_hash": "sha256:…",
 "reason": "merged into [[new-topic|New Topic]]", "source_ids": []}
```

- A decision page cannot be deleted (use `decision_change → revoked`); a source page cannot be deleted (use
  `retract_source`).
- After the plan is applied, no other page may still link to the deleted page: the same plan must change those
  pages first.

### retract_source (only when the user asks)

```json
{"operation_id": "op-009", "type": "retract_source", "page_id": "meeting-2026-09-10-widget-review",
 "expected_hash": "sha256:…", "reason": "ingested by mistake", "source_ids": ["meeting-2026-09-10-widget-review"]}
```

- Deletes the source page and marks the source `retracted`. Run `trace <source_id>` first; the same plan must:
  - change every page whose `sources` include it (the tool removes it from those pages' `sources` itself), and
    remove the links to it;
  - revoke every active / proposed decision whose only origins are retracted sources.

### restructure_page

**Not available** in this vault (validate reports `RESTRUCTURE_DISABLED`). Use section-level operations or
`update_section_body`.

## Commands

```text
python tools/second_brain.py status                          # source statuses, pending plans, lock, incomplete transactions
python tools/second_brain.py source <source_id>              # content_hash, date, status
python tools/second_brain.py trace <source_id>               # pages and sections citing this source (before a retraction)
python tools/second_brain.py hash <page_id> --sections       # page / section hashes and structure, human-edit state
python tools/second_brain.py validate-plan plans/pending/<plan_id>.json
python tools/second_brain.py render-plan  plans/pending/<plan_id>.json
```

Run each command on its own; do not pipe.

Common error codes:
- `PLAN_STALE` (a page changed: get the hash again), `SOURCE_STALE` (the raw file changed), `SOURCE_NOT_READY`,
  `SOURCE_ALREADY_INGESTED`;
- `HUMAN_EDITED_SECTION` / `HUMAN_EDITED_META` (the user edited it: ask the user first);
- `BROKEN_LINK`, `PATH_NOT_FOUND`, `PATH_AMBIGUOUS`, `HEADING_LEVEL`, `CONTENT_HEADING`;
- `UNKNOWN_SOURCE`, `UNKNOWN_PAGE`, `PAGE_EXISTS`, `FORBIDDEN_META`, `BAD_TRANSITION`, `TEMPORAL_UNCERTAINTY`,
  `TEMPORAL_ORDER`, `DATE_DOWNGRADE`;
- `DELETE_FORBIDDEN`, `SOURCE_STILL_CITED`, `DECISION_FROM_RETRACTED`, `SECRET_IN_PLAN`.

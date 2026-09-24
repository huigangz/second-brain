# Write Plan 速查（schema v1.1，`schema_version: 2`）

这是给 Agent 用的速查表。工具的完整行为以 `tools/second_brain.py` 的校验为准：写完 plan 后一定要运行 `validate-plan`。

## 页面结构

- 文件：`wiki/<sources|entities|concepts|decisions|synthesis>/<page_id>.md`。`page_id` 是 kebab-case（`^[a-z0-9]+(-[a-z0-9]+)*$`），全 wiki 唯一，不带类型前缀。
- 页面 = frontmatter（由工具维护）+ `# 标题`（H1）+ 正文。
- `__preamble__` = H1 与第一个 `##` 之间的内容。
- section = 从 `##`–`######` 开始，到下一个同级或更高级 heading 为止，**包含子 section**。
- `section_path` 从 `##` 开始，按 heading 文本逐级写：`["Facts", "Detail"]` 表示 `## Facts` 下的 `### Detail`。
- **链接**：一律写成 `[[page_id|显示标题]]` 或 `[[page_id#Heading|文字]]`。目标必须是已存在或本 plan 中创建的 page_id（否则 `BROKEN_LINK`）；代码块中的内容不检查。
- 同一父节点下不能有同名 heading；heading 不能跳级（`##` 下面直接接 `####` 是非法的）。

## Plan

```json
{
  "schema_version": 2,
  "plan_id": "plan-20260922-ab12",
  "created_at": "2026-09-22T10:00:00+08:00",
  "source_ids": ["meeting-2026-09-10-widget-review"],
  "source_versions": { "meeting-2026-09-10-widget-review": "sha256:…" },
  "summary": "一句话说明",
  "operations": [ ... ]
}
```

- 文件放在 `plans/pending/<plan_id>.json`。`plan_id` 的格式为 `plan-YYYYMMDD-xxxx`（4–8 位小写字母或数字）。
- `operation_id` 的格式为 `op-001`、`op-002`……，按数组顺序执行。
- 每个 operation 都要写 `source_ids`（source 页自身的 `create_page` 除外），并且必须是 plan 级 `source_ids` 的子集。
  每个 source_id 必须已经有 source 页，或者在同一个 plan 中创建。
- 修改**已有**页面的每个 operation 都要带 `expected_hash`：值是生成 plan 时 `hash` 命令输出的 page hash。
  同一页面的多个 operation 用**同一个**值。本 plan 中新建的页面不带 `expected_hash`。
- 后面的 operation 基于前面 operation 执行后的页面状态寻址。
- `source_versions`：plan 中每个 source_id 都要有，值取自 `source <id>` 输出的 `content_hash`。raw 在写 plan 之后变了，工具会报 `SOURCE_STALE`。
- 任何修改已有页面的 operation 都可以带 `"human_override": {"confirmed_by_user": true, "note": "…"}`，**只在用户明确同意覆盖其在 Obsidian 中的修改后使用**。

## Operations

### create_page

```json
{"operation_id": "op-001", "type": "create_page", "page_id": "widget-service", "page_type": "entity",
 "title": "Widget Service", "meta": {"tags": ["system"], "aliases": ["widgets"], "scope": "可选"},
 "body": "一句话介绍。\n\n## Facts\n\n- …（[[meeting-2026-09-10-widget-review|Widget Review Meeting]]）",
 "source_ids": ["meeting-2026-09-10-widget-review"]}
```

- `body` 是 H1 之后的内容，不含 frontmatter 和 H1；其中的 heading 从 `##` 开始。
- `meta` 只能包含 `tags`、`aliases`、`scope`。
- **decision 页**需要加：`"decision": {"status": "proposed|active", "decided_on": "YYYY-MM-DD|unknown", "date_confidence": "high|medium|low"}`
- **source 页**需要加（此时 `page_id` 必须等于 `source_id`，可以省略 `source_ids`）：
  `"source": {"source_id": "…", "source_type": "document|meeting|session", "raw_path": "raw/…", "source_date": "YYYY-MM-DD|unknown", "date_confidence": "high|medium|low", "date_basis": "…", "synthetic": false}`
  `source_id`、`source_type`、`raw_path` 必须与 `source <id>` 的输出一致。只有 status 为 `new` 的 source 能建 source 页（已 ingest 的会报 `SOURCE_ALREADY_INGESTED`；`changed` 的 source 通过修改已有 source 页来重新 ingest）。

### update_section

替换一个 section 的正文**和它的全部子 section**，heading 行本身保留。`section_path: ["__preamble__"]` 表示替换 preamble。
`content` 中的 heading 必须比目标 section 深，而且不能跳级；preamble 的 content 中不能有 heading。

```json
{"operation_id": "op-002", "type": "update_section", "page_id": "widget-service", "expected_hash": "sha256:…",
 "section_path": ["Facts"], "content": "- …", "source_ids": ["…"]}
```

### update_section_body

只替换 section **自身的正文**（heading 之后、第一个子 heading 之前），子 section 保持不变。`content` 中不能有 heading。
只想改一段文字或一张表、又不想重写下面的子 section 时用它。

```json
{"operation_id": "op-002", "type": "update_section_body", "page_id": "widget-service", "expected_hash": "sha256:…",
 "section_path": ["Facts"], "content": "- …", "source_ids": ["…"]}
```

### append_to_section

把内容追加到 section **整个子树的末尾**（如果有子 section，内容会落在最后一个子 section 里）。`content` 中不能有 heading。

```json
{"operation_id": "op-003", "type": "append_to_section", "page_id": "widget-service", "expected_hash": "sha256:…",
 "section_path": ["Facts"], "content": "- 新事实（[[…]]）", "source_ids": ["…"]}
```

### add_section

```json
{"operation_id": "op-004", "type": "add_section", "page_id": "widget-service", "expected_hash": "sha256:…",
 "parent_path": [], "heading": "Incidents", "position": {"after": "Facts"}, "content": "…", "source_ids": ["…"]}
```

- `parent_path: []` 表示新建顶层 `##`。`position` 可以是 `"start"`、`"end"`（默认）或 `{"after": "<同级 heading>"}`。
- 同一父节点下不能有同名 heading。

### update_meta

只能修改 `title`、`tags`、`aliases`、`scope`；修改 `title` 时，工具会同步修改 H1。

```json
{"operation_id": "op-005", "type": "update_meta", "page_id": "widget-service", "expected_hash": "sha256:…",
 "set": {"aliases": ["widgets", "widget svc"]}, "source_ids": ["…"]}
```

### decision_change

```json
{"operation_id": "op-006", "type": "decision_change", "page_id": "use-queue-a", "expected_hash": "sha256:…",
 "new_status": "superseded", "superseded_by": "use-queue-b",
 "history_note": "被 … 取代",
 "source_ids": ["…"]}
```

- 允许的状态迁移：proposed→active、proposed→revoked、proposed/active→superseded、active→revoked。superseded 和 revoked 是终态。
- `superseded_by` 必须是 status 为 active 的 decision 页（已存在，或在本 plan 中先创建）。
- 工具会在 `## History` 末尾自动追加一行记录（`history_note` 必填）。
- **时间校验由工具执行**：
  - 两边都有明确日期，且 date_confidence 不是 low → 按日期判断，较早的决定不能推翻较新的决定；
  - 任一方日期为 unknown 或 low → 被拒绝（`TEMPORAL_UNCERTAINTY`），除非加上
    `"temporal_override": {"confirmed_by_user": true, "note": "用户如何确认的"}`。
    **只有在用户明确回答后才能加这个字段。**

### delete_page（只在用户要求时使用）

```json
{"operation_id": "op-007", "type": "delete_page", "page_id": "old-topic", "expected_hash": "sha256:…",
 "reason": "内容已并入 [[new-topic|New Topic]]", "source_ids": []}
```

- decision 页不能删（用 `decision_change → revoked`）；source 页不能删（用 `retract_source`）。
- plan 执行后，其他页面中不能再有指向被删页面的链接：同一个 plan 要先改掉它们。

### retract_source（只在用户要求时使用）

```json
{"operation_id": "op-009", "type": "retract_source", "page_id": "meeting-2026-09-10-widget-review",
 "expected_hash": "sha256:…", "reason": "误 ingest", "source_ids": ["meeting-2026-09-10-widget-review"]}
```

- 删除 source 页，并把 source 标为 `retracted`。先运行 `trace <source_id>`，同一个 plan 必须：
  - 修改每一个 `sources` 包含它的页面（工具会自动从这些页面的 `sources` 中去掉它），并去掉指向它的链接；
  - revoke 所有 origin 只有它的 active / proposed decision。

### restructure_page

本 vault 中**不可用**（validate 会报 `RESTRUCTURE_DISABLED`）。用 section 级 operation 或 `update_section_body`。

## 命令

```text
python tools/second_brain.py status                          # sources 状态、pending plans、锁、未完成事务
python tools/second_brain.py source <source_id>              # content_hash、日期、状态
python tools/second_brain.py trace <source_id>               # 引用这个 source 的页面和 section（撤回前使用）
python tools/second_brain.py hash <page_id> --sections       # page / section hash 和结构
python tools/second_brain.py validate-plan plans/pending/<plan_id>.json
python tools/second_brain.py render-plan  plans/pending/<plan_id>.json
```

每条命令单独运行，不要接管道。

常见错误码：
- `PLAN_STALE`（页面变了，重新取 hash）、`SOURCE_STALE`（raw 变了）、`SOURCE_NOT_READY`、`SOURCE_ALREADY_INGESTED`；
- `HUMAN_EDITED_SECTION` / `HUMAN_EDITED_META`（用户改过，先问用户）；
- `BROKEN_LINK`、`PATH_NOT_FOUND`、`PATH_AMBIGUOUS`、`HEADING_LEVEL`、`CONTENT_HEADING`；
- `UNKNOWN_SOURCE`、`UNKNOWN_PAGE`、`PAGE_EXISTS`、`FORBIDDEN_META`、`BAD_TRANSITION`、`TEMPORAL_UNCERTAINTY`、`TEMPORAL_ORDER`；
- `DELETE_FORBIDDEN`、`SOURCE_STILL_CITED`、`DECISION_FROM_RETRACTED`、`SECRET_IN_PLAN`。

# Second Brain — Agent Rules

> rules-version: stage1-v0.3 · 语义规则来自 Stage 0（0A-v0.2.1 / 0B-v1）并按 Stage 0 的发现修订 · 这是正式的 vault
> 基于 NicholasSpisak/second-brain 的 wiki schema，并加入了 decision / 证据分类规则。

你是这个知识库的维护者：读取 `raw/` 中的 source，把值得长期保存的知识整理进 `wiki/`。
**质量标准：宁可漏记，也不能把讨论、猜测或已被推翻的说法写成事实或决定。**

## 1. 目录

```text
raw/        不可变证据。只读，永远不修改、不移动、不重命名。
  documents/  meetings/  sessions/
wiki/       你维护的知识
  sources/    每个 source 一页（事实性摘要 + 证据分类）
  entities/   人、团队、系统、产品、工具
  concepts/   模式、机制、方法、领域知识
  decisions/  每个决定一页，带生命周期状态
  synthesis/  跨 source 的比较或分析（只在确有价值时创建）
  index.md    所有页面的目录
  log.md      只追加的操作日志
plans/
  pending/    你写的 write plan（JSON）
  applied/    已被人批准并执行的 plan（只读）
  rejected/   审阅时被打回的 plan（只读；打回原因会告诉你）
state/      工具维护的 registry、baseline、事务记录（只读，不要打开修改）
tools/second_brain.py   确定性的 plan 校验 / 渲染 / 执行工具
output/     临时产物
```

**你不能直接写 `wiki/`。** 所有 wiki 改动都写成 `plans/pending/<plan_id>.json`，由人审阅 render 结果并批准后，
由工具执行。写法见 §8 和 [PLAN-SCHEMA.md](PLAN-SCHEMA.md)。
下文 §2–§5 中说 "写到某页" "在页面中加警告" "把 status 改为 superseded" 的地方，都是指**在 plan 中用相应的 operation 表达**。

## 2. 证据分类（最重要）

从 source 中提取的每一条信息，写入前都先归入以下一类：

| 类别 | 判定标准 | 可以写到哪里 |
|---|---|---|
| **Decision** | 有权决定的人明确表态（"就用 B"、"That's the decision"），或提议被在场的相关方明确接受 | decision 页（`active`）+ source 页 |
| **Proposed decision** | 文档（ADR、设计稿、提案）中明确写出的结论，但文档没有说明已被谁接受 | decision 页（`proposed`）+ source 页 |
| **Confirmed fact** | 有直接证据：日志、测试、复现、配置、代码，或当事人陈述的现状 | 任何页面 |
| **Documented claim** | **文档**（不是会议或 session 中的口头说法）对现状的描述（代码结构、流程、实现细节），vault 中没有独立证据 | 任何页面，但要写成 "据 [[source-id\|Source 标题]]：…" |
| **Open question / Discussion** | 被提出但没有结论；"可能"、"以后再说"、"需要评估" | 仅 source 页（Open questions）；相关 decision 页的 Open follow-ups |
| **Rejected alternative** | 被提出后被明确否决的方案 | source 页 + 相关 decision 页的 "Alternatives rejected" |
| **Disproven hypothesis** | 排查中提出、后来被证据排除的假设 | **仅** source 页的 "Ruled out" |
| **Unverified claim** | 顺口的推测、估计、"应该是"、"I would expect" | source 页；durable 页中也可以写，但**必须**以 `未验证：` 开头，保留原话的限定语气，并注明出处 |
| **Action item** | 某人承诺去做的事 | 仅 source 页（Action items） |

硬规则：

1. **Discussion 不是 decision。** 带有 "I think / maybe / probably / 可能 / 建议" 且没有被明确接受的内容，不能建 decision 页。
2. **失败的假设不能写成知识。** Ruled-out 内容不能出现在 entity / concept / decision 页的正文里；decision 页的 Alternatives rejected 小节除外。
   Unverified 内容写进 durable 页时，如果没有 `未验证：` 标记，就算作违规。
   注明是谁说的（"据 Speaker 3"）**不能**代替 `未验证：`，也不能去掉原话的限定语；同一说法在不同页面中的确定程度必须一致。
3. **以最终状态为准。** 同一 source 中后来被更正的说法（数值更正、状态更正），只把更正后的版本写入 durable 页面。
4. **状态快照要注明时间。** "目前在进行中"、"尚未上生产" 这类随时间变化的事实，写成 "截至 <source_date>：…"。
5. **限定语保留。** 原文说 "not proven / opaque / would expect"，wiki 中也必须保持同等程度的不确定。
6. **action item 不是 decision**，负责人宣布的意向也不是（除非细节已定且被接受）。

## 3. Decision 生命周期

decision 页的 frontmatter：

```yaml
page_type: decision
status: active          # proposed | active | superseded | revoked
superseded_by: null     # 被推翻时填新 decision 的页面名
decided_on: 2026-09-18  # = 产生该决定的 source 的 source_date
date_confidence: high   # 继承自 source
scope: <一句话说明适用范围>
```

正文小节：`## Decision`、`## Rationale`、`## Alternatives rejected`、`## Open follow-ups`、`## History`。

`proposed`：来自文档的结论，没有证据表明已被接受。页面要写明提出方（如文档作者或文档名），以及"未确认被接受"。
之后如果有 source 明确接受（或否决）它，就把 status 改为 `active`（或 `revoked`），并在 History 中记录。一个 proposed 结论不能被当作 active 决定引用。

**Supersession（后来的 source 推翻了已有的决定）：**

1. 新建一个 decision 页（status: active）。
2. 旧页 **不删除**，除了第 3 条拆分时移走的内容外，**不改写原文**：用 `decision_change` 把 status 改为 superseded、填 superseded_by；工具会在 History 小节追加记录。
3. **部分推翻**：如果旧决定只有一部分被推翻，**先拆分再改 status**：把仍然有效的部分移到新的 active 页（注明从哪一页拆出），
   旧页只保留被推翻的部分，再标为 superseded。一页只能有一种状态；不能把仍然有效的内容留在 superseded 页的正文里。
   **自检（Stage 0 中出过错）：在 plan 之前，逐条列出旧决定正文中的每个组成部分（目标、范围、做法、边界……），分别标注 "被推翻" 或 "仍然有效"。**
   任何 "仍然有效" 的部分，都必须出现在某个 active decision 中。
4. **时间校验**：先比较两个 source 的 source_date。如果任一方 date_confidence 为 low，或者无法确定谁先谁后：
   **不要修改旧页的 status。** 在两页都加上 `> ⚠ TEMPORAL UNCERTAINTY: 可能被 [[x|X]] 推翻，待确认`，并在最终报告中向用户提问。
   工具会拒绝没有 `temporal_override` 的这类推翻；只有用户明确确认了先后顺序，才能加 `temporal_override`。
5. 不能让较早的 source 推翻较新的决定。

## 4. Source 页

每个 source 一页，frontmatter：

```yaml
page_type: source
source_id: <由 discover 生成，用 second_brain.py source <id> 查看；不要自己编>
source_type: document | meeting | session
raw_path: raw/meetings/<原文件名>
source_date: 2026-09-18 | unknown
date_confidence: high | medium | low
date_basis: <日期从哪里来：元数据 / 文件名 / 正文推断>
synthetic: false   # 只有当 source 自己表明是合成或测试材料时才为 true
tags: [...]
created: <今天>
updated: <今天>
```

日期规则：结构化元数据 → high；文件名中的日期 → high/medium；正文中能明确识别的日期 → medium；只能靠推断 → low，写 `unknown` 或推断值并注明依据。
discover 已经按前三级（结构化元数据、文件名、标题区中唯一的日期）确定了日期（见 `source <id>`）。你可以根据正文把 `unknown` 细化为具体日期（medium / low，并写明依据），但不要降低 discover 给出的置信度。
如果 source_date 晚于 ingest 当天，在页面和最终报告中标出 `FUTURE DATE`。

正文小节，只保留有内容的：`## Summary`、`## Decisions`、`## Confirmed facts`、`## Open questions`、`## Rejected alternatives`、`## Ruled out`、`## Unverified claims`、`## Action items`、`## Pages touched`。

## 5. Entity / Concept 页

- frontmatter：`page_type`、`tags`、`sources`（source_id 列表）、`aliases`（可选）、`created`、`updated`。
- 每条事实后注明来源：`（[[source-id|Source 标题]]）`。
- **优先更新已有页面，不新建。** 创建前先查 `wiki/index.md` 和同类目录，包括同义词和缩写。
- 粒度：一个主题只有在有实质内容（多条事实，或多个 source 引用）时才单独建页；否则写在上级页面中。不要为每个列表项建一页。
- 转写或拼写变体（例如 Otter 把同一人名或系统名转写成多种写法）：归到同一页，列在 `aliases` 中。无法确定是否同一对象时，在页面中注明不确定，不要猜。
- 不要把 "Speaker N" 对应到具体人名，除非 source 本身给出了对应关系。
- 新 source 与已有内容冲突时：更新页面，并写明冲突，两边都要引用 source。

## 6. 格式

- 文件名 = page_id（kebab-case）；页面标题（H1）为 Title Case。
- **链接一律写成 `[[page_id|显示标题]]`**（也可以是 `[[page_id#Heading|文字]]`）。目标必须是已存在或本 plan 中创建的 page_id，否则工具报 `BROKEN_LINK`。
- 正文用中文书写；专有名词、代码标识符、配置值和原话引用保留原文。
- `wiki/index.md` 和 `wiki/log.md` 由工具自动生成，**不要**在 plan 中处理它们。
- frontmatter 中的 `page_id`、`page_type`、`sources`、`created`、`updated`、decision 的 `status` 等字段由工具维护；你只通过 operation 的字段表达它们（见 PLAN-SCHEMA.md）。

## 7. 禁止

- 修改 `raw/`。
- 用 Write / Edit 修改 `wiki/` 下的任何文件，或 `plans/applied/`。
- 运行 `apply-plan`、`reject-plan`、`rollback`、`discover`、`maintain --fix`、`unlock`。这些只能由人运行。
- 除了 `python tools/second_brain.py status|source|trace|hash|validate-plan|render-plan` 以外，运行任何 shell 命令；
  不要给命令接管道或串联其他命令；不要用 shell 读写文件。只用 Read / Grep / Glob 读取，只用 Write 写 plan 文件。
- 在没有用户明确同意的情况下使用 `human_override` 或 `temporal_override`。
- 读取或 ingest `status` 不是 `new` / `changed` 的 source（`blocked_secret` 可能含有密钥，不要打开；`blocked_unscannable` 是无法扫描的文件（PDF、DOCX 等没有 `.txt` 伴随文件，或者文本不是 UTF-8），请用户补上文本）。
- 读取 vault 以外的文件，或使用 raw/ 以外的信息补充 wiki 内容。
- 编造 source 中没有的事实、日期或人物身份。

## 8. 写入方式

- 每个 source 写**一个** plan，这个 plan 包含本次 ingest 的全部改动（source 页、新页、对已有页面的修改、decision 状态变化）。
  plan 要么整体生效，要么整体不生效。
- 开始前运行 `status`，只处理用户指定的、状态为 `new` 或 `changed` 的 source。运行 `source <id>`，把 `content_hash` 写进 plan 的 `source_versions`。
  `changed` 的 source 要完整重新 ingest：必须实际修改它的 source 页正文（只改 tags、只改其他页面都不算，工具会警告 `SOURCE STILL CHANGED`）；新建 source 页时，它的 source_id 也必须列在 plan 的 `source_ids` 中，而且这个 create_page 要排在所有引用它的 operation 之前。
- 修改已有页面前，先运行 `hash <page_id> --sections`，取得 `expected_hash` 和 section 结构。输出中 `baseline: DIFFERS` 或 `none` 表示用户改过这一页，修改相应 section 前要先问用户（见下一条）。
- 写完 plan 后，运行 `validate-plan`，直到输出 `OK`；再运行 `render-plan`，检查 diff 是否就是你想要的改动。
- 如果 validate 报 `HUMAN_EDITED_SECTION` / `HUMAN_EDITED_META`：说明用户在 Obsidian 中改过这一页。**不要绕过。** 把报错中的 baseline → current diff 转述给用户，问是否允许覆盖；
  得到同意后才能在该 operation 上加 `human_override`，否则换一种不覆盖人工内容的写法（例如 `add_section`）。
- 如果需要等用户回答（例如 TEMPORAL UNCERTAINTY），先提交不含该改动的 plan，得到回答后再写一个新的 plan。
- plan 被打回（移到 `plans/rejected/`）时，按打回原因重写一个新的 plan，不要修改被打回的文件。
- 删除和撤回（`delete_page`、`retract_source`）只在用户要求时使用。撤回前先运行 `trace <source_id>`，同一个 plan 要清理所有引用。
- 可用的 operation 和字段见 [PLAN-SCHEMA.md](PLAN-SCHEMA.md)。

Ingest 的具体步骤见 [INGEST.md](INGEST.md)。

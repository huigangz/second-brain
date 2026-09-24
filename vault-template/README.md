# Second Brain — Daily use / 日常使用

[English](#english) · [中文](#中文)

## English

This is a Second Brain vault. Every change to the wiki goes through **plan → human review → apply**, and every
apply is a transaction you can roll back.
Agent rules: `AGENTS.md`, `INGEST.md`; plan format: `PLAN-SCHEMA.md`. The tools and rule files are managed by
the second-brain project's `install` / `upgrade`; do not edit them inside the vault.

### Adding sources

```powershell
# 1. Put the file into raw/ (documents / meetings / sessions), then:
python tools/second_brain.py discover        # registers it and scans for secrets; status: new
python tools/second_brain.py status
```

- PDF: also add a text version with the same name plus `.txt` (`x.pdf.txt`). That is what the agent reads.
- `BLOCKED`: the file may contain a secret. Clean the raw file, then run discover again.
- `POSSIBLE MOVED+MODIFIED`: if it really is the same source, run `discover --link <new path> <source_id>`.

### Ingest (one plan per source)

1. Start Claude Code in this directory and say `ingest <source_id>`. With several sources, go one at a time,
   oldest first.
2. The agent writes `plans/pending/<plan_id>.json`, runs validate and render itself, and reports back.
3. **You review it:**
   ```powershell
   python tools/second_brain.py render-plan plans/pending/<plan_id>.json
   ```
   Look at the `PLAN SHA256` on the first line, the flags of each operation, and the diff. Check in particular:
   - whether the status of each new decision (proposed / active) is right;
   - whether discussion or speculation has been written down as fact;
   - the flags `HUMAN-MAINTAINED PAGE`, `TEMPORAL OVERRIDE`, `HUMAN EDIT OVERRIDE`, `SYNTHETIC SOURCE`,
     `FUTURE DATE`;
   - for a partial reversal, whether the parts that still hold were split out onto an active page.
4. To approve:
   ```powershell
   python tools/second_brain.py apply-plan plans/pending/<plan_id>.json --approve <PLAN SHA256>
   ```
   To reject:
   ```powershell
   python tools/second_brain.py reject-plan plans/pending/<plan_id>.json --reason "…"
   ```
   Then tell the agent why, and let it write a new plan.
5. Tell the agent "the plan was approved and applied", then move on to the next source.

### Editing by hand in Obsidian

You can edit pages directly. The tool detects your edits and protects them:
- When the AI later wants to change the same section, it is stopped (`HUMAN_EDITED_SECTION`) and the agent
  should ask you first.
- Changes to other sections are not affected.
- The page is permanently marked as human-maintained.
- Run `python tools/second_brain.py maintain` from time to time. It reports unregistered pages, renamed files,
  broken links, manual edits, and source pages that are behind their raw file (after a move or rename).
  With `--fix` it registers new pages, renames changed file names back to the page_id, syncs source pages and
  regenerates the index. When there is nothing to fix, it writes no transaction.

### Deleting and retracting

- To remove some content from a page: have the agent write a plan that updates the section.
- To delete a whole page: have the agent use `delete_page` (decision pages cannot be deleted, only revoked).
- To withdraw everything a source contributed: run `python tools/second_brain.py trace <source_id>` first,
  then have the agent write a cleanup plan with `retract_source`.

### When something goes wrong

```powershell
python tools/second_brain.py status          # incomplete transactions, the lock
python tools/second_brain.py rollback <txn_id>   # restore (txn_id: see the apply output or state/txn/)
# if a rollback stops halfway, status shows an incomplete transaction and every write is blocked;
# run the same rollback again to finish it
python tools/second_brain.py maintain --revert <page_id> ...  # restore pages to their last controlled version
python tools/second_brain.py unlock --force  # only when you are sure no other command is running
```

If a page was changed without a plan (for example, Codex writing where it should not):
1. `maintain` lists every page that differs from its baseline: `HUMAN_EDITED <page_id>`,
   `INDEX_OUT_OF_DATE`, `LOG_EDITED`, and new unregistered pages.
   If it reports `DUPLICATE_PAGE_ID` (two files claim the same page_id), `--fix` changes nothing: keep the
   right file, or run `--revert` for that page_id.
2. If the change was not yours, run `maintain --revert <page_id> ...` (several pages at once are fine):
   - a registered page goes back to its content after the last apply (the baseline `maintain` compares
     against); a deleted page is restored, and a renamed one is moved back;
   - an unregistered page is removed;
   - `index` is regenerated, and `log` goes back to what the last transaction wrote.
3. The revert is itself a transaction: the last line of its output gives the txn_id, and
   `rollback <txn_id>` brings the reverted content back if you want to look at it.

Note: a revert also removes edits you made by hand, so use it only on pages you did not change yourself.
External pages have no controlled version and cannot be reverted.

### Obsidian settings

Settings → Files and links → Excluded files: add `state/`, `plans/`, `tools/`, `output/`.

### Supported agents

One set of rules (`AGENTS.md`, `INGEST.md`, `PLAN-SCHEMA.md`) and one guard script (`tools/agent_guard.py`)
serve every agent. The guard is a PreToolUse hook; all four agents treat its exit code 2 as "refuse this call":

- files can be written only to `plans/pending/*.json`;
- shell commands must be read-only, or one of the 6 agent commands of `second_brain.py` (status, source,
  trace, hash, validate-plan, render-plan).

| Agent | Rules entry point | Ingest skill | Enforcement |
|---|---|---|---|
| Claude Code | `CLAUDE.md` → `@AGENTS.md` | `.claude/skills/second-brain-ingest` | `.claude/settings.json`: allow / deny permissions plus the guard hook |
| GitHub Copilot (VS Code agent, Copilot CLI) | `AGENTS.md` (`.vscode/settings.json` turns on `chat.useAgentsMdFile`) plus `.github/copilot-instructions.md` | `.agents/skills/…` (Copilot also reads `.claude/skills`, so it may show two identical skills with the same name) | `.github/hooks/second-brain-guard.json`; in VS Code the human-only commands always need confirmation |
| Codex | `AGENTS.md` | `.agents/skills/second-brain-ingest` | `.codex/config.toml`: read-only sandbox, writing a plan needs your approval; `.codex/hooks.json`: the guard; `.codex/rules/`: human-only commands forbidden |

Notes:
- **Claude Code**: before first use, start Claude Code **interactively** in the vault directory once and accept
  the trust dialog. Otherwise the project's allow rules are ignored and the agent cannot even write a plan.
- **Codex (smoke test, 2026-09-23)**: in the test environment (Windows 11), Codex's `apply_patch` **was not
  held back by the read-only sandbox, and the hook could not stop it** (openai/codex#27833), so **Codex can
  write to the wiki directly**. Only the written rules in AGENTS.md hold it back, and its own report of what it
  did may not match what happened.
  Recommendation: **prefer Claude Code for ingest**. If you use Codex:
  - run `maintain` after each session. A page reported as `HUMAN_EDITED` that you did not change was written
    by Codex; restore it with `maintain --revert <page_id>` (see "When something goes wrong");
  - if your user-level `~/.codex/config.toml` sets `approvals_reviewer = "guardian_subagent"`, change it to
    `"user"`. Otherwise approval requests go to an AI reviewer instead of you.
- **Copilot**: the workspace hook is subject to VS Code Workspace Trust.
- Whatever the agent, the final guarantee is the same: a plan is applied only after you review it and run it
  with `--approve <sha256>`. Running `maintain` after each session shows whether anything changed the wiki
  without a plan.
- These agent configurations follow each vendor's documentation. The first time you use an agent, run a quick
  smoke test:
  1. ask it to edit `wiki/index.md` directly: this must be refused;
  2. ask it to run `apply-plan`: this must be refused;
  3. ask it to write `plans/pending/test.json`: this must be allowed (in Codex, it asks for your approval).

---

## 中文

这是一个 Second Brain vault。所有 wiki 改动都走 **plan → 人工审阅 → apply**，每次 apply 都是一个可回滚的事务。
Agent 规则：`AGENTS.md`、`INGEST.md`；plan 格式：`PLAN-SCHEMA.md`。工具和规则文件由 second-brain 项目的 `install` / `upgrade` 管理，不要在 vault 中直接修改它们。

### 加入新资料

```powershell
# 1. 放进 raw/（documents / meetings / sessions），然后：
python tools/second_brain.py discover        # 登记，扫描 secret；status 为 new
python tools/second_brain.py status
```

- PDF：同时放一个同名的 `.pdf.txt` 文本版，Agent 读的是它。
- `BLOCKED`：文件可能含有密钥。清理 raw 文件后，再运行一次 discover。
- `POSSIBLE MOVED+MODIFIED`：如果确实是同一份资料，运行 `discover --link <新路径> <source_id>`。

### Ingest（每份资料一个 plan）

1. 在本目录启动 Claude Code，说：`ingest <source_id>`（多份资料时按日期从早到晚，一份一份来）。
2. Agent 写 `plans/pending/<plan_id>.json`，自己运行 validate 和 render，然后报告。
3. **你来审阅：**
   ```powershell
   python tools/second_brain.py render-plan plans/pending/<plan_id>.json
   ```
   先看第一行的 `PLAN SHA256`、各个 operation 的 flags，以及 diff，重点检查：
   - 新 decision 的状态（proposed / active）是否合理；
   - 有没有把讨论或猜测写成事实；
   - `HUMAN-MAINTAINED PAGE`、`TEMPORAL OVERRIDE`、`HUMAN EDIT OVERRIDE`、`SYNTHETIC SOURCE`、`FUTURE DATE` 这些 flag；
   - 部分推翻时，仍然有效的内容是否拆到了 active 页。
4. 批准：
   ```powershell
   python tools/second_brain.py apply-plan plans/pending/<plan_id>.json --approve <PLAN SHA256>
   ```
   不批准：
   ```powershell
   python tools/second_brain.py reject-plan plans/pending/<plan_id>.json --reason "…"
   ```
   然后把原因告诉 Agent，让它重写。
5. 告诉 Agent "plan 已批准并执行"，再继续下一份。

### 在 Obsidian 中手工修改

可以直接改。工具会检测到修改，并保护你改过的内容：
- AI 之后要改同一个 section 时，会被拦下（`HUMAN_EDITED_SECTION`），Agent 应该先问你；
- 修改其他 section 不受影响。
- 页面会被永久标记为 human-maintained。
- 定期运行 `python tools/second_brain.py maintain` 检查：未登记页面、改名、断链、人工修改、source 页落后于 raw（移动或改名之后）。加 `--fix` 会登记新页面、把改过的文件名改回 page_id、同步 source 页、重新生成 index；没有要改的内容时不会生成事务。

### 删除与撤回

- 删掉页面中的某些内容：让 Agent 写一个修改 section 的 plan。
- 删整页：让 Agent 用 `delete_page`（decision 页不能删，只能 revoke）。
- 撤回一份资料的全部贡献：先运行 `python tools/second_brain.py trace <source_id>`，再让 Agent 用 `retract_source` 写一个清理 plan。

### 出问题时

```powershell
python tools/second_brain.py status          # 未完成的事务、锁
python tools/second_brain.py rollback <txn_id>   # 恢复（txn_id 见 apply 的输出或 state/txn/）
# rollback 如果中途失败，status 会显示未完成的事务，所有写操作都会被阻止；再运行一次同一条 rollback 即可继续完成
python tools/second_brain.py maintain --revert <page_id> ...  # 页面恢复到最后一次受控写入后的版本
python tools/second_brain.py unlock --force  # 只在确认没有其他命令在运行时使用
```

有页面被绕过 plan 改了（例如 Codex 越权写入）：
1. `maintain` 列出所有和基准不同的页面：`HUMAN_EDITED <page_id>`、`INDEX_OUT_OF_DATE`、`LOG_EDITED`，以及未登记的新页面。
   如果出现 `DUPLICATE_PAGE_ID`（两个文件声明同一个 page_id），`--fix` 不会做任何修改：留下正确的那个文件，或者对该 page_id 运行 `--revert`。
2. 不是你改的，就运行 `maintain --revert <page_id> ...`，可一次恢复多个页面：
   - 已登记的页面恢复成最后一次 apply 之后的内容（也就是 `maintain` 的比较基准）；被删除的页面会补回，被改名的页面会移回原来的路径；
   - 未登记的页面会被删除；
   - `index` 会重新生成，`log` 恢复到最后一次事务写入后的内容。
3. 恢复本身也是一个事务：输出最后一行给出 txn_id，`rollback <txn_id>` 可以把被恢复掉的内容拿回来查看。

注意：你自己手工改过的内容也会被恢复掉，所以只对不是你改的页面使用。external 页面没有受控版本，不能恢复。

### Obsidian 设置

Settings → Files and links → Excluded files：加入 `state/`、`plans/`、`tools/`、`output/`。

### 支持的 Agent

同一套规则（`AGENTS.md`、`INGEST.md`、`PLAN-SCHEMA.md`）和同一个 guard 脚本（`tools/agent_guard.py`）服务所有 agent。
guard 是一个 PreToolUse hook，四种 agent 都把它的 exit code 2 视为 "拒绝这次调用"：

- 文件只能写到 `plans/pending/*.json`；
- shell 命令只能是只读命令，或者 `second_brain.py` 的 6 个 agent 命令（status、source、trace、hash、validate-plan、render-plan）。

| Agent | 规则入口 | ingest skill | 强制执行 |
|---|---|---|---|
| Claude Code | `CLAUDE.md` → `@AGENTS.md` | `.claude/skills/second-brain-ingest` | `.claude/settings.json`：allow / deny 权限 + guard hook |
| GitHub Copilot（VS Code agent、Copilot CLI） | `AGENTS.md`（`.vscode/settings.json` 开启 `chat.useAgentsMdFile`）+ `.github/copilot-instructions.md` | `.agents/skills/…`（Copilot 也读 `.claude/skills`，所以可能看到两份同名 skill，内容相同） | `.github/hooks/second-brain-guard.json`；VS Code 的人工专用命令始终需要确认 |
| Codex | `AGENTS.md` | `.agents/skills/second-brain-ingest` | `.codex/config.toml`：只读 sandbox，写 plan 需要你批准；`.codex/hooks.json`：guard；`.codex/rules/`：禁止人工专用命令 |

注意：
- **Claude Code**：第一次使用前，在 vault 目录**交互式地**启动一次 Claude Code，并接受 trust 对话框；否则项目的 allow 规则会被忽略，Agent 连 plan 也写不了。
- **Codex（2026-09-23 冒烟测试结果）**：在测试环境（Windows 11）中，Codex 的 `apply_patch` **不受 read-only sandbox 限制，hook 也拦不住**（openai/codex#27833），**Codex 可以直接写 wiki**。它只受 AGENTS.md 的文字约束，而且它的自我报告可能与实际不符。
  建议：**ingest 优先用 Claude Code**。如果用 Codex：
  - 每次 session 结束后运行 `maintain`，自己没改过却出现 `HUMAN_EDITED` 的页面，就是 Codex 越权写入，用 `maintain --revert <page_id>` 一键恢复（见"出问题时"）；
  - 如果用户级 `~/.codex/config.toml` 设置了 `approvals_reviewer = "guardian_subagent"`，改为 `"user"`，否则审批请求会交给 AI 审核，而不是你。
- **Copilot**：workspace hook 受 VS Code Workspace Trust 约束。
- 不论用哪个 agent，最后的保证都一样：plan 必须由你审阅并用 `--approve <sha256>` 执行。每次 session 结束后运行一次 `maintain`，可以发现有没有绕过 plan 直接改 wiki 的情况。
- 这些 agent 配置是按各家官方文档写的，第一次使用某个 agent 时建议做一次冒烟测试：
  1. 让它直接编辑 `wiki/index.md`，应该被拒绝；
  2. 让它运行 `apply-plan`，应该被拒绝；
  3. 让它写 `plans/pending/test.json`，应该被允许，或者在 Codex 中请求你批准。

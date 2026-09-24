# Second Brain — 日常使用

这是一个 Second Brain vault。所有 wiki 改动都走 **plan → 人工审阅 → apply**，每次 apply 都是一个可回滚的事务。
Agent 规则：`AGENTS.md`、`INGEST.md`；plan 格式：`PLAN-SCHEMA.md`。工具和规则文件由 second-brain 项目的 `install` / `upgrade` 管理，不要在 vault 中直接修改它们。

## 加入新资料

```powershell
# 1. 放进 raw/（documents / meetings / sessions），然后：
python tools/second_brain.py discover        # 登记，扫描 secret；status 为 new
python tools/second_brain.py status
```

- PDF：同时放一个同名的 `.pdf.txt` 文本版，Agent 读的是它。
- `BLOCKED`：文件可能含有密钥。清理 raw 文件后，再运行一次 discover。
- `POSSIBLE MOVED+MODIFIED`：如果确实是同一份资料，运行 `discover --link <新路径> <source_id>`。

## Ingest（每份资料一个 plan）

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

## 在 Obsidian 中手工修改

可以直接改。工具会检测到修改，并保护你改过的内容：
- AI 之后要改同一个 section 时，会被拦下（`HUMAN_EDITED_SECTION`），Agent 应该先问你；
- 修改其他 section 不受影响。
- 页面会被永久标记为 human-maintained。
- 定期运行 `python tools/second_brain.py maintain` 检查：未登记页面、改名、断链、人工修改、source 页落后于 raw（移动或改名之后）。加 `--fix` 会登记新页面、把改过的文件名改回 page_id、同步 source 页、重新生成 index；没有要改的内容时不会生成事务。

## 删除与撤回

- 删掉页面中的某些内容：让 Agent 写一个修改 section 的 plan。
- 删整页：让 Agent 用 `delete_page`（decision 页不能删，只能 revoke）。
- 撤回一份资料的全部贡献：先运行 `python tools/second_brain.py trace <source_id>`，再让 Agent 用 `retract_source` 写一个清理 plan。

## 出问题时

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

## Obsidian 设置

Settings → Files and links → Excluded files：加入 `state/`、`plans/`、`tools/`、`output/`。

## 支持的 Agent

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
  - 每次 session 结束后运行 `maintain`，自己没改过却出现 `HUMAN_EDITED` 的页面，就是 Codex 越权写入，用 `maintain --revert <page_id>` 一键恢复（见“出问题时”）；
  - 如果用户级 `~/.codex/config.toml` 设置了 `approvals_reviewer = "guardian_subagent"`，改为 `"user"`，否则审批请求会交给 AI 审核，而不是你。
- **Copilot**：workspace hook 受 VS Code Workspace Trust 约束。
- 不论用哪个 agent，最后的保证都一样：plan 必须由你审阅并用 `--approve <sha256>` 执行。每次 session 结束后运行一次 `maintain`，可以发现有没有绕过 plan 直接改 wiki 的情况。
- 这些 agent 配置是按各家官方文档写的，第一次使用某个 agent 时建议做一次冒烟测试：
  1. 让它直接编辑 `wiki/index.md`，应该被拒绝；
  2. 让它运行 `apply-plan`，应该被拒绝；
  3. 让它写 `plans/pending/test.json`，应该被允许，或者在 Codex 中请求你批准。

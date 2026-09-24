# Ingest Procedure

> rules-version: stage1-v0.3 · 适用于 [AGENTS.md](AGENTS.md)（写入方式见 AGENTS.md §8 和 PLAN-SCHEMA.md）

用户会说 "ingest <source_id>" 或 "ingest raw/<path>"。先运行 `status` 和 `source <id>`：只处理状态为 `new` / `changed` 的 source。**每次只处理一个 source。** 即使 raw/ 中还有其他未处理文件，也不要顺手处理。

## 步骤

1. **完整阅读 source。** 长文件要分段读完，不能只读开头。PDF 如果有同名的 `.txt` 伴随文件，读伴随文件。
2. **读取现有 wiki。** 先读 `wiki/index.md`，再读与本 source 主题相关的已有页面（同一系统、同一项目、同一决定）。
3. **提取与分类（先在内部完成，不写文件）。** 按 AGENTS.md §2 把内容逐条归类。特别检查：
   - 每一条候选 decision：谁决定的？是否被明确接受？有没有 hedge 语气？
   - 每一条候选 fact：后文有没有被更正或推翻？
   - 排查类 session：哪些假设被排除了？最终确认的根因是什么？
4. **确定日期**：以 `source <id>` 的结果为起点，按 AGENTS.md §4 的规则，写明 date_confidence 和 date_basis。
5. **对要修改的已有页面运行 `hash <page_id> --sections`**，确认 section 结构和 expected_hash。把 `source <id>` 的 content_hash 写进 `source_versions`。
6. **写一个 plan**（`plans/pending/<plan_id>.json`）：用 `create_page` 建 source 页，并用 operation 表达对 decision / entity / concept 页的全部创建和更新。能更新就不新建。
7. **Supersession 检查。** 本 source 是否推翻、修改或撤销了 `wiki/decisions/` 中已有的决定？是否接受或否决了某个 `proposed` 决定？如果是，按 AGENTS.md §3 处理，包括时间校验。
8. **补 wikilinks**（写进 plan 的 content 中）。index 和 log 由工具生成，不要处理。然后运行 `validate-plan` 直到 `OK`，再运行 `render-plan` 检查 diff。
9. **自检（对照 render 的 diff 逐页检查本次改动的 durable 页面）：**
   - 没有 ruled-out 假设；
   - 所有 unverified 内容都以 `未验证：` 开头；
   - 被更正的数值或状态只保留最终版本；
   - 没有 proposed 结论被写成 active 决定；
   - 没有 superseded 页正文中仍然保留着有效内容（逐条列出旧决定的组成部分核对，AGENTS.md §3 第 3 条）；
   - 所有链接都是 `[[page_id|标题]]` 形式。
10. **最终报告**，交给用户（**不要**执行 plan）：
   - plan 文件路径和 `render-plan` 输出第一行的 PLAN SHA256
   - 用了哪些 operation；如果某个改动因为 operation 的限制无法按理想方式表达，说明理想做法是什么、实际怎么做的
   - 3–5 条关键 takeaways
   - 创建的页面 / 更新的页面（每页一句话说明改了什么）
   - 被分类为 open question / rejected / ruled out / unverified 的条目（各列简短清单）
   - Supersession 和时间不确定的情况（如果有，**明确提问**）
   - 你没把握的分类判断
   - 如果 validate 报了 `HUMAN_EDITED_*`：转述 diff，并询问用户是否允许覆盖

Pilot 模式下，**不需要在写 plan 之前等用户确认 takeaways**，直接写好 plan、validate、render 后再报告。这样才能评估 Agent 自己的判断。
plan 被批准并执行后，用户会告诉你；下一个 source 的 hash 要在执行之后重新获取。

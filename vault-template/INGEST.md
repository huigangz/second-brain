# Ingest Procedure

> rules-version: stage1-v0.4 (English translation of stage1-v0.3) · applies with [AGENTS.md](AGENTS.md) (how to
> write: AGENTS.md §8 and PLAN-SCHEMA.md)

The user says "ingest <source_id>" or "ingest raw/<path>". First run `status` and `source <id>`: work only on a
source whose status is `new` / `changed`. **Handle one source at a time.** Even if raw/ holds other unprocessed
files, do not take them on along the way.

## Steps

1. **Read the whole source.** Read long files to the end, in parts if needed, not just the beginning. For a PDF
   with a `.txt` companion of the same name, read the companion.
2. **Read the existing wiki.** Read `wiki/index.md` first, then the existing pages related to this source's topics
   (the same system, the same project, the same decision).
3. **Extract and classify (internally first; write no files).** Classify every item following AGENTS.md §2. Check
   in particular:
   - every candidate decision: who decided? Was it explicitly accepted? Is it hedged?
   - every candidate fact: is it corrected or overturned later in the source?
   - troubleshooting sessions: which hypotheses were ruled out? What was the confirmed root cause?
4. **Set the date**: start from the output of `source <id>`, follow the rules of AGENTS.md §4, and state
   date_confidence and date_basis.
5. **Run `hash <page_id> --sections` on every existing page you will change**, to confirm its section structure
   and expected_hash. Put the content_hash from `source <id>` into `source_versions`.
6. **Write one plan** (`plans/pending/<plan_id>.json`): create the source page with `create_page`, and express
   every creation and update of decision / entity / concept pages as operations. Update rather than create
   whenever you can.
7. **Supersession check.** Does this source overturn, change or revoke an existing decision in `wiki/decisions/`?
   Does it accept or reject a `proposed` decision? If so, follow AGENTS.md §3, including the temporal check.
8. **Add wikilinks** (in the plan's content). The tool generates index and log; do not touch them. Then run
   `validate-plan` until it prints `OK`, and `render-plan` to check the diff.
9. **Self-check (go through each durable page changed by this plan against the rendered diff):**
   - no ruled-out hypotheses;
   - every piece of unverified content starts with the unverified marker (`未验证：`, AGENTS.md §6);
   - corrected numbers or statuses appear only in their final version;
   - no proposed conclusion is written as an active decision;
   - no superseded page still holds content that is still valid in its body (check each component of the old
     decision one by one, AGENTS.md §3 rule 3);
   - every link has the form `[[page_id|title]]`.
10. **Final report** to the user (do **not** apply the plan):
    - the plan file path, and the PLAN SHA256 from the first line of the `render-plan` output
    - which operations you used; if an operation's limits kept a change from being expressed the ideal way, say
      what the ideal would have been and what you did instead
    - 3–5 key takeaways
    - pages created / pages updated (one sentence per page on what changed)
    - the items classified as open question / rejected / ruled out / unverified (a short list for each)
    - supersessions and temporal uncertainties (if any, **ask explicitly**)
    - classification calls you are unsure about
    - if validate reported `HUMAN_EDITED_*`: relay the diff and ask the user whether it may be overwritten

In pilot mode, **do not wait for the user to confirm the takeaways before writing the plan**: write the plan,
validate and render it, then report. That is how the agent's own judgement can be evaluated.
After the plan is approved and applied, the user will tell you; get fresh hashes for the next source after that.

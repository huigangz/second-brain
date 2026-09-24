"""PreToolUse guard shared by Claude Code, GitHub Copilot (VS Code / CLI) and Codex.

Every agent calls this script before a tool runs and passes a JSON payload on stdin.
Exit code 0 allows the call; exit code 2 denies it (all four agents treat 2 as deny), with the
reason on stderr. Policy (vault AGENTS.md §7):

- file writes are allowed only for  plans/pending/*.json  inside the vault;
- shell commands are allowed only if every pipeline segment is a read-only command, or one of
  `python tools/second_brain.py status|source|trace|hash|validate-plan|render-plan`;
- everything else that edits files or runs commands is denied. Read/search tools are allowed.

This is defense in depth. The approval binding (--approve <sha256>) and the checks in
second_brain.py hold regardless of which agent wrote the plan.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

AGENT_COMMANDS = {"status", "source", "trace", "hash", "validate-plan", "render-plan"}
# No command here may run code or write files; the options that would are denied in _segment_ok.
# awk / foreach-object / less / more were removed after review (2026-09-23): each can write or execute.
READ_ONLY = {
    "cat", "type", "get-content", "gc", "head", "tail", "wc", "nl",
    "rg", "grep", "egrep", "fgrep", "findstr", "select-string", "sls",
    "ls", "dir", "get-childitem", "gci", "tree", "pwd", "get-location", "cd", "set-location",
    "sort", "uniq", "cut", "tr", "column", "jq", "diff", "cmp", "file", "stat", "realpath", "basename", "dirname",
    "measure-object", "select-object", "sort-object", "format-table", "format-list", "where-object",
    "sed", "find", "echo", "printf", "true", "test-path",
}
SED_PRINT = re.compile(r"^(?:(?:\d+|\$|/[^/]*/)(?:,(?:\d+|\$|/[^/]*/))?p;?)+$")  # sed -n '1,80p' / '/a/,/b/p'
DENIED_OPTIONS = {  # command → option prefixes that write files or run programs
    "find": ("-exec", "-ok", "-fprint", "-fls", "-delete"),
    "sort": ("-o", "/o", "--compress-program"),  # /O: Windows sort.exe output file
    "tree": ("-o",),
    "rg": ("--pre",),
    "file": ("-c", "--compile"),
    "diff": ("-o",),
}
ANY_COMMAND_DENIED = ("--output",)  # GNU long option that writes a file (sort, diff, ...)
SHELL_WRAPPERS = {"bash", "sh", "zsh", "pwsh", "powershell", "powershell.exe", "pwsh.exe", "cmd", "cmd.exe"}
WRITE_TOOLS = re.compile(r"(write|edit|create|patch|replace|insert|delete|rename|move|notebook)", re.I)
SHELL_TOOLS = re.compile(r"(bash|shell|terminal|powershell|command|exec)", re.I)


def deny(reason: str) -> None:
    print(f"[second-brain guard] denied: {reason}", file=sys.stderr)
    sys.exit(2)


def _vault(payload: dict) -> Path:
    for key in ("cwd", "workspace", "project_dir"):
        if isinstance(payload.get(key), str) and payload[key]:
            return Path(payload[key]).resolve()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).resolve() if env else Path.cwd().resolve()


def _normalise(payload: dict) -> tuple[str, dict]:
    """Return (tool_name, tool_input) across the Claude / VS Code / Copilot CLI / Codex payload shapes."""
    name = payload.get("tool_name") or payload.get("toolName") or payload.get("tool") or ""
    args = payload.get("tool_input") or payload.get("toolArgs") or payload.get("input") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"command": args}
    return str(name), args if isinstance(args, dict) else {"value": args}


# --- file writes -------------------------------------------------------------------------

PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)
PATH_KEYS = ("file_path", "filePath", "path", "notebook_path", "target", "destination", "new_path", "old_path")


def _write_targets(args: dict) -> list[str]:
    targets = [args[k] for k in PATH_KEYS if isinstance(args.get(k), str)]
    for key in ("files", "paths", "filePaths"):
        if isinstance(args.get(key), list):
            targets += [t if isinstance(t, str) else t.get("path", "") for t in args[key]]
    for key in ("patch", "input", "command", "diff"):
        value = args.get(key)
        text = " ".join(value) if isinstance(value, list) else value
        if isinstance(text, str):
            targets += [a or b for a, b in PATCH_FILE.findall(text)]
    return [t.strip() for t in targets if t and t.strip()]


def _allowed_write(target: str, vault: Path) -> bool:
    path = Path(target)
    path = (path if path.is_absolute() else vault / path).resolve()
    pending = (vault / "plans" / "pending").resolve()
    return path.parent == pending and path.suffix == ".json"


# --- shell commands ----------------------------------------------------------------------

def _command_text(args: dict) -> str | None:
    for key in ("command", "commandLine", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, list):
            return " ".join(shlex.quote(str(v)) for v in value)
        if isinstance(value, str):
            return value
    return None


def _unwrap(tokens: list[str]) -> str | None:
    """`bash -lc "…"`, `powershell -Command "…"`, `cmd /c …` → inner command text."""
    if tokens and Path(tokens[0]).name.lower() in SHELL_WRAPPERS:
        for i, t in enumerate(tokens[1:], 1):
            if t.lower() in ("-c", "-lc", "-command", "/c", "-noprofile", "-nologo", "-noninteractive"):
                if t.lower() in ("-noprofile", "-nologo", "-noninteractive"):
                    continue
                return " ".join(tokens[i + 1:]) if i + 1 < len(tokens) else ""
    return None


def _segment_ok(segment: str) -> str | None:
    """None if the segment is allowed, else a reason."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return None
    inner = _unwrap(tokens)
    if inner is not None:
        return _check_command(inner)
    head = Path(tokens[0]).name.lower()
    if head in ("python", "python3", "py", "python.exe"):
        rest = [t.replace("\\", "/") for t in tokens[1:] if not t.startswith("-3")]
        if len(rest) >= 2 and rest[0].lstrip("./") == "tools/second_brain.py":
            sub = next((t for t in rest[1:] if not t.startswith("--")), "")
            if any(t.startswith("--vault") for t in rest):
                return "second_brain.py --vault is not allowed for the agent"
            return None if sub in AGENT_COMMANDS else f"second_brain.py {sub} is a human-only command"
        return "python is allowed only for tools/second_brain.py " + "|".join(sorted(AGENT_COMMANDS))
    if head not in READ_ONLY:
        return f"{tokens[0]!r} is not a read-only command"
    args = tokens[1:]
    if head == "sed":
        opts = [t for t in args if t.startswith("-")]
        scripts = [t for t in args if not t.startswith("-")][:1]
        if set(opts) - {"-n", "--quiet", "--silent", "-E", "-r"} or "-n" not in opts and "--quiet" not in opts                 and "--silent" not in opts or not scripts or not SED_PRINT.match(scripts[0].replace(" ", "")):
            return "sed is allowed only as  sed -n '<from>,<to>p' <file>"
    for prefix in DENIED_OPTIONS.get(head, ()) + ANY_COMMAND_DENIED:
        if any(t.lower().startswith(prefix) for t in args):
            return f"{head} {prefix} writes files or runs programs"
    if head == "uniq" and len([t for t in args if not t.startswith("-")]) > 1:
        return "uniq with an output file"
    return None


QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
HARMLESS_REDIRECT = re.compile(r"\d?>\s*&\d|\d?>\s*/dev/null|\d?>\s*\$null|\d?>\s*nul\b", re.I)
NON_FILE_TOOLS = {"todowrite", "todo_write", "manage_todo_list", "update_plan", "task", "agent"}


def _check_command(command: str) -> str | None:
    # Windows paths: treat backslashes as separators, not shell escapes
    command = command.replace("\\", "/")
    try:
        inner = _unwrap(shlex.split(command, posix=True))
    except ValueError:
        inner = None
    if inner is not None:  # `bash -lc "…"` / `powershell -Command "…"`: judge the inner command
        return _check_command(inner)
    command = HARMLESS_REDIRECT.sub(" ", command)
    # PowerShell runs code in script blocks, (…) / @(…) expressions and [Type]::Method calls; outside
    # quotes none of these belong in a read-only command (inside quotes they are literal text, e.g. rg "f\(").
    unquoted = QUOTED.sub(" ", command)
    if re.search(r"[{}()]|::", unquoted):
        return "script blocks, (...) expressions and [Type]::Method calls are not allowed; use the Read/Grep tools"
    if re.search(r"(?<![<>=!-])>(?!=)|>>|\$\(|`|\b(?:tee|out-file|set-content|add-content|new-item|remove-item|"
                 r"move-item|copy-item|rename-item|invoke-expression|iex|start-process)\b", command, re.I):
        return "output redirection, command substitution or file-writing cmdlets are not allowed"
    for segment in re.split(r"\|\||&&|[|;&\n]", command):
        reason = _segment_ok(segment.strip())
        if reason:
            return reason
    return None


def decide(payload: dict) -> str | None:
    """None = allow, otherwise the deny reason."""
    name, args = _normalise(payload)
    if name.lower() in NON_FILE_TOOLS:
        return None
    vault = _vault(payload)
    command = _command_text(args)
    targets = _write_targets(args)
    is_patch = name.lower() in ("apply_patch", "applypatch") or bool(targets and command and "*** " in command)
    if is_patch or (WRITE_TOOLS.search(name) and not SHELL_TOOLS.search(name)):
        if not targets:
            return f"{name}: cannot determine the target file; writes are limited to plans/pending/*.json"
        bad = [t for t in targets if not _allowed_write(t, vault)]
        return f"writes are limited to plans/pending/*.json (got: {', '.join(bad)})" if bad else None
    if command is not None and (SHELL_TOOLS.search(name) or not name):
        return _check_command(command)
    return None


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        deny("unreadable hook payload")
    reason = decide(payload if isinstance(payload, dict) else {})
    if reason:
        deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""tools/agent_guard.py: one PreToolUse policy for Claude Code, Copilot (VS Code / CLI) and Codex payloads."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import agent_guard as g  # noqa: E402

VAULT = "C:/vault" if sys.platform == "win32" else "/vault"


def claude(tool, **inp):
    return {"session_id": "s", "cwd": VAULT, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp}


def copilot_cli(tool, **args):
    return {"sessionId": "s", "cwd": VAULT, "toolName": tool, "toolArgs": json.dumps(args)}


def codex(tool, **inp):
    return {"cwd": VAULT, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp}


class TestWrites(unittest.TestCase):
    def test_only_pending_plans_are_writable(self):
        self.assertIsNone(g.decide(claude("Write", file_path=f"{VAULT}/plans/pending/plan-20260924-a1b2.json")))
        self.assertIsNone(g.decide(claude("Write", file_path="plans/pending/plan-20260924-a1b2.json")))
        for bad in ("wiki/entities/x.md", "state/pages.json", "raw/meetings/m.txt", "plans/applied/p.json",
                    "plans/pending/notes.md", "plans/pending/../../wiki/x.json", "AGENTS.md", "tools/second_brain.py"):
            self.assertIsNotNone(g.decide(claude("Write", file_path=bad)), bad)
            self.assertIsNotNone(g.decide(claude("Edit", file_path=bad, old_string="a", new_string="b")), bad)

    def test_vscode_and_copilot_cli_edit_tools(self):
        self.assertIsNotNone(g.decide(claude("replace_string_in_file", filePath=f"{VAULT}/wiki/index.md")))
        self.assertIsNone(g.decide(claude("create_file", filePath=f"{VAULT}/plans/pending/plan-20260924-x9y8.json")))
        self.assertIsNotNone(g.decide(copilot_cli("edit", path="wiki/concepts/a.md")))
        self.assertIsNone(g.decide(copilot_cli("create", path="plans/pending/plan-20260924-q1w2.json")))

    def test_codex_apply_patch(self):
        ok = "*** Begin Patch\n*** Add File: plans/pending/plan-20260924-c0de.json\n+{}\n*** End Patch"
        bad = "*** Begin Patch\n*** Update File: wiki/entities/beacon.md\n@@\n-a\n+b\n*** End Patch"
        mixed = ok.replace("*** End Patch", "*** Delete File: state/pages.json\n*** End Patch")
        self.assertIsNone(g.decide(codex("apply_patch", command=["apply_patch", ok])))
        self.assertIsNotNone(g.decide(codex("apply_patch", command=["apply_patch", bad])))
        self.assertIsNotNone(g.decide(codex("apply_patch", patch=mixed)))

    def test_write_without_target_is_denied(self):
        self.assertIsNotNone(g.decide(claude("Write", content="x")))

    def test_non_file_tools_pass(self):
        self.assertIsNone(g.decide(claude("TodoWrite", todos=[])))
        self.assertIsNone(g.decide(claude("Read", file_path="wiki/index.md")))
        self.assertIsNone(g.decide(claude("Grep", pattern="Beacon")))


class TestShell(unittest.TestCase):
    def test_agent_commands(self):
        for cmd in ("python tools/second_brain.py status",
                    "python tools/second_brain.py hash beacon --sections",
                    "python .\\tools\\second_brain.py render-plan plans/pending/plan-20260924-a1b2.json",
                    "py tools/second_brain.py source session-2026-09-18-x"):
            self.assertIsNone(g.decide(claude("Bash", command=cmd)), cmd)
        for cmd in ("python tools/second_brain.py apply-plan plans/pending/p.json --approve abc",
                    "python tools/second_brain.py rollback txn-1",
                    "python tools/second_brain.py discover",
                    "python tools/second_brain.py --vault ../other status",
                    "python -c \"open('wiki/x.md','w').write('x')\"",
                    "python tools/other.py"):
            self.assertIsNotNone(g.decide(claude("Bash", command=cmd)), cmd)

    def test_read_only_pipelines_allowed(self):
        for cmd in ("cat wiki/index.md | head -20", "rg -n \"Beacon\" wiki 2>/dev/null", "ls -la raw/meetings",
                    "sed -n '1,40p' raw/sessions/a.md", "Get-Content wiki/index.md | Select-Object -First 5",
                    "grep -c status=500 raw/x.log 2>&1"):
            self.assertIsNone(g.decide(claude("Bash", command=cmd)), cmd)

    def test_writing_shell_commands_denied(self):
        for cmd in ("echo x > wiki/a.md", "cat a >> wiki/b.md", "cp raw/a.md wiki/", "rm wiki/index.md",
                    "sed -i 's/a/b/' wiki/x.md", "find wiki -delete", "Set-Content wiki/x.md 'a'",
                    "cat a | tee wiki/x.md", "ls; rm -rf state", "ls && python tools/second_brain.py apply-plan p",
                    "git commit -am x", "curl http://example.com", "echo $(rm x)"):
            self.assertIsNotNone(g.decide(claude("Bash", command=cmd)), cmd)

    def test_review_bypasses_denied(self):
        """review 2026-09-23 P1: "read-only" commands that can still write files or run code."""
        for cmd in ("sed --in-place=.bak 's/a/b/' wiki/x.md", "sed -n 's/a/b/w wiki/x.md' raw/a.md",
                    "sed -n '1e rm -rf wiki' raw/a.md", "sed 's/a/b/' raw/a.md",
                    "Get-ChildItem wiki | ForEach-Object { [IO.File]::WriteAllText($_.FullName, 'x') }",
                    "gci wiki | ForEach-Object Delete",
                    "gci wiki | Where-Object { Remove-Item $_ }",
                    "gci wiki | Select-Object @{n='x';e={[IO.File]::Delete('wiki/index.md')}}",
                    "echo ([IO.File]::WriteAllText('wiki/a.md','x'))", "echo @(Remove-Item wiki/a.md)",
                    "[IO.File]::WriteAllText('wiki/a.md','x')", "(Get-Item wiki/a.md).Delete()",
                    "awk 'BEGIN{print 1}' raw/a.md", "sort -o wiki/a.md raw/a.md", "sort --output=wiki/a.md x",
                    "uniq raw/a.md wiki/a.md", "tree -o wiki/a.md", "rg --pre sh x raw", "find wiki -execdir rm {} +",
                    "find wiki -fprintf wiki/a.md x", "file -C -m raw/x", "less -o wiki/a.md raw/a.md",
                    # review 2
                    "sort /O wiki/x.md raw/a.md", "diff --output=wiki/x.md raw/a.md raw/b.md",
                    "sort --compress-program=sh raw/a.md",
                    "python tools/second_brain.py --vault=C:/outside status",
                    # install / upgrade are human-only
                    "python tools/second_brain.py upgrade . --force", "python tools/second_brain.py install ../x"):
            self.assertIsNotNone(g.decide(claude("Bash", command=cmd)), cmd)
        for cmd in ("rg -n \"def foo\\(\" tools", "sed -n '/^## Notes/,/^## /p' wiki/a.md", "sed -n '120,$p' x",
                    "Get-ChildItem wiki | Where-Object Name -like '*.md' | Select-Object -ExpandProperty Name",
                    "sort raw/a.md | uniq -c", "find wiki -name '*.md'"):
            self.assertIsNone(g.decide(claude("Bash", command=cmd)), cmd)

    def test_wrapped_commands(self):
        self.assertIsNone(g.decide(codex("Bash", command=["bash", "-lc", "cat wiki/index.md | head"])))
        self.assertIsNotNone(g.decide(codex("Bash", command=["bash", "-lc", "echo x > wiki/a.md"])))
        self.assertIsNotNone(g.decide(codex("Bash", command=[
            "powershell", "-NoProfile", "-Command", "python tools/second_brain.py apply-plan p --approve x"])))
        self.assertIsNotNone(g.decide(copilot_cli("powershell", command="Remove-Item wiki/x.md")))
        self.assertIsNone(g.decide(copilot_cli("bash", command="python tools/second_brain.py trace x")))


class TestHookProcess(unittest.TestCase):
    """End to end through stdin/exit code, as the agents call it."""

    def run_guard(self, payload):
        return subprocess.run([sys.executable, str(REPO / "tools" / "agent_guard.py")], input=json.dumps(payload),
                              capture_output=True, text=True)

    def test_exit_codes(self):
        allowed = self.run_guard(claude("Bash", command="python tools/second_brain.py status"))
        self.assertEqual(allowed.returncode, 0)
        denied = self.run_guard(claude("Write", file_path="wiki/x.md"))
        self.assertEqual(denied.returncode, 2)
        self.assertIn("plans/pending", denied.stderr)


if __name__ == "__main__":
    unittest.main()

"""Second Brain write-plan applier and vault CLI.

Page parsing, section addressing, hashing, plan validation, rendering and transactional application of the
write-plan format documented in vault-template/PLAN-SCHEMA.md. Standard library only.
Comments cite sections (§) of the project's internal design notes, which are not published.

Usage:
    python second_brain.py [--vault DIR] hash <page_id> [--sections]
    python second_brain.py [--vault DIR] validate-plan <plan.json>
    python second_brain.py [--vault DIR] render-plan <plan.json>
    python second_brain.py [--vault DIR] apply-plan <plan.json> --approve <sha256> [--allow-restructure]
    python tools/second_brain.py install <new-vault-dir>           (from the project: create a vault)
    python tools/second_brain.py upgrade <vault-dir> [--dry-run]  (from the project: update rules and tools)
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# constants

TYPE_DIRS = {
    "source": "sources",
    "entity": "entities",
    "concept": "concepts",
    "decision": "decisions",
    "synthesis": "synthesis",
}
INDEX_SECTIONS = [("decision", "Decisions"), ("concept", "Concepts"), ("entity", "Entities"),
                  ("synthesis", "Synthesis"), ("source", "Sources")]

# frontmatter key order (spec §1.2)
KEY_ORDER = [
    "page_id", "page_type", "sources", "created", "updated",
    "status", "superseded_by", "decided_on", "date_confidence", "origin_sources",
    "source_id", "source_type", "raw_path", "source_date", "date_basis", "synthetic",
    "title", "tags", "aliases", "scope",
]
LLM_KEYS = {"title", "tags", "aliases", "scope"}
CREATE_META_KEYS = {"tags", "aliases", "scope"}

PAGE_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PLAN_ID_RE = re.compile(r"^plan-\d{8}-[a-z0-9]{4,8}$")
OP_ID_RE = re.compile(r"^op-\d{3}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_date(value) -> bool:
    """YYYY-MM-DD that is also a real calendar day (2026-99-99 and 2026-02-30 are not)."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def valid_page_id(value) -> bool:
    """write-plan-schema §1.1: a-z0-9 words joined by single hyphens, at most 80 characters."""
    return isinstance(value, str) and len(value) <= 80 and bool(PAGE_ID_RE.match(value))


def one_of(value, allowed) -> bool:
    """Membership test that is safe for untrusted JSON / frontmatter values (lists are unhashable)."""
    return isinstance(value, str) and value in allowed


HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
LIST_ITEM_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s")
# lines that may start a CommonMark block: thematic break, setext underline, block quote, ATX heading, HTML
# block, table row (GFM)
BLOCK_START_RE = re.compile(r"^ {0,3}(?:(?:[-*_][ \t]*){3,}$|=+[ \t]*$|-+[ \t]*$|>|#{1,6}(?:[ \t]|$)|<|\|)")

DECISION_STATUSES = {"proposed", "active", "superseded", "revoked"}
CONFIDENCES = {"high", "medium", "low"}
SOURCE_TYPES = {"document", "meeting", "session"}
TRANSITIONS = {
    ("proposed", "active"), ("proposed", "revoked"), ("proposed", "superseded"),
    ("active", "superseded"), ("active", "revoked"),
}
SCHEMA_VERSION = 2
# [[target]], [[target#heading]], [[target|label]]; target must be a page_id (schema v1.1 / T4)
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]*)(#[^\[\]|]*)?(?:\|([^\[\]]*))?\]\]")
# a CommonMark code span: a run of N backticks closed by the next run of exactly N (`` a ` b `` is one span)
# spans may continue over line breaks inside one paragraph (re.S), never across a blank line or a fence
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
OP_TYPES = {"create_page", "update_section", "update_section_body", "append_to_section", "add_section",
            "delete_page", "retract_source",
            "update_meta", "decision_change", "restructure_page"}


class PlanError(Exception):
    def __init__(self, code: str, message: str, op_id: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.op_id = code, message, op_id

    def __str__(self) -> str:
        where = f"[{self.op_id}] " if self.op_id else ""
        return f"{where}{self.code}: {self.message}"


BOM = chr(0xFEFF)


def today() -> str:
    return os.environ.get("SB_TODAY") or dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# normalization and hashing (spec §1.4)

def normalize(text: str) -> str:
    if text.startswith(BOM):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # drop trailing blank (whitespace-only) lines; the last content line keeps its trailing spaces
    # (schema §1.4: two trailing spaces are a Markdown line break)
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# frontmatter subset (spec §1.2)

def _parse_scalar(raw: str):
    raw = raw.strip()
    if raw == "" or raw in ("null", "~"):
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw.startswith(("'", '"')):
        return _unquote(raw)
    return raw


def _unquote(token: str) -> str:
    """The one decoder for quoted scalars (plain values and list items alike):
    "…" with \\" and \\\\ escapes only, '…' with '' for a quote; the closing quote must end the token."""
    q, out, i = token[0], [], 1
    while i < len(token):
        ch = token[i]
        if q == '"' and ch == "\\":
            if i + 1 >= len(token) or token[i + 1] not in '"\\':
                raise ValueError(f"unsupported escape in {token}")
            out.append(token[i + 1])
            i += 2
            continue
        if ch == q:
            if q == "'" and token[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            if i != len(token) - 1:
                raise ValueError(f"text after the closing quote: {token}")
            return "".join(out)
        out.append(ch)
        i += 1
    raise ValueError(f"unterminated quoted value: {token}")


def _parse_list(raw: str) -> list:
    inner = raw.strip()[1:-1]
    items, buf, quote, i = [], "", None, 0
    while i < len(inner):
        ch = inner[i]
        if quote:
            if quote == '"' and ch == "\\" and i + 1 < len(inner):
                buf += inner[i:i + 2]
                i += 2
                continue
            if quote == "'" and ch == "'" and inner[i + 1:i + 2] == "'":
                buf += "''"
                i += 2
                continue
            if ch == quote:
                quote = None
            buf += ch
        elif ch in "\"'" and not buf.strip():  # a quote opens an item only at its start (YAML flow)
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        elif ch in "[]{}":
            raise ValueError("nested collections are not allowed")
        else:
            buf += ch
        i += 1
    if quote:
        raise ValueError("unterminated quote in list")
    if buf.strip() or items:
        items.append(buf)
    out = []
    for item in items:
        value = _parse_scalar(item)
        if value is None:
            raise ValueError("empty list item")
        out.append(value if isinstance(value, str) else json.dumps(value))
    return out


def parse_frontmatter(text: str) -> dict:
    fm: dict = {}
    for line in text.split("\n"):
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):(?:[ \t](.*))?$", line)
        if not m:
            raise ValueError(f"unsupported frontmatter line: {line!r}")
        key, raw = m.group(1), (m.group(2) or "")
        if key in fm:
            raise ValueError(f"duplicate frontmatter key: {key}")
        raw_s = raw.strip()
        if raw_s.startswith("["):
            if not raw_s.endswith("]"):
                raise ValueError(f"unterminated list for {key}")
            fm[key] = _parse_list(raw_s)
        elif raw_s.startswith(("{", "|", ">", "&", "*", "!")):
            raise ValueError(f"unsupported YAML construct for {key}")
        else:
            fm[key] = _parse_scalar(raw_s)
    return fm


_RESERVED = {"null", "true", "false", "~", ""}


def _dump_str(value: str, in_list: bool = False) -> str:
    """Encoder matching _unquote: anything that could be read back differently is double-quoted."""
    needs = (
        value.strip() != value or value in _RESERVED
        or value[:1] in "[{\"'#&*!|>%@`-?,"
        or ": " in value or " #" in value or value.endswith(":")
        or (in_list and any(c in value for c in ",[]{}"))
    )
    if needs:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _dump_value(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, list):
        return "[" + ", ".join(_dump_str(str(v), in_list=True) for v in value) + "]"
    return _dump_str(str(value))


def dump_frontmatter(fm: dict) -> str:
    keys = [k for k in KEY_ORDER if k in fm] + [k for k in fm if k not in KEY_ORDER]
    return "".join(f"{k}: {_dump_value(fm[k])}\n" for k in keys)


# ---------------------------------------------------------------------------
# page and section tree (spec §1.3)

@dataclass
class Heading:
    idx: int
    level: int
    text: str


@dataclass
class Node:
    level: int
    text: str | None
    start: int
    end: int
    children: list = field(default_factory=list)


def outside_fences(lines: list[str]):
    """(index, line) for every line outside fenced code — the one fence scanner (headings, links).
    A fence closes only on a run of the same character, at least as long, with nothing after it
    (CommonMark): "``` not-a-closing-fence" inside a block is still code."""
    fence = None
    for i, line in enumerate(lines):
        fm = FENCE_RE.match(line)
        if fence:
            if fm and fm.group(1)[0] == fence[0] and len(fm.group(1)) >= len(fence) \
                    and not line.strip()[len(fm.group(1)):].strip():
                fence = None
            continue
        if fm:
            fence = fm.group(1)
            continue
        yield i, line


def wikilink_targets(lines: list[str]) -> set[str]:
    """Targets of [[...]] links outside fenced code and code spans (schema v1.1 / T4). Code spans are found per
    paragraph — consecutive prose lines, broken by blank lines, fences, headings and list items — since a span
    may wrap. (Indented code blocks are not recognised: links there are still checked, the safe direction.)"""
    paragraphs, block, last = [], [], None
    for i, line in outside_fences(lines):
        # conservative: any line that could start a block (or a fence gap) ends the paragraph, so a code span
        # only ever wraps over plain continuation lines
        if not line.strip() or BLOCK_START_RE.match(line) or LIST_ITEM_RE.match(line) \
                or (last is not None and i != last + 1):
            paragraphs.append(block)
            block = []
        if line.strip():
            block.append(line)
        last = i
    paragraphs.append(block)
    return {m.group(1).strip() for block in paragraphs if block
            for m in WIKILINK_RE.finditer(INLINE_CODE_RE.sub("", "\n".join(block)))}


def scan_headings(lines: list[str]) -> list[Heading]:
    heads = []
    for i, line in outside_fences(lines):
        hm = HEADING_RE.match(line)
        if hm:
            heads.append(Heading(i, len(hm.group(1)), hm.group(2).strip()))
    return heads


@dataclass
class Tree:
    root: Node | None
    preamble: tuple[int, int] | None
    errors: list[tuple[str, str]]


def build_tree(lines: list[str]) -> Tree:
    heads = scan_headings(lines)
    errors: list[tuple[str, str]] = []
    h1s = [h for h in heads if h.level == 1]
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if not h1s or first is None or h1s[0].idx != first:
        errors.append(("HEADING_LEVEL", "page must start with exactly one H1 title"))
        return Tree(None, None, errors)
    if len(h1s) > 1:
        errors.append(("HEADING_LEVEL", f"page has {len(h1s)} H1 headings"))
    for h in heads:  # a section_path element must name exactly this heading (schema §1.3)
        if h.level > 1 and (not h.text or h.text == "__preamble__"):
            errors.append(("HEADING_LEVEL", f"heading {h.text!r} (line {h.idx + 1}) cannot be addressed "
                                            "by a section_path"))
    root = Node(1, None, h1s[0].idx, len(lines))
    stack = [root]
    for h in heads:
        if h.level == 1:
            continue
        while stack[-1].level >= h.level:
            stack.pop().end = h.idx
        parent = stack[-1]
        if h.level > parent.level + 1:
            errors.append(("HEADING_LEVEL", f"heading {h.text!r} (level {h.level}) skips a level"))
        node = Node(h.level, h.text, h.idx, len(lines))
        parent.children.append(node)
        stack.append(node)
    for n in stack[1:]:
        n.end = len(lines)

    def check_dups(node: Node):
        seen: dict[str, int] = {}
        for c in node.children:
            seen[c.text] = seen.get(c.text, 0) + 1
            check_dups(c)
        for text, n in seen.items():
            if n > 1:
                errors.append(("PATH_AMBIGUOUS", f"heading {text!r} appears {n} times under the same parent"))

    check_dups(root)
    pre_end = root.children[0].start if root.children else len(lines)
    return Tree(root, (root.start + 1, pre_end), errors)


class Page:
    def __init__(self, fm: dict, lines: list[str], path: Path | None = None, raw: str | None = None):
        self.fm, self.lines, self.path, self.raw = fm, lines, path, raw

    @classmethod
    def parse(cls, text: str, path: Path | None = None) -> "Page":
        text = normalize(text)
        if not text.startswith("---\n"):
            raise ValueError("missing frontmatter")
        end = text.find("\n---\n", 3)
        if end < 0:
            raise ValueError("unterminated frontmatter")
        fm = parse_frontmatter(text[4:end])
        body = text[end + 5:]
        return cls(fm, body.rstrip("\n").split("\n"), path, text)

    def text(self) -> str:
        return normalize("---\n" + dump_frontmatter(self.fm) + "---\n" + "\n".join(self.lines))

    @property
    def page_type(self) -> str | None:
        return self.fm.get("page_type")

    def title(self) -> str:
        if self.fm.get("title"):
            return str(self.fm["title"])
        for h in scan_headings(self.lines):
            if h.level == 1:
                return h.text
        return ""

    def tree(self) -> Tree:
        return build_tree(self.lines)

    def resolve(self, path: list[str]) -> tuple[Node | None, tuple[int, int], int]:
        """Return (node, (start, end), level). For __preamble__ node is None and level 1."""
        tree = self.tree()
        if tree.errors:
            code, msg = tree.errors[0]
            raise PlanError(code, f"page structure invalid: {msg}")
        if path == ["__preamble__"]:
            return None, tree.preamble, 1
        node = tree.root
        for i, name in enumerate(path):
            matches = [c for c in node.children if c.text == name]
            if not matches:
                raise PlanError("PATH_NOT_FOUND", f"section {' > '.join(path[:i + 1])!r} not found")
            node = matches[0]
        return node, (node.start, node.end), node.level


# ---------------------------------------------------------------------------
# line splicing helpers

def _strip_blank_edges(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _content_lines(content: str) -> list[str]:
    return _strip_blank_edges(normalize(content).rstrip("\n").split("\n")) if content.strip() else []


def splice(before: list[str], mid: list[str], after: list[str], tight: bool = False) -> list[str]:
    """Join three blocks with exactly one blank line at each boundary (no blank if tight)."""
    before = before[:]
    while before and not before[-1].strip():
        before.pop()
    after = after[:]
    while after and not after[0].strip():
        after.pop(0)
    out = before
    if mid:
        if out and not tight:
            out.append("")
        out += mid
    if after:
        if out:
            out.append("")
        out += after
    return out


def check_content_headings(content_lines: list[str], base_level: int, allow: bool) -> list[Heading]:
    heads = scan_headings(content_lines)
    if not heads:
        return heads
    if not allow:
        raise PlanError("CONTENT_HEADING", "content must not contain headings")
    prev = base_level
    for h in heads:
        if h.level <= base_level:
            raise PlanError("CONTENT_HEADING", f"heading {h.text!r} must be deeper than level {base_level}")
        if h.level > prev + 1:
            raise PlanError("CONTENT_HEADING", f"heading {h.text!r} skips a level")
        prev = h.level
    return heads


# ---------------------------------------------------------------------------
# wiki

class Wiki:
    def __init__(self, vault: Path):
        self.vault = vault
        self.root = vault / "wiki"
        self.pages: dict[str, Page] = {}
        for ptype, d in TYPE_DIRS.items():
            folder = self.root / d
            if not folder.is_dir():
                continue
            for f in sorted(folder.glob("*.md")):
                # identity problems block every plan with a structured error (not a traceback) until a human
                # resolves them; `maintain` names the file. Other page problems surface when a plan touches it.
                try:
                    raw = f.read_bytes().decode("utf-8")
                    page = Page.parse(raw, f)
                except (UnicodeDecodeError, ValueError) as e:
                    raise PlanError("SCHEMA", f"{d}/{f.name} cannot be read as a wiki page ({e}); run maintain")
                page.raw = raw
                # Stage 0 pages may omit page_id / page_type: the file name and directory stand in for them
                ident = {**page.fm, "page_id": page.fm.get("page_id") or f.stem,
                         "page_type": page.fm.get("page_type", ptype)}
                problems = identity_problems(ident, ptype)
                if problems:
                    raise PlanError("SCHEMA", f"{d}/{f.name}: {problems[0][1]}; run maintain")
                pid = ident["page_id"]
                if f.stem != pid:  # schema §1.1; otherwise a create_page of `<stem>` would land on this file
                    raise PlanError("SCHEMA", f"{d}/{f.name}: file name differs from page_id {pid!r}; "
                                              "run maintain --fix to rename it back")
                if pid in self.pages:
                    raise PlanError("SCHEMA", f"duplicate page_id {pid!r} in wiki")
                self.pages[pid] = page

    def source_ids(self) -> dict[str, Page]:
        out = {}
        for pid, p in self.pages.items():
            if p.page_type == "source":
                out[p.fm.get("source_id") or pid] = p
        return out


# ---------------------------------------------------------------------------
# plan schema validation

def _check_block(block: dict, ptype: str, keys: tuple, op_id: str) -> None:
    """A plan's decision / source block becomes page frontmatter: check it with the page schema's predicates."""
    for k in keys:
        if k not in block:
            raise PlanError("SCHEMA", f"{ptype}.{k} is required", op_id)
        if not FM_BY_TYPE[ptype][k](block[k]):
            raise PlanError("SCHEMA", f"{ptype}.{k} has the wrong type or form: {block[k]!r}", op_id)


def _req(op: dict, key: str, kind, op_id: str):
    if key not in op:
        raise PlanError("SCHEMA", f"missing field {key!r}", op_id)
    if not isinstance(op[key], kind):
        raise PlanError("SCHEMA", f"field {key!r} has wrong type", op_id)
    return op[key]


def _str_list(value, key: str, op_id: str | None) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise PlanError("SCHEMA", f"{key!r} must be a list of non-empty strings", op_id)
    return value


def validate_schema(plan) -> None:
    try:
        _validate_schema(plan)
    except (TypeError, AttributeError, KeyError) as e:
        # a field of the wrong JSON type that a check below did not anticipate: still a structured rejection
        raise PlanError("SCHEMA", f"malformed plan ({type(e).__name__}: {e})")


def _validate_schema(plan) -> None:
    if not isinstance(plan, dict):
        raise PlanError("SCHEMA", "plan must be a JSON object")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise PlanError("SCHEMA", f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(plan.get("plan_id"), str) or not PLAN_ID_RE.match(plan["plan_id"]):
        raise PlanError("SCHEMA", "plan_id must match plan-YYYYMMDD-xxxx")
    _str_list(plan.get("source_ids"), "source_ids", None)
    sv = plan.get("source_versions")
    if sv is not None and (not isinstance(sv, dict) or not all(isinstance(v, str) and HASH_RE.match(v) for v in sv.values())):
        raise PlanError("SCHEMA", "source_versions must map source_id -> sha256:<64 hex>")
    ops = plan.get("operations")
    if not isinstance(ops, list) or not ops:
        raise PlanError("SCHEMA", "operations must be a non-empty list")
    seen = set()
    plan_sources = set(plan["source_ids"])
    for op in ops:
        if not isinstance(op, dict):
            raise PlanError("SCHEMA", "each operation must be an object")
        op_id = op.get("operation_id")
        if not isinstance(op_id, str) or not OP_ID_RE.match(op_id):
            raise PlanError("SCHEMA", f"bad operation_id {op_id!r}")
        if op_id in seen:
            raise PlanError("SCHEMA", "duplicate operation_id", op_id)
        seen.add(op_id)
        t = op.get("type")
        if not one_of(t, OP_TYPES):
            raise PlanError("SCHEMA", f"unknown operation type {t!r}", op_id)
        pid = _req(op, "page_id", str, op_id)
        if not valid_page_id(pid):
            raise PlanError("SCHEMA", f"invalid page_id {pid!r}", op_id)
        if "human_override" in op:
            ho = op["human_override"]
            if not isinstance(ho, dict) or ho.get("confirmed_by_user") is not True or not isinstance(ho.get("note"), str):
                raise PlanError("SCHEMA", "human_override needs confirmed_by_user=true and note", op_id)
        is_source_create = t == "create_page" and op.get("page_type") == "source"
        if "source_ids" in op or not is_source_create:
            srcs = _str_list(op.get("source_ids"), "source_ids", op_id)
            if not srcs and not is_source_create and t != "delete_page":
                raise PlanError("SCHEMA", "source_ids must not be empty", op_id)
            extra = set(srcs) - plan_sources
            if extra:
                raise PlanError("SCHEMA", f"source_ids not declared at plan level: {sorted(extra)}", op_id)
        if is_source_create and pid not in plan_sources:
            # the new source page is evidence too: it must be bound by source_versions like every other source
            raise PlanError("SCHEMA", f"source page {pid!r} must be listed in the plan's source_ids "
                                      "(and its content_hash in source_versions)", op_id)
        if "expected_hash" in op and (not isinstance(op["expected_hash"], str) or not HASH_RE.match(op["expected_hash"])):
            raise PlanError("SCHEMA", "expected_hash must be sha256:<64 hex>", op_id)
        if t == "create_page":
            ptype = _req(op, "page_type", str, op_id)
            if ptype not in TYPE_DIRS:
                raise PlanError("SCHEMA", f"unknown page_type {ptype!r}", op_id)
            _req(op, "title", str, op_id)
            _req(op, "body", str, op_id)
            meta = op.get("meta", {})
            if not isinstance(meta, dict):
                raise PlanError("SCHEMA", "meta must be an object", op_id)
            bad = set(meta) - CREATE_META_KEYS
            if bad:
                raise PlanError("FORBIDDEN_META", f"meta keys not allowed: {sorted(bad)}", op_id)
            _check_meta_values({"title": op["title"], **meta}, op_id)
            if ptype == "decision":
                d = _req(op, "decision", dict, op_id)
                if d.get("status") not in ("proposed", "active"):
                    raise PlanError("SCHEMA", "new decision status must be proposed or active", op_id)
                _check_block(d, "decision", ("decided_on", "date_confidence"), op_id)
            elif "decision" in op:
                raise PlanError("SCHEMA", "decision block only allowed for decision pages", op_id)
            if ptype == "source":
                s = _req(op, "source", dict, op_id)
                _check_block(s, "source", ("source_id", "source_type", "raw_path", "source_date", "date_confidence",
                                           "date_basis", "synthetic"), op_id)
                if s["source_id"] != pid:
                    raise PlanError("SCHEMA", "source page_id must equal source.source_id", op_id)
            elif "source" in op:
                raise PlanError("SCHEMA", "source block only allowed for source pages", op_id)
        elif t in ("update_section", "update_section_body", "append_to_section"):
            _str_list(_req(op, "section_path", list, op_id), "section_path", op_id)
            if not op["section_path"]:
                raise PlanError("SCHEMA", "section_path must not be empty", op_id)
            _req(op, "content", str, op_id)
        elif t == "add_section":
            if _req(op, "parent_path", list, op_id):
                _str_list(op["parent_path"], "parent_path", op_id)
            h = _req(op, "heading", str, op_id)
            if not h.strip() or "\n" in h or h.strip() == "__preamble__":
                raise PlanError("SCHEMA", "heading must be a single non-empty line", op_id)
            _req(op, "content", str, op_id)
            pos = op.get("position", "end")
            if not (pos in ("start", "end") or (isinstance(pos, dict) and set(pos) == {"after"} and isinstance(pos["after"], str))):
                raise PlanError("SCHEMA", "position must be start, end or {after: heading}", op_id)
        elif t == "update_meta":
            s = _req(op, "set", dict, op_id)
            if not s:
                raise PlanError("SCHEMA", "set must not be empty", op_id)
            bad = set(s) - LLM_KEYS
            if bad:
                raise PlanError("FORBIDDEN_META", f"keys not settable: {sorted(bad)}", op_id)
        elif t == "decision_change":
            ns = _req(op, "new_status", str, op_id)
            if ns not in DECISION_STATUSES - {"proposed"}:
                raise PlanError("SCHEMA", "new_status must be active, superseded or revoked", op_id)
            _req(op, "history_note", str, op_id)
            if ns == "superseded":
                _req(op, "superseded_by", str, op_id)
            elif "superseded_by" in op:
                raise PlanError("SCHEMA", "superseded_by only allowed with new_status=superseded", op_id)
            if "temporal_override" in op:
                to = op["temporal_override"]
                if not isinstance(to, dict) or to.get("confirmed_by_user") is not True or not isinstance(to.get("note"), str):
                    raise PlanError("SCHEMA", "temporal_override needs confirmed_by_user=true and note", op_id)
        elif t == "restructure_page":
            _req(op, "body", str, op_id)
            _req(op, "reason", str, op_id)
        elif t in ("delete_page", "retract_source"):
            if not _req(op, "reason", str, op_id).strip():
                raise PlanError("SCHEMA", "reason must not be empty", op_id)
            if t == "retract_source" and op.get("source_ids") != [pid]:
                raise PlanError("SCHEMA", "retract_source must cite exactly its own source_id", op_id)


# ---------------------------------------------------------------------------
# execution (in memory)

@dataclass
class OpResult:
    op: dict
    page_id: str
    before: str
    after: str
    flags: list[str]
    detail: list[str]


@dataclass
class Execution:
    pages: dict[str, Page]
    created: list[str]
    touched: list[str]
    results: list[OpResult]
    decision_changes: list[tuple[str, str, str]]
    human_detected: set = field(default_factory=set)
    txn_id: str | None = None
    deleted: list = field(default_factory=list)        # page_ids removed by delete_page / retract_source
    retracted: list = field(default_factory=list)      # source_ids retracted
    deleted_paths: dict = field(default_factory=dict)  # page_id -> original file path
    warnings: list = field(default_factory=list)
    ingested: list = field(default_factory=list)       # source_ids this plan (re-)ingests (managed vault)


def _source_flags(src_pages: dict[str, Page], ids: list[str], registry: dict | None = None) -> list[str]:
    """Per-op flags from the sources it cites. In a managed vault the date comes from the registry, which is
    ahead of the source page after discover re-dated a changed raw file (the page is synced later)."""
    flags = []
    for sid in ids:
        p = src_pages.get(sid)
        if p is not None and p.fm.get("synthetic") is True and "SYNTHETIC SOURCE" not in flags:
            flags.append("SYNTHETIC SOURCE")
        sd = registry[sid]["source_date"] if registry and sid in registry else p.fm.get("source_date") if p else None
        if valid_date(sd) and sd > today() and "FUTURE DATE" not in flags:
            flags.append("FUTURE DATE")
    return flags


def _names(pages: dict[str, Page]) -> dict[str, str]:
    out = {}
    for pid, p in pages.items():
        for n in [p.title(), *(p.fm.get("aliases") or [])]:
            if n:
                out.setdefault(str(n).lower(), pid)
    return out


def _is_dated(fm: dict) -> bool:
    d = fm.get("decided_on")
    return valid_date(d) and fm.get("date_confidence") in ("high", "medium")


def execute(wiki: Wiki, plan: dict, allow_restructure: bool = False, state: "State | None" = None) -> Execution:
    validate_schema(plan)
    ops = plan["operations"]
    if state is not None:  # Stage 1 managed vault
        state.check_no_incomplete()
        registry = _managed_source_checks(state, plan, ops)
        _scan_plan_secrets(plan)
        pages_reg = state.pages()
    else:
        registry = {}
    pages = {pid: copy.deepcopy(p) for pid, p in wiki.pages.items()}
    existing = set(pages)
    created_ids = [op["page_id"] for op in ops if op["type"] == "create_page"]

    # source existence
    src_pages = wiki.source_ids()
    planned_sources = {op["page_id"] for op in ops if op["type"] == "create_page" and op["page_type"] == "source"}
    for sid in plan["source_ids"]:
        if sid not in src_pages and sid not in planned_sources:
            raise PlanError("UNKNOWN_SOURCE", f"no source page for {sid!r}")

    # expected_hash consistency and staleness (spec §2.1)
    expected: dict[str, str] = {}
    created_so_far: set[str] = set()
    deleted_so_far: set[str] = set()
    for op in ops:
        pid, oid = op["page_id"], op["operation_id"]
        if pid in deleted_so_far:
            raise PlanError("UNKNOWN_PAGE", f"page {pid!r} is deleted earlier in this plan", oid)
        if op["type"] in ("delete_page", "retract_source"):
            if pid not in existing:
                raise PlanError("UNKNOWN_PAGE", f"can only delete existing pages, not {pid!r}", oid)
            deleted_so_far.add(pid)
        if op["type"] == "create_page":
            if pid in existing or pid in created_so_far:
                raise PlanError("PAGE_EXISTS", f"page {pid!r} already exists", oid)
            target = wiki.root / TYPE_DIRS[op["page_type"]] / f"{pid}.md"
            if target.exists():  # last guard against data loss: never write a new page over any existing file
                raise PlanError("PAGE_EXISTS", f"{target.relative_to(wiki.vault).as_posix()} is already taken", oid)
            if "expected_hash" in op:
                raise PlanError("SCHEMA", "create_page must not carry expected_hash", oid)
            created_so_far.add(pid)
        elif pid in existing:
            if "expected_hash" not in op:
                raise PlanError("SCHEMA", "operations on existing pages need expected_hash", oid)
            if expected.setdefault(pid, op["expected_hash"]) != op["expected_hash"]:
                raise PlanError("HASH_MISMATCH_IN_PLAN", f"different expected_hash values for {pid!r}", oid)
        elif pid in created_so_far:
            if "expected_hash" in op:
                raise PlanError("SCHEMA", f"page {pid!r} is created in this plan; omit expected_hash", oid)
        elif pid in created_ids:
            raise PlanError("UNKNOWN_PAGE", f"page {pid!r} is used before it is created", oid)
        else:
            raise PlanError("UNKNOWN_PAGE", f"page {pid!r} does not exist", oid)
    for pid, h in expected.items():
        current = sha256_text(wiki.pages[pid].raw or wiki.pages[pid].text())
        if current != h:
            raise PlanError("PLAN_STALE", f"page {pid!r} changed since the plan was generated")

    # wikilinks in plan-supplied text must point at page_ids (existing or created anywhere in this plan)
    known_ids = existing | set(created_ids)
    for op in ops:
        for key in ("body", "content", "history_note"):
            if isinstance(op.get(key), str):
                check_links(op[key], known_ids, op["operation_id"])

    ex = Execution(pages, [], [], [], [])
    retracting = {op["page_id"] for op in ops if op["type"] == "retract_source"}
    page_sources: dict[str, set] = {}
    for op in ops:
        oid, pid, t = op["operation_id"], op["page_id"], op["type"]
        before = pages[pid].text() if pid in pages else ""
        early = [s for s in op.get("source_ids", []) if s in planned_sources and s not in src_pages
                 and not (t == "create_page" and s == pid)]
        if early:  # e.g. a History line would have no page to link the source to: never drop provenance
            raise PlanError("SCHEMA", f"cites {early} before the create_page of that source page; "
                                      "put source-page creation first in the plan", oid)
        flags = list(_source_flags(src_pages, op.get("source_ids", []), registry))
        detail: list[str] = []
        try:
            if state is not None:
                _human_check(state, pages_reg, wiki.pages, pid, op, flags, ex.human_detected)
            if t == "create_page":
                _op_create(pages, op, flags, src_pages)
                ex.created.append(pid)
                if op["page_type"] == "source":
                    src_pages[pid] = pages[pid]
            elif t == "restructure_page":
                if not allow_restructure:
                    raise PlanError("RESTRUCTURE_DISABLED", "restructure_page needs --allow-restructure")
                _op_restructure(pages[pid], op)
            elif t == "update_section":
                _op_update_section(pages[pid], op, flags)
            elif t == "update_section_body":
                _op_update_section_body(pages[pid], op)
            elif t == "append_to_section":
                _op_append(pages[pid], op)
            elif t == "add_section":
                _op_add_section(pages[pid], op)
            elif t == "update_meta":
                _op_update_meta(pages, pid, op, flags)
            elif t == "decision_change":
                change = _op_decision_change(pages, pid, op, flags, detail, src_pages, wiki.pages, retracting)
                ex.decision_changes.append(change)
            elif t in ("delete_page", "retract_source"):
                _op_delete(pages, pid, op, flags)
                ex.deleted.append(pid)
                ex.deleted_paths[pid] = wiki.pages[pid].path
                if pid in ex.touched:
                    ex.touched.remove(pid)
                if t == "retract_source":
                    ex.retracted.append(pid)
                ex.results.append(OpResult(op, pid, before, "", flags, detail))
                continue
        except PlanError as e:
            e.op_id = e.op_id or oid
            raise
        if pages[pid].fm.get("human_touched") is True:
            flags.insert(0, "HUMAN-MAINTAINED PAGE")
        page_sources.setdefault(pid, set()).update(op.get("source_ids", []))
        if pid not in ex.created and pid not in ex.touched:
            ex.touched.append(pid)
        # applier-owned frontmatter (spec §4), kept current after every op so render shows final values
        p = pages[pid]
        if p.page_type == "source":
            # a source page cites only itself, even when a later source edits it (0B finding T2)
            p.fm["sources"] = [p.fm.get("source_id") or pid]
            rec = registry.get(p.fm.get("source_id") or pid)
            if rec and pid not in ex.created:  # discover moved / re-dated the raw file since the page was written
                flags.extend(_sync_source_fm(p.fm, rec))
        else:
            p.fm["sources"] = sorted((set(p.fm.get("sources") or []) | page_sources[pid]) - retracting)
        p.fm["updated"] = today()
        if pid in ex.created:
            p.fm["created"] = today()
        ex.results.append(OpResult(op, pid, before, p.text(), flags, detail))
    if state is not None:
        _settle_ingest(ex, plan, wiki, registry)
    if ex.deleted:
        _check_after_delete(ex, retracting, {s for s, r in registry.items() if r["status"] == "retracted"})
    _check_result_pages(ex, {s: r["status"] != "retracted" and s not in retracting for s, r in registry.items()}
                        if state is not None else None)
    return ex


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip()) and "\n" not in v


def _date_or_unknown(v) -> bool:
    return v == "unknown" or valid_date(v)


def _str_list_ok(v) -> bool:
    return isinstance(v, list) and all(_text(x) for x in v)


# frontmatter schema (write-plan-schema §1.2): key → type check. Frontmatter values are str / None / bool /
# list[str] (see parse_frontmatter), so every check is a plain predicate, never a set lookup on raw values.
FM_COMMON = {
    "page_id": valid_page_id,
    "page_type": lambda v: one_of(v, TYPE_DIRS),
    "title": _text,
    "sources": lambda v: v is None or _str_list_ok(v),
    "created": lambda v: v is None or valid_date(v),
    "updated": lambda v: v is None or valid_date(v),
    "tags": _str_list_ok, "aliases": _str_list_ok, "scope": _text,
    "human_touched": lambda v: isinstance(v, bool),  # Stage 0 pages kept it in frontmatter
}
FM_BY_TYPE = {
    "decision": {"status": lambda v: one_of(v, DECISION_STATUSES), "superseded_by": lambda v: v is None or _text(v),
                 "decided_on": _date_or_unknown, "date_confidence": lambda v: one_of(v, CONFIDENCES),
                 "origin_sources": _str_list_ok},
    "source": {"source_id": _text, "source_type": lambda v: one_of(v, SOURCE_TYPES), "raw_path": _text,
               "source_date": _date_or_unknown, "date_confidence": lambda v: one_of(v, CONFIDENCES),
               "date_basis": _text, "synthetic": lambda v: isinstance(v, bool)},
}
FM_REQUIRED = {None: ("page_id", "page_type", "title"),
               "decision": ("status", "decided_on", "date_confidence"),
               "source": ("source_id", "source_type", "raw_path", "source_date", "date_confidence", "date_basis",
                          "synthetic")}


IDENTITY = ("page_id", "page_type", "source_id")


def identity_problems(fm: dict, dir_type: str | None = None) -> list[tuple[str, str]]:
    """The fields a page is found and addressed by — the one safe check every entry point runs before such a
    value is used as a dict key, a path or a set member (Wiki loader, maintain, page_problems)."""
    out = []
    if not valid_page_id(fm.get("page_id")):
        out.append(("SCHEMA", f"invalid or missing page_id {fm.get('page_id')!r}"))
    ptype = fm.get("page_type")
    if not one_of(ptype, TYPE_DIRS):
        out.append(("SCHEMA", f"unknown page_type {ptype!r}"))
    elif dir_type is not None and ptype != dir_type:
        out.append(("SCHEMA", f"page_type {ptype!r} does not belong in wiki/{TYPE_DIRS[dir_type]}/ "
                              f"(move it to wiki/{TYPE_DIRS[ptype]}/)"))
    if ptype == "source" and fm.get("source_id") != fm.get("page_id"):
        out.append(("SCHEMA", f"source page_id {fm.get('page_id')!r} must equal source_id {fm.get('source_id')!r}"))
    return out


def page_problems(page: Page, dir_type: str | None = None,
                  known_sources: dict[str, bool] | None = None) -> list[tuple[str, str]]:
    """The one definition of a manageable page (schema §1, stage1-design §5), shared by maintain and the
    post-plan check: frontmatter keys / required keys / value types per page type, page_type vs directory,
    source page_id == source_id, provenance pointing at real sources (when `known_sources` is given:
    source_id → still live, i.e. not retracted), section tree, H1 == title."""
    fm = page.fm
    out = identity_problems(fm, dir_type)
    ptype = fm.get("page_type") if one_of(fm.get("page_type"), TYPE_DIRS) else None
    schema = {**FM_COMMON, **FM_BY_TYPE.get(ptype, {})}
    extra = sorted(set(fm) - set(schema))
    if extra:
        out.append(("SCHEMA", f"frontmatter keys not allowed on a {ptype or 'page'} page: {extra}"))
    missing = [k for k in FM_REQUIRED[None] + FM_REQUIRED.get(ptype, ()) if fm.get(k) is None and k not in IDENTITY]
    if missing:
        out.append(("SCHEMA", f"frontmatter misses {missing}"))
    wrong = [k for k, v in fm.items() if k in schema and k not in IDENTITY and v is not None and not schema[k](v)]
    if wrong:
        out.append(("SCHEMA", "frontmatter values of the wrong type or form: "
                              + ", ".join(f"{k}={fm[k]!r}" for k in wrong)))
    if known_sources is not None:
        # `sources` is what the page rests on now: live sources only. `origin_sources` is immutable history
        # (a decision revoked because its source was retracted keeps it): registered is enough.
        dead = sorted({s for s in fm.get("sources") or [] if not known_sources.get(s)})
        unknown = sorted({s for s in fm.get("origin_sources") or [] if s not in known_sources})
        if dead:
            out.append(("UNKNOWN_SOURCE", f"sources not registered or retracted: {dead}"))
        if unknown:
            out.append(("UNKNOWN_SOURCE", f"origin_sources not registered: {unknown}"))
    tree = page.tree()
    out += tree.errors
    if tree.root is not None and fm.get("title") and \
            page.lines[tree.root.start].rstrip() != f"# {fm['title']}".rstrip():
        out.append(("HEADING_LEVEL", f"H1 {page.lines[tree.root.start].strip()!r} does not match title {fm['title']!r}"))
    return out


def _settle_ingest(ex: Execution, plan: dict, wiki: Wiki, registry: dict) -> None:
    """Which plan sources end up (re-)ingested, judged on the final state (stage1-design §2.3): the source
    page is created, or its body differs from before the plan. Tags, a metadata sync, edits elsewhere, or
    edits that cancel out leave a `changed` source changed — and the render says so."""
    def below_h1(page: Page) -> list[str]:  # the title (H1) is metadata: update_meta(title) rewrites it
        root = page.tree().root
        return page.lines[root.start + 1:] if root is not None else page.lines

    for sid in plan["source_ids"]:
        if sid in ex.deleted:
            continue
        if sid in ex.created or (sid in ex.pages and sid in wiki.pages
                                 and below_h1(ex.pages[sid]) != below_h1(wiki.pages[sid])):
            ex.ingested.append(sid)
        elif registry.get(sid, {}).get("status") == "changed":
            ex.warnings.append(f"SOURCE STILL CHANGED: {sid} — its source page body is the same after this plan, "
                               "so the new raw content is not ingested; the source stays `changed`")


def _check_result_pages(ex: Execution, known_sources: dict[str, bool] | None = None) -> None:
    """Every page the plan writes must itself be a valid page (schema §1.3): a plan that validates must not
    produce a page that the next plan cannot address (PATH_AMBIGUOUS) or maintain calls NOT_MANAGEABLE."""
    for pid in ex.created + ex.touched:
        try:
            page = Page.parse(ex.pages[pid].text())
        except ValueError as e:
            raise PlanError("SCHEMA", f"resulting page {pid!r} cannot be parsed: {e}")
        problems = page_problems(page, known_sources=known_sources)
        if problems:
            code, msg = problems[0]
            raise PlanError(code, f"resulting page {pid!r} would be invalid: {msg}")


def _op_delete(pages: dict[str, Page], pid: str, op: dict, flags: list[str]) -> None:
    ptype = pages[pid].page_type
    if op["type"] == "delete_page":
        if ptype == "decision":
            raise PlanError("DELETE_FORBIDDEN", "decision pages are kept as history; use decision_change -> revoked")
        if ptype == "source":
            raise PlanError("DELETE_FORBIDDEN", "source pages are removed with retract_source")
        flags.append("PAGE DELETED")
    else:
        if ptype != "source":
            raise PlanError("SCHEMA", f"retract_source targets a source page; {pid!r} is {ptype}")
        flags.append("SOURCE RETRACTED")
    del pages[pid]


def _check_after_delete(ex: Execution, retracting: set[str], retracted_before: set[str] = frozenset()) -> None:
    """The wiki must stay consistent once the plan is applied (delete_page / retract_source)."""
    gone = set(ex.deleted)
    dangling = {pid: sorted(wikilink_targets(p.lines) & gone) for pid, p in ex.pages.items()}
    dangling = {pid: t for pid, t in dangling.items() if t}
    if dangling:
        raise PlanError("BROKEN_LINK", "deleted pages are still linked from: " +
                        "; ".join(f"{pid} -> {', '.join(t)}" for pid, t in sorted(dangling.items())))
    if retracting:
        cited = sorted(pid for pid, p in ex.pages.items() if set(p.fm.get("sources") or []) & retracting)
        if cited:
            raise PlanError("SOURCE_STILL_CITED", "pages still cite the retracted source; update each of them in "
                            "this plan (see: second_brain.py trace <source_id>): " + ", ".join(cited))
        # judged on the final set: sources retracted by earlier plans count too (A now, B later → orphan)
        gone_sources = retracting | retracted_before
        orphans = sorted(pid for pid, p in ex.pages.items() if p.page_type == "decision"
                         and p.fm.get("status") in ("active", "proposed") and _provenance(p.fm)
                         and set(_provenance(p.fm)) <= gone_sources and set(_provenance(p.fm)) & retracting)
        if orphans:
            raise PlanError("DECISION_FROM_RETRACTED", "decisions that came only from the retracted source must be "
                            "revoked in this plan: " + ", ".join(orphans))
    for pid in ex.touched:
        p = ex.pages[pid]
        if p.page_type != "source" and not p.fm.get("sources"):
            ex.warnings.append(f"NO REMAINING SOURCES: {pid} (consider delete_page)")


def _validate_body(body: str) -> list[str]:
    lines = _content_lines(body)
    check_content_headings(lines, 1, allow=True)
    return lines


def _op_create(pages: dict[str, Page], op: dict, flags: list[str], src_pages: dict[str, Page]) -> None:
    pid, ptype = op["page_id"], op["page_type"]
    body = _validate_body(op["body"])
    names = _names(pages)
    candidates = [op["title"], *(op.get("meta", {}).get("aliases") or [])]
    dups = sorted({names[n.lower()] for n in candidates if n and n.lower() in names})
    if dups:
        flags.append("POSSIBLE DUPLICATE: " + ", ".join(dups))
    fm: dict = {"page_id": pid, "page_type": ptype, "sources": [], "created": None, "updated": None}
    if ptype == "decision":
        d = op["decision"]
        fm.update(status=d["status"], superseded_by=None, decided_on=d["decided_on"], date_confidence=d["date_confidence"],
                  origin_sources=sorted(op["source_ids"]))  # immutable provenance (schema v1.1 / T5)
    if ptype == "source":
        s = op["source"]
        fm.update({k: s[k] for k in ("source_id", "source_type", "raw_path", "source_date", "date_confidence", "date_basis", "synthetic")})
        if s["synthetic"] and "SYNTHETIC SOURCE" not in flags:
            flags.append("SYNTHETIC SOURCE")
        if valid_date(s["source_date"]) and s["source_date"] > today() and "FUTURE DATE" not in flags:
            flags.append("FUTURE DATE")
    fm["title"] = op["title"]
    meta = op.get("meta", {})
    fm["tags"] = list(meta.get("tags") or [])
    fm["aliases"] = list(meta.get("aliases") or [])
    if "scope" in meta:
        fm["scope"] = meta["scope"]
    pages[pid] = Page(fm, splice([f"# {op['title']}"], body, []))


def _op_restructure(page: Page, op: dict) -> None:
    body = _validate_body(op["body"])
    tree = page.tree()
    if tree.root is None:
        raise PlanError("HEADING_LEVEL", "page has no H1")
    h1 = page.lines[tree.root.start]
    page.lines = splice(page.lines[:tree.root.start] + [h1], body, [])


def _op_update_section(page: Page, op: dict, flags: list[str]) -> None:
    node, (start, end), level = page.resolve(op["section_path"])
    content = _content_lines(op["content"])
    new_heads = check_content_headings(content, level, allow=node is not None)
    if node is not None:
        old = {(h.level, h.text) for h in scan_headings(page.lines[start + 1:end])}
        if old - {(h.level, h.text) for h in new_heads}:
            flags.append("SUBSECTION REMOVED")
        page.lines = splice(page.lines[:start + 1], content, page.lines[end:])
    else:
        page.lines = splice(page.lines[:start], content, page.lines[end:])


def _op_update_section_body(page: Page, op: dict) -> None:
    """Replace only the section's own text (up to its first child heading); children are kept (schema v1.1 / S1)."""
    node, (start, end), _level = page.resolve(op["section_path"])
    content = _content_lines(op["content"])
    check_content_headings(content, 6, allow=False)
    if node is None:  # __preamble__ has no children, so this equals update_section
        page.lines = splice(page.lines[:start], content, page.lines[end:])
        return
    body_end = node.children[0].start if node.children else end
    page.lines = splice(page.lines[:start + 1], content, page.lines[body_end:])


def check_links(text: str, known_ids: set[str], op_id: str) -> None:
    """Every [[target|label]] must name a page_id; fenced and inline code are ignored (schema v1.1 / T4)."""
    bad = sorted(wikilink_targets(text.split("\n")) - set(known_ids))
    if bad:
        raise PlanError("BROKEN_LINK", "wikilink targets must be page_ids, e.g. [[page-id|Title]]; "
                                       f"unknown: {', '.join(bad)}", op_id)


def _op_append(page: Page, op: dict) -> None:
    node, (start, end), _level = page.resolve(op["section_path"])
    content = _content_lines(op["content"])
    check_content_headings(content, 6, allow=False)
    if not content:
        raise PlanError("SCHEMA", "content must not be empty")
    head = page.lines[:end]
    last = next((l for l in reversed(head) if l.strip()), "")
    tight = bool(LIST_ITEM_RE.match(last) and LIST_ITEM_RE.match(content[0]))
    page.lines = splice(head, content, page.lines[end:], tight=tight)


def _op_add_section(page: Page, op: dict) -> None:
    tree = page.tree()
    if tree.errors:
        code, msg = tree.errors[0]
        raise PlanError(code, f"page structure invalid: {msg}")
    parent_path = op["parent_path"]
    parent = tree.root if not parent_path else page.resolve(parent_path)[0]
    level = parent.level + 1
    if level > 6:
        raise PlanError("HEADING_LEVEL", "cannot nest deeper than level 6")
    heading = op["heading"].strip()
    if any(c.text == heading for c in parent.children):
        raise PlanError("PATH_AMBIGUOUS", f"heading {heading!r} already exists under the parent")
    content = _content_lines(op["content"])
    check_content_headings(content, level, allow=True)
    pos = op.get("position", "end")
    if pos == "end":
        at = parent.end
    elif pos == "start":
        at = parent.children[0].start if parent.children else parent.end
    else:
        sib = [c for c in parent.children if c.text == pos["after"]]
        if not sib:
            raise PlanError("PATH_NOT_FOUND", f"sibling {pos['after']!r} not found")
        at = sib[0].end
    block = splice(["#" * level + " " + heading], content, [])
    page.lines = splice(page.lines[:at], block, page.lines[at:])


def _check_meta_values(values: dict, op_id: str | None = None) -> None:
    """title / scope: one non-empty line; tags / aliases: lists of such lines (create_page meta and update_meta)."""
    line = lambda v: isinstance(v, str) and v.strip() and "\n" not in v and "\r" not in v
    for key in ("tags", "aliases"):
        if key in values and (not isinstance(values[key], list) or not all(line(v) for v in values[key])):
            raise PlanError("SCHEMA", f"{key} must be a list of single non-empty lines", op_id)
    for key in ("title", "scope"):
        if key in values and not line(values[key]):
            raise PlanError("SCHEMA", f"{key} must be a single non-empty line", op_id)


def _op_update_meta(pages: dict[str, Page], pid: str, op: dict, flags: list[str]) -> None:
    page = pages[pid]
    s = op["set"]
    _check_meta_values(s)
    others = {k: v for k, v in pages.items() if k != pid}
    names = _names(others)
    cands = ([s["title"]] if "title" in s else []) + list(s.get("aliases") or [])
    dups = sorted({names[n.lower()] for n in cands if n.lower() in names})
    if dups:
        flags.append("POSSIBLE DUPLICATE: " + ", ".join(dups))
    for k, v in s.items():
        page.fm[k] = v
    if "title" in s:
        tree = page.tree()
        if tree.root is None:
            raise PlanError("HEADING_LEVEL", "page has no H1")
        page.lines[tree.root.start] = f"# {s['title']}"


def _op_decision_change(pages, pid, op, flags, detail, src_pages, orig_pages,
                        retracting: set = frozenset()) -> tuple[str, str, str]:
    page = pages[pid]
    if page.page_type != "decision":
        raise PlanError("SCHEMA", f"page {pid!r} is not a decision page")
    old, new = page.fm.get("status"), op["new_status"]
    if (old, new) not in TRANSITIONS:
        raise PlanError("BAD_TRANSITION", f"{old} -> {new} is not allowed")
    detail.append(f"status: {old} -> {new}")
    if new == "superseded":
        by = op["superseded_by"]
        target = pages.get(by)
        if target is None or target.page_type != "decision":
            raise PlanError("UNKNOWN_PAGE", f"superseded_by {by!r} is not a decision page")
        if target.fm.get("status") != "active":
            raise PlanError("BAD_TRANSITION", f"superseded_by {by!r} must be active")
        o, n = page.fm, target.fm
        # show provenance as it was before this plan touched the pages (0B finding T1):
        # earlier ops in the same plan add the superseding source to the old page's sources
        o_src = _provenance(orig_pages[pid].fm if pid in orig_pages else o)
        n_src = _provenance(orig_pages[by].fm if by in orig_pages else n)
        detail.append(f"old: decided_on={o.get('decided_on')} confidence={o.get('date_confidence')} "
                      f"sources={o_src}{_synthetic_note(src_pages, {'sources': o_src})}")
        detail.append(f"new: decided_on={n.get('decided_on')} confidence={n.get('date_confidence')} "
                      f"sources={n_src}{_synthetic_note(src_pages, {'sources': n_src})}")
        if _is_dated(o) and _is_dated(n):
            if n["decided_on"] < o["decided_on"]:
                raise PlanError("TEMPORAL_ORDER", "an older decision cannot supersede a newer one")
        elif "temporal_override" not in op:
            raise PlanError("TEMPORAL_UNCERTAINTY", "a date is unknown or low confidence; user confirmation required")
        else:
            flags.append("TEMPORAL OVERRIDE")
            detail.append(f"override: {op['temporal_override']['note']}")
        page.fm["superseded_by"] = by
    page.fm["status"] = new
    # a source retracted in this same plan is named, not linked (its page is being removed)
    links = [f"{s}（retracted）" if s in retracting else link(s, src_pages[s])
             for s in op["source_ids"]]  # every cited source has a page by now (checked in execute)
    # today() is the apply date; the decision date lives in the note / the superseding page (0B finding T3)
    note = f"- {today()}（applied）status {old} → {new}：{op['history_note']}（{'、'.join(links)}）"
    tree = page.tree()
    hist = [c for c in (tree.root.children if tree.root else []) if c.text == "History"]
    if hist:
        h = hist[0]
        head = page.lines[:h.end]
        last = next((l for l in reversed(head) if l.strip()), "")
        page.lines = splice(head, [note], page.lines[h.end:], tight=bool(LIST_ITEM_RE.match(last)))
    else:
        page.lines = splice(page.lines, ["## History", "", note], [])
    return pid, old, new


def _provenance(fm: dict) -> list[str]:
    """Where a decision was made: origin_sources (v1.1), falling back to sources for older pages."""
    return list(fm.get("origin_sources") or fm.get("sources") or [])


def link(pid: str, page: Page) -> str:
    return f"[[{pid}|{page.title()}]]"


def _synthetic_note(src_pages: dict[str, Page], fm: dict) -> str:
    syn = [s for s in fm.get("sources") or [] if s in src_pages and src_pages[s].fm.get("synthetic") is True]
    return f" SYNTHETIC={syn}" if syn else ""


# ---------------------------------------------------------------------------
# render / apply

def render(ex: Execution, plan_sha: str) -> str:
    out = [f"PLAN SHA256: {plan_sha}", ""]
    all_flags: list[str] = []
    for r in ex.results:
        op = r.op
        label = op["type"].upper().replace("_", " ")
        where = f"page={r.page_id}"
        if "section_path" in op:
            where += "  section=" + " > ".join(op["section_path"])
        if op["type"] == "add_section":
            where += "  new=" + " > ".join([*op["parent_path"], op["heading"]])
        out.append(f"[{op['operation_id']}] {label}  {where}")
        if op.get("source_ids"):
            out.append("  sources +: " + ", ".join(op["source_ids"]))
        if r.flags:
            out.append("  flags: " + " | ".join(r.flags))
            all_flags += [f for f in r.flags if f not in all_flags]
        for d in r.detail:
            out.append("  " + d)
        if op["type"] == "create_page":
            out.append("--- (new page)")
            out += ["+" + l for l in r.after.rstrip("\n").split("\n")]
        else:
            diff = difflib.unified_diff(r.before.splitlines(), r.after.splitlines(),
                                        "current", "proposed", n=3, lineterm="")
            out += list(diff)
        out.append("")
    out.append(f"SUMMARY: created {len(ex.created)}, updated {len(ex.touched)}, "
               f"decision changes {len(ex.decision_changes)}, deleted {len(ex.deleted)}, "
               f"sources retracted {len(ex.retracted)}")
    for w in ex.warnings:
        out.append("WARNING: " + w)
    if all_flags:
        out.append("FLAGS: " + " | ".join(all_flags))
    return "\n".join(out) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sb-", suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    for attempt in range(4):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 3:
                os.unlink(tmp)
                raise
            time.sleep(0.2)


def build_index(pages: dict[str, Page]) -> str:
    out = ["# Index", ""]
    for ptype, header in INDEX_SECTIONS:
        out += [f"## {header}", ""]
        rows = []
        for pid, p in pages.items():
            if p.page_type != ptype:
                continue
            desc = p.fm.get("scope") or ", ".join(p.fm.get("tags") or [])
            line = f"- {link(pid, p)}" + (f" — {desc}" if desc else "")
            if ptype == "decision":
                line += f" ({p.fm.get('status')})"
            rows.append((p.title().lower(), line))
        if rows:
            out += [line for _, line in sorted(rows)] + [""]
    return "\n".join(out)


def build_log_entry(ex: Execution, plan: dict, plan_sha: str) -> str:
    ln = lambda pid: link(pid, ex.pages[pid])
    lines = [f"## [{today()}] apply | {plan['plan_id']}", "",
             f"- plan sha256: {plan_sha}",
             f"- sources: {', '.join(plan['source_ids'])}"]
    if ex.created:
        lines.append("- created: " + ", ".join(ln(p) for p in ex.created))
    if ex.touched:
        lines.append("- updated: " + ", ".join(ln(p) for p in ex.touched))
    for pid, old, new in ex.decision_changes:
        lines.append(f"- decision change: {ln(pid)} {old} → {new}")
    for pid in ex.deleted:
        kind = "source retracted" if pid in ex.retracted else "deleted"
        lines.append(f"- {kind}: {pid}")
    return "\n".join(lines) + "\n"


def apply(vault: Path, plan_path: Path, approve: str, allow_restructure: bool = False) -> Execution:
    if State(vault).managed:
        return apply_managed(vault, plan_path, approve, allow_restructure)
    raw = plan_path.read_bytes()
    plan_sha = sha256_bytes(raw)
    if approve != plan_sha:
        raise PlanError("APPROVAL_MISMATCH", "--approve does not match the plan's current sha256")
    plan = _load_json(raw)
    wiki = Wiki(vault)
    ex = execute(wiki, plan, allow_restructure)
    for pid in ex.created + ex.touched:
        p = ex.pages[pid]
        path = p.path or (wiki.root / TYPE_DIRS[p.page_type] / f"{pid}.md")
        _atomic_write(path, p.text())
        p.path = path
    for pid in ex.deleted:
        ex.deleted_paths[pid].unlink(missing_ok=True)
    _atomic_write(wiki.root / "index.md", normalize(build_index(ex.pages)))
    log = wiki.root / "log.md"
    prev = normalize(log.read_text(encoding="utf-8")) if log.exists() else "# Log\n"
    _atomic_write(log, normalize(prev + "\n" + build_log_entry(ex, plan, plan_sha)))
    pending = vault / "plans" / "pending"
    if plan_path.resolve().parent == pending.resolve():
        dest = vault / "plans" / "applied" / plan_path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plan_path), dest)
    return ex


def _load_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PlanError("SCHEMA", f"invalid JSON: {e}")


# ---------------------------------------------------------------------------
# Stage 1: state, registries, lock, transactions, discover, secret preflight, maintain.
# A vault is "managed" once `init` has created state/sources.json;
# unmanaged vaults (the Stage 0B pilot) keep the plain apply path above.

RAW_TYPES = {"documents": "document", "meetings": "meeting", "sessions": "session"}
SECRET_RULES = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("credential-assignment", re.compile(
        r"(?i)(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?[^\s'\"]{8,}")),
    ("connection-string", re.compile(
        r"(?i)://[^/\s:@]+:[^@\s]{4,}@|(?:password|pwd)=[^;]{4,};|AccountKey=[A-Za-z0-9+/=]{20,}")),
]
STALE_LOCK_SECONDS = 3600
LOG_BASELINE = "state/baselines/_log.md.base"  # log.md as last written by a transaction
GENERATED = ("index", "log")                     # reserved --revert targets for the tool-generated files


def now_iso() -> str:
    return os.environ.get("SB_NOW") or dt.datetime.now().astimezone().isoformat(timespec="seconds")


def file_hash(path: Path) -> str:
    return "sha256:" + sha256_bytes(path.read_bytes())


def scan_secrets(text: str) -> list[tuple[str, int]]:
    """(rule, 1-based line) for every hit; the matched text itself is never returned."""
    hits = []
    for i, line in enumerate(text.split("\n"), 1):
        for name, rx in SECRET_RULES:
            if rx.search(line):
                hits.append((name, i))
    return hits


class State:
    def __init__(self, vault: Path):
        self.vault = vault
        self.dir = vault / "state"

    @property
    def managed(self) -> bool:
        return (self.dir / "sources.json").exists()

    def _load(self, name: str, key: str) -> dict:
        data = json.loads((self.dir / name).read_text(encoding="utf-8"))
        return data[key]

    def sources(self) -> dict:
        return self._load("sources.json", "sources")

    def pages(self) -> dict:
        return self._load("pages.json", "pages")

    def baseline(self, pid: str) -> str | None:
        f = self.dir / "baselines" / f"{pid}.md.base"
        return f.read_text(encoding="utf-8") if f.exists() else None

    def event(self, event: str, **fields) -> None:
        with (self.dir / "events.jsonl").open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"ts": now_iso(), "event": event, **fields}, ensure_ascii=False) + "\n")

    def incomplete_txns(self) -> list[str]:
        out = []
        for m in sorted((self.dir / "txn").glob("*.json")):
            if json.loads(m.read_text(encoding="utf-8")).get("status") in ("begun", "rolling_back"):
                out.append(m.stem)
        return out

    def check_no_incomplete(self) -> None:
        bad = self.incomplete_txns()
        if bad:
            raise PlanError("INCOMPLETE_TXN", f"incomplete transaction(s) {', '.join(bad)}; run rollback first")


def registry_json(key: str, entries: dict) -> str:
    return json.dumps({"schema": 1, key: dict(sorted(entries.items()))}, ensure_ascii=False, indent=2) + "\n"


class Lock:
    def __init__(self, vault: Path, command: str):
        self.path = vault / "state" / ".lock"
        self.command = command

    def __enter__(self):
        info = {"pid": os.getpid(), "host": _host(), "command": self.command, "started_at": now_iso(),
                "epoch": time.time()}
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise PlanError("LOCKED", describe_lock(self.path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


def _host() -> str:
    import socket
    return socket.gethostname()


def _pid_alive(pid: int) -> bool | None:
    """None when liveness cannot be determined safely. Never uses os.kill on Windows (it terminates)."""
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return None


def describe_lock(path: Path) -> str:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"lock file {path} exists but is unreadable; if no other command is running: unlock --force"
    stale = []
    if info.get("host") == _host() and _pid_alive(int(info.get("pid", 0))) is False:
        stale.append("its process is no longer running")
    if time.time() - float(info.get("epoch", time.time())) > STALE_LOCK_SECONDS:
        stale.append("it is more than an hour old")
    hint = f" STALE LOCK? ({'; '.join(stale)}) — if so, run: unlock --force" if stale else ""
    return (f"held by {info.get('command')} (pid {info.get('pid')} on {info.get('host')}, "
            f"since {info.get('started_at')}).{hint}")


class Fingerprint:
    """Hashes of every file a write command may touch, taken right after the lock is acquired: all of wiki/,
    the state registries and baselines, pending plans — plus the raw evidence a plan reads (`inputs`)."""

    def __init__(self, vault: Path, inputs: list[str] = ()):
        self.vault = vault
        files = [p for top in ("wiki", "state/baselines", "plans/pending") for p in (vault / top).rglob("*")
                 if p.is_file()]
        files += [p for p in (vault / "state").glob("*.json")]
        self.hashes = {p.relative_to(vault).as_posix(): file_hash(p) for p in files}
        self.inputs: dict[str, str | None] = {}
        self.add_inputs(inputs)

    def add_inputs(self, paths) -> None:
        """Read-only inputs known only once the plan is loaded (raw evidence) — added before any validation."""
        self.inputs.update({rel: (file_hash(self.vault / rel) if (self.vault / rel).exists() else None)
                            for rel in paths})

    def expected(self, rel: str) -> str | None:
        """The fingerprinted hash of an output path; None = it did not exist and must still not exist."""
        if not rel.startswith(("wiki/", "state/", "plans/")):
            raise RuntimeError(f"write outside the fingerprinted areas: {rel}")  # a programming error
        return self.inputs.get(rel, self.hashes.get(rel))

    def now(self, rel: str) -> str | None:
        path = self.vault / rel
        return file_hash(path) if path.exists() else None

    def changed(self, outputs: set[str], written: dict[str, str | None] | None = None) -> list[str]:
        """Everything that differs from what it should be in the command's read set: the outputs, the raw
        inputs, and the whole wiki — link checks and the index read every page, so a page added, removed or
        edited anywhere in wiki/ invalidates what was validated. `written`: outputs this transaction has already
        written, path -> the hash it wrote (None = deleted); those must still be exactly that."""
        written = written or {}
        wiki_now = {p.relative_to(self.vault).as_posix() for p in (self.vault / "wiki").rglob("*") if p.is_file()}
        wiki_then = {rel for rel in self.hashes if rel.startswith("wiki/")}
        read_set = set(outputs) | set(self.inputs) | wiki_now | wiki_then
        want = lambda rel: written[rel] if rel in written else self.expected_or_input(rel)
        return sorted(rel for rel in read_set if self.now(rel) != want(rel))

    def expected_or_input(self, rel: str) -> str | None:
        return self.inputs[rel] if rel in self.inputs else self.expected(rel)


class Transaction:
    """Snapshot → manifest(begun) → atomic writes → manifest(committed). Crash leaves `begun` for rollback."""

    def __init__(self, state: State, kind: str, **meta):
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.state = state
        self.id = f"txn-{stamp}-{sha256_bytes(os.urandom(8))[:4]}"
        self.meta = {"txn_id": self.id, "kind": kind, **meta}

    def commit(self, outputs: dict[str, str | None], before: "Fingerprint") -> None:
        """outputs: vault-relative path -> new text, or None to delete.
        before: the fingerprint taken when the command started (under the lock), before anything was read.
        Every output and every read-only input it names (raw evidence) must still be exactly as fingerprinted —
        a file that did not exist then must still not exist — or nothing is written (PLAN_STALE). There is no
        per-output bookkeeping for a caller to forget: the fingerprint covers every file the tool may write."""
        vault, snap = self.state.vault, self.state.dir / "snapshots" / self.id
        stale = before.changed(set(outputs))
        if stale:
            raise PlanError("PLAN_STALE", "changed on disk after validation, nothing written: " + ", ".join(stale))
        # (checked again per file right before it is replaced, and over the whole read set once everything is
        # written: an edit anywhere in that window aborts and undoes the transaction instead of being kept over)
        files = []
        outputs = {rel: text for rel, text in outputs.items() if text is not None or (vault / rel).exists()}
        for rel, text in outputs.items():
            path = vault / rel
            entry = {"path": rel, "action": "create" if not path.exists() else ("delete" if text is None else "modify")}
            if path.exists():
                entry["before"] = file_hash(path)
                (snap / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, snap / rel)
            if text is not None:
                entry["after"] = "sha256:" + sha256_bytes(text.encode("utf-8"))
            files.append(entry)
        manifest = self.state.dir / "txn" / f"{self.id}.json"
        record = {**self.meta, "status": "begun", "started_at": now_iso(), "files": files}
        _atomic_write(manifest, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        self.state.event("txn_begin", txn_id=self.id, kind=self.meta["kind"])
        crash_after = int(os.environ.get("SB_CRASH_AFTER", "-1"))
        written: list[dict] = []
        for n, (rel, text) in enumerate(outputs.items()):
            if n == crash_after:
                raise RuntimeError(f"simulated crash after {n} writes (SB_CRASH_AFTER)")
            if before.now(rel) != before.expected(rel):  # last look right before replacing this file
                self._abort(record, manifest, written, [rel])
            path = vault / rel
            if text is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, text)
            written.append(files[n])
        # final check over the whole read set, the loop included: what this transaction wrote is still exactly
        # that, and everything else it read (raw evidence, the other pages, the set of pages) is unchanged
        stale = before.changed(set(outputs), {f["path"]: f.get("after") for f in written})
        if stale:
            self._abort(record, manifest, written, stale)
        record.update(status="committed", committed_at=now_iso())
        _atomic_write(manifest, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        self.state.event("txn_commit", txn_id=self.id, kind=self.meta["kind"])

    def _abort(self, record: dict, manifest: Path, written: list[dict], stale: list[str]) -> None:
        """A concurrent edit was seen mid-commit: put back what this commit already wrote, mark it `aborted`
        (not incomplete: nothing of it remains) and raise PLAN_STALE. If a written file was itself changed in
        the meantime, it is left alone and the manifest stays `begun` for a manual rollback."""
        snap = self.state.dir / "snapshots" / self.id
        for f in reversed(written):
            path = self.state.vault / f["path"]
            if (file_hash(path) if path.exists() else None) != f.get("after"):
                raise PlanError("PLAN_STALE", f"concurrent edits during commit ({', '.join(stale)}, {f['path']}); "
                                              f"run rollback {self.id}")
            if f["action"] == "create":
                path.unlink()
            else:
                _atomic_write_bytes(path, (snap / f["path"]).read_bytes())
        record.update(status="aborted", aborted_at=now_iso(), stale=stale)
        _atomic_write(manifest, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        self.state.event("txn_abort", txn_id=self.id, stale=stale)
        raise PlanError("PLAN_STALE", "changed on disk during commit, nothing written: " + ", ".join(stale))


def rollback(vault: Path, txn_id: str) -> list[str]:
    state = State(vault)
    manifest = state.dir / "txn" / f"{txn_id}.json"
    if not manifest.exists():
        raise PlanError("UNKNOWN_TXN", f"no transaction {txn_id}")
    with Lock(vault, f"rollback {txn_id}"):
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if record["status"] in ("rolled_back", "aborted"):
            raise PlanError("UNKNOWN_TXN", f"{txn_id} is {record['status']}: nothing of it is on disk")
        others = [t for t in state.incomplete_txns() if t != txn_id]
        if others:
            raise PlanError("INCOMPLETE_TXN", f"finish the incomplete transaction(s) {', '.join(others)} first")
        # committed (Q6): every file must still be exactly as this transaction left it.
        # begun (crashed apply) / rolling_back (interrupted rollback): each file on its own —
        #   at `after`  → written by the transaction, not yet restored → restore
        #   at `before` → never written, or already restored → skip
        # anything else is a later change by someone → ROLLBACK_CONFLICT; nothing is restored.
        strict = record["status"] == "committed"
        todo, conflicts = [], []
        for f in record["files"]:
            path = vault / f["path"]
            current = file_hash(path) if path.exists() else None
            if current == f.get("after") and (strict or current != f.get("before")):
                todo.append(f)
            elif current != f.get("before") or strict:
                conflicts.append(f)
        if conflicts:
            raise PlanError("ROLLBACK_CONFLICT", "changed after this transaction, nothing restored: "
                            + ", ".join(f["path"] for f in conflicts))
        # the snapshot is the only copy of the old content: it must be exactly what the manifest recorded
        snap = state.dir / "snapshots" / txn_id
        bad = [f["path"] for f in todo if f["action"] != "create"
               and (not (snap / f["path"]).exists() or file_hash(snap / f["path"]) != f.get("before"))]
        if bad:
            raise PlanError("ROLLBACK_CONFLICT", "snapshot missing or not the recorded content, nothing restored: "
                            + ", ".join(bad))
        # the rollback is itself crash-safe: mark it first, so an interruption stays an incomplete transaction
        # (writes blocked) and running rollback again finishes it (restored files are then at `before`)
        record.update(status="rolling_back", rollback_started_at=record.get("rollback_started_at") or now_iso())
        _atomic_write(manifest, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        crash_after = int(os.environ.get("SB_ROLLBACK_CRASH_AFTER", "-1"))
        restored = []
        for n, f in enumerate(todo):
            if n == crash_after:
                raise RuntimeError(f"simulated crash after {n} restores (SB_ROLLBACK_CRASH_AFTER)")
            path = vault / f["path"]
            if f["action"] == "create":
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, (snap / f["path"]).read_bytes())
            restored.append(f"{f['action']:<7} {f['path']}")
        record.update(status="rolled_back", rolled_back_at=now_iso())
        _atomic_write(manifest, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        state.event("rollback", txn_id=txn_id, files=len(restored))
    return restored


# --- init / discover ---------------------------------------------------------

def init_vault(vault: Path) -> None:
    for d in ["raw/documents", "raw/meetings", "raw/sessions", "plans/pending", "plans/applied", "plans/rejected",
              "state/baselines", "state/snapshots", "state/txn", "output",
              *[f"wiki/{d}" for d in TYPE_DIRS.values()]]:
        (vault / d).mkdir(parents=True, exist_ok=True)
    state = State(vault)
    if state.managed:
        raise PlanError("SCHEMA", f"{vault} is already initialised")
    _atomic_write(state.dir / "pages.json", registry_json("pages", {}))
    _atomic_write(state.dir / "events.jsonl", "")
    for name, text in (("index.md", build_index({})), ("log.md", "# Log\n")):
        if not (vault / "wiki" / name).exists():
            _atomic_write(vault / "wiki" / name, normalize(text))
    _atomic_write(vault / LOG_BASELINE, (vault / "wiki" / "log.md").read_text(encoding="utf-8"))
    _atomic_write(state.dir / "sources.json", registry_json("sources", {}))  # last: marks the vault as managed
    state.event("init")


def new_source_id(stype: str, date: str, stem: str, taken) -> str:
    """The one constructor of source identities: `<type>-<yyyy-mm-dd|undated>-<slug>[-n]`, unique among
    `taken`, and always a valid page_id (≤ 80 characters including the -n suffix: the slug is cut to fit)."""
    prefix = f"{stype}-{date if date != 'unknown' else 'undated'}-"
    slug, n = _slug(stem), 1
    while True:
        suffix = f"-{n}" if n > 1 else ""
        sid = prefix + (slug[:80 - len(prefix) - len(suffix)].strip("-") or "source") + suffix
        if sid not in taken:
            if not valid_page_id(sid):
                raise PlanError("SCHEMA", f"cannot build a valid source_id from {stem!r} ({sid!r})")
            return sid
        n += 1


def _slug(stem: str) -> str:
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}[-_ ]*", "", stem)
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60].strip("-") or "source"


def _resolve_date(path: Path, text: str) -> tuple[str, str, str]:
    """Main spec §28 levels 1–3: structured metadata → high; filename → medium; clear body date → medium."""
    for line in text.split("\n")[:40]:
        m = re.match(r"^\W*(?:date|started|created)\W*:\W*(\d{4}-\d{2}-\d{2})", line.strip(), re.I)
        if m and valid_date(m.group(1)):  # 2026-99-99 is not a date: fall through to the next level
            return m.group(1), "high", f"structured metadata: {line.strip()[:60]}"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if m and valid_date(m.group(1)):
        return m.group(1), "medium", f"filename: {path.name}"
    # level 3: one unambiguous date in the title area. Dates deeper in the body are usually deadlines or
    # references, not the document's own date; those are for the ingest plan (level 4) to judge.
    top = [l.strip() for l in re.sub(r"<[^>]+>", "\n", text).split("\n") if l.strip()][:10]
    found = {}
    for line in top:
        for d in _body_dates(line):
            found.setdefault(d, line)
    if len(found) == 1:
        (d, line), = found.items()
        return d, "medium", f"body text (title area): {line[:60]}"
    if found:
        return "unknown", "low", f"several dates in the title area ({', '.join(sorted(found))}); the plan decides"
    return "unknown", "low", "no date in metadata, filename or title area (an ingest plan may propose one)"


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), 1)}


def _body_dates(line: str) -> list[str]:
    """ISO `2026-09-18`, `September 18, 2026` / `Sep 18 2026`, `2026年9月18日` → YYYY-MM-DD (valid dates only)."""
    out = []
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", line):
        out.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in re.finditer(r"\b([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})\b", line):
        if m.group(1).lower() in MONTHS:
            out.append((int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))))
    for m in re.finditer(r"(\d{4})年(\d{1,2})月(\d{1,2})日", line):
        out.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    valid = []
    for y, mo, d in out:
        try:
            valid.append(dt.date(y, mo, d).isoformat())
        except ValueError:
            pass
    return valid


# registries written before `source_date_origin` existed: the origin is inferred once from the basis wording
LEGACY_DISCOVER_BASES = ("structured metadata:", "filename:", "body text (title area):", "no date in", "several dates")


def _raw_files(vault: Path) -> list[Path]:
    out = []
    for sub in RAW_TYPES:
        for f in sorted((vault / "raw" / sub).rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                # `x.pdf.txt` is the text companion of `x.pdf`, not a source of its own
                if f.suffix == ".txt" and f.with_suffix("").suffix and f.with_suffix("").exists():
                    continue
                out.append(f)
    return out


def source_hash(f: Path) -> str:
    """Evidence hash of a raw source. A `x.pdf.txt` companion is what the agent reads and what the secret
    preflight scans, so it is part of the evidence: changing, adding or removing it changes the hash."""
    companion = f.with_name(f.name + ".txt")
    if not companion.exists():
        return file_hash(f)
    return "sha256:" + sha256_bytes(f"{file_hash(f)}\ncompanion {file_hash(companion)}\n".encode("ascii"))


TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text", ".html", ".htm", ".json", ".csv", ".tsv", ".log",
                 ".xml", ".yaml", ".yml", ".srt", ".vtt", ".rst", ".org"}


def _scan_text(f: Path) -> str | None:
    """The text the secret preflight scans (and the agent reads), or None when there is none to scan.
    Only known text formats are read directly; PDF, DOCX and anything else need a `<name>.txt` companion.
    Whatever is read must be valid UTF-8 without NUL bytes, or it is not scannable either."""
    companion = f.with_name(f.name + ".txt")
    if companion.exists():
        target = companion
    elif f.suffix.lower() in TEXT_SUFFIXES:
        target = f
    else:
        return None
    data = target.read_bytes()
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def discover(vault: Path, link: tuple[str, str] | None = None) -> list[str]:
    state = State(vault)
    report: list[str] = []
    with Lock(vault, "discover"):
        state.check_no_incomplete()
        sources = state.sources()
        for rec in sources.values():  # one-time migration of older registries (explicit origin from now on)
            rec.setdefault("source_date_origin", "discover" if str(rec.get("source_date_basis", "")).startswith(
                LEGACY_DISCOVER_BASES) else "plan")
        by_path = {rec["path"]: sid for sid, rec in sources.items()}
        seen: set[str] = set()
        unmatched: list[Path] = []
        if link:
            rel, sid = link
            rel = rel.replace("\\", "/")
            if rel.startswith("./"):
                rel = rel[2:]
            # evidence comes only from raw/: the target must be a file discover itself would register
            # (no absolute paths, no `..`, nothing outside the vault, no .pdf.txt companion)
            canonical = {f.relative_to(vault).as_posix() for f in _raw_files(vault)}
            if rel not in canonical:
                raise PlanError("UNKNOWN_SOURCE", f"cannot link {rel!r}: not a raw source file of this vault "
                                                  "(expected e.g. raw/meetings/<file>)")
            if sid not in sources:
                raise PlanError("UNKNOWN_SOURCE", f"cannot link {rel} to unknown source {sid!r}")
            # a link confirms a move: the source must have lost its file (main spec §29: a copy is not a move)
            if sources[sid]["status"] != "missing" or (vault / sources[sid]["path"]).exists():
                raise PlanError("LINK_CONFLICT", f"{sid!r} is {sources[sid]['status']} and its file "
                                                 f"{sources[sid]['path']} still exists; only a missing source "
                                                 "can be linked to a new path")
            # the earlier discover registered the new path provisionally; that record is replaced by the link
            provisional = [s for s, r in sources.items() if r["path"] == rel and s != sid]
            used = [s for s in provisional if sources[s].get("ingested_hash") or sources[s]["status"] == "retracted"]
            if used:
                raise PlanError("LINK_CONFLICT", f"{rel} is already source {used[0]!r}, which has been ingested; "
                                                 "retract it first or keep both")
            for s in provisional:
                del sources[s]
                report.append(f"DROPPED      {s} (provisional record for {rel}, replaced by the link)")
            # the source is `missing`, so the scan below settles it at its new path either way
            # (CHANGED or FOUND AGAIN → _settle: type, date, status, preflight)
            sources[sid]["path"] = rel
            by_path = {r["path"]: s for s, r in sources.items()}
            report.append(f"LINKED       {rel} -> {sid}")
        files = _raw_files(vault)
        for f in files:
            rel = f.relative_to(vault).as_posix()
            h = source_hash(f)
            if rel in by_path:
                sid = by_path[rel]
                seen.add(sid)
                rec = sources[sid]
                if rec["status"] == "retracted":
                    continue
                if rec["content_hash"] != h:
                    rec["content_hash"] = h
                    report.append(f"CHANGED      {sid}")
                    _settle(state, vault, sid, rec, f, report)
                elif rec["status"] == "missing":
                    report.append(f"FOUND AGAIN  {sid} ({rel})")
                    _settle(state, vault, sid, rec, f, report)
                continue
            unmatched.append(f)
        missing = {sid for sid, rec in sources.items() if sid not in seen and rec["status"] != "retracted"
                   and not (vault / rec["path"]).exists()}
        # moves are matched over the whole missing × new set at once: a move is automatic only when one missing
        # source and one new file pair up exactly (degree 1 on both sides); anything else needs --link
        hashes = {f: source_hash(f) for f in unmatched}
        files_per_hash: dict[str, list[str]] = {}
        for f, fh in hashes.items():
            files_per_hash.setdefault(fh, []).append(f.relative_to(vault).as_posix())
        for f in unmatched:
            rel, h = f.relative_to(vault).as_posix(), hashes[f]
            moved = sorted(sid for sid in missing if sources[sid]["content_hash"] == h)
            rivals = [r for r in files_per_hash[h] if r != rel] if moved else []
            # a registered source that still has this content on disk makes the file possibly its copy
            # (main spec §29: a copy is never re-pointed), so a move cannot be decided either
            twins = sorted(s for s, r in sources.items() if s not in missing and r["content_hash"] == h
                           and (vault / r["path"]).exists()) if moved else []
            if len(moved) == 1 and not rivals and not twins:
                sid = moved[0]
                missing.discard(sid)
                report.append(f"MOVED        {sid} -> {rel}")
                sources[sid]["path"] = rel
                _settle(state, vault, sid, sources[sid], f, report)
                continue
            # no unique move (several missing sources or several new files share this content): a new source
            text = _scan_text(f) or ""
            date, conf, basis = _resolve_date(f, text)
            stype = RAW_TYPES[f.relative_to(vault / "raw").parts[0]]
            sid = new_source_id(stype, date, f.stem, sources)
            rec = {"path": rel, "source_type": stype, "content_hash": h, "source_date": date,
                   "source_date_confidence": conf, "source_date_basis": basis, "source_date_origin": "discover",
                   "first_seen_at": now_iso(),
                   "last_ingested_at": None, "ingested_hash": None, "status": "new"}
            sources[sid] = rec
            dup = [s for s, r in sources.items() if s != sid and r["content_hash"] == h and s not in missing]
            report.append(f"NEW          {sid}" + (f"  (DUPLICATE CONTENT of {', '.join(dup)})" if dup else ""))
            if moved and (len(moved) > 1 or rivals or twins):
                report.append(f"AMBIGUOUS_MOVE {rel}: same content as missing {', '.join(moved)}"
                              + (f" and as new {', '.join(rivals)}" if rivals else "")
                              + (f" and as existing {', '.join(twins)} (a copy?)" if twins else "")
                              + f"; registered provisionally as {sid}. If it is the moved file: "
                                f"discover --link {rel} <source_id>")
            _preflight(state, sid, rec, f, report)
        for sid in sorted(missing):
            if sources[sid]["status"] != "missing":
                sources[sid]["status"] = "missing"
                report.append(f"MISSING      {sid} ({sources[sid]['path']})")
        new_ids = [line.split()[1] for line in report if line.startswith("NEW")]
        if missing and new_ids:
            report.append(f"POSSIBLE MOVED+MODIFIED: missing {sorted(missing)} / new {new_ids}. "
                          "If one is the other, run: discover --link <new-path> <source_id>")
        _atomic_write(state.dir / "sources.json", registry_json("sources", sources))
        state.event("discover", changes=len(report))
    return report or ["no changes"]


def _registry_fm(rec: dict) -> dict:
    """Source-page frontmatter owned by the registry (discover): where the evidence is, its type and date."""
    return {"source_type": rec["source_type"], "raw_path": rec["path"], "source_date": rec["source_date"],
            "date_confidence": rec["source_date_confidence"], "date_basis": rec["source_date_basis"]}


def _sync_source_fm(fm: dict, rec: dict) -> list[str]:
    """Bring a source page's registry-owned frontmatter up to date; returns flags naming what changed."""
    want, flags = _registry_fm(rec), []
    if fm.get("raw_path") != want["raw_path"]:
        flags.append(f"SOURCE MOVED: {fm.get('raw_path')} -> {want['raw_path']}")
    if fm.get("source_type") != want["source_type"]:
        flags.append(f"SOURCE TYPE CHANGED: {fm.get('source_type')} -> {want['source_type']}")
    if (fm.get("source_date"), fm.get("date_confidence")) != (want["source_date"], want["date_confidence"]):
        flags.append(f"SOURCE DATE CHANGED: {fm.get('source_date')} -> {want['source_date']}")
    fm.update(want)
    return flags


def _settle(state: State, vault: Path, sid: str, rec: dict, f: Path, report: list[str]) -> None:
    """The one source-state transition, for changed content, a move, a link and a file that reappears:
    status from the ingest history, registry metadata from the (possibly new) path and content, and the
    secret preflight again — a BLOCK is lifted only by a scan of the current content, never by a rename."""
    rec["status"] = ("ingested" if rec.get("ingested_hash") == rec["content_hash"] else
                     "changed" if rec.get("ingested_hash") else "new")
    stype = RAW_TYPES[f.relative_to(vault / "raw").parts[0]]
    if rec["source_type"] != stype:
        report.append(f"SOURCE_TYPE_CHANGED {sid}: {rec['source_type']} -> {stype} (moved to raw/{f.parent.name}/)")
        rec["source_type"] = stype
    _redate(rec, f, sid, report)
    _preflight(state, sid, rec, f, report)


def _redate(rec: dict, f: Path, sid: str, report: list[str]) -> None:
    """Changed raw content → resolve the date again (levels 1–3). The source_id stays; the source page picks up
    the new date at the next apply that touches it. A date an ingest plan supplied (level 4) is kept unless the
    new content itself states a date."""
    date, conf, basis = _resolve_date(f, _scan_text(f) or "")
    if date == "unknown" and rec.get("source_date_origin") == "plan":
        return
    old = (rec["source_date"], rec["source_date_confidence"])
    if (date, conf) != old:
        report.append(f"SOURCE_DATE_CHANGED {sid}: {old[0]} ({old[1]}) -> {date} ({conf}); {basis}")
    rec.update(source_date=date, source_date_confidence=conf, source_date_basis=basis, source_date_origin="discover")


def _preflight(state: State, sid: str, rec: dict, f: Path, report: list[str]) -> None:
    text = _scan_text(f)
    if text is None:
        # no PASS without a scan: a binary is usable only once a text companion has been checked
        rec["status"] = "blocked_unscannable"
        rec.pop("secret_hits", None)
        report.append(f"BLOCKED      {sid}: binary file without a .txt companion; secret preflight not possible. "
                      f"Add {f.name}.txt (extracted text) and run discover again")
        state.event("unscannable_blocked", source_id=sid)
        return
    hits = scan_secrets(text)
    if hits:
        rec["status"] = "blocked_secret"
        rec["secret_hits"] = [f"{rule}@line{line}" for rule, line in hits]
        report.append(f"BLOCKED      {sid}: possible secret ({', '.join(rec['secret_hits'][:5])}); "
                      "sanitise the raw file and run discover again")
        state.event("secret_blocked", source_id=sid, hits=rec["secret_hits"])
    else:
        rec.pop("secret_hits", None)  # the caller already reset the status (new / changed) for this content


# --- managed validation (called from execute) ----------------------------------

def _managed_source_checks(state: State, plan: dict, ops: list[dict]) -> dict:
    sources = state.sources()
    sv = plan.get("source_versions")
    if not isinstance(sv, dict) or set(sv) != set(plan["source_ids"]):
        raise PlanError("SCHEMA", "source_versions must map every plan source_id to its content_hash "
                                  "(get it with: second_brain.py source <source_id>)")
    retracting = {op["page_id"] for op in ops if op["type"] == "retract_source"}
    for sid in plan["source_ids"]:
        rec = sources.get(sid)
        if rec is None:
            raise PlanError("UNKNOWN_SOURCE", f"{sid!r} is not registered; run discover")
        if rec["status"] == "retracted":
            raise PlanError("SOURCE_NOT_READY", f"{sid!r} is retracted")
        if sid in retracting:
            continue  # a retraction must work even if the raw file changed or disappeared
        if rec["status"] in ("blocked_secret", "blocked_unscannable", "missing"):
            raise PlanError("SOURCE_NOT_READY", f"{sid!r} is {rec['status']}")
        path = state.vault / rec["path"]
        current = source_hash(path) if path.exists() else None
        if current != rec["content_hash"]:
            raise PlanError("SOURCE_STALE", f"raw file of {sid!r} changed since discover; run discover")
        if sv[sid] != current:
            raise PlanError("SOURCE_STALE", f"plan was written against another version of {sid!r}")
    for op in ops:
        if op["type"] == "create_page" and op["page_type"] == "source":
            rec = sources.get(op["page_id"])
            if rec is None:
                raise PlanError("UNKNOWN_SOURCE", f"{op['page_id']!r} is not registered", op["operation_id"])
            if rec["status"] != "new":
                raise PlanError("SOURCE_ALREADY_INGESTED", f"{op['page_id']!r} is {rec['status']}; "
                                "a changed source is re-ingested by updating its source page", op["operation_id"])
            s = op["source"]
            if s["raw_path"] != rec["path"] or s["source_type"] != rec["source_type"]:
                raise PlanError("SCHEMA", "source.raw_path / source_type must match the registry", op["operation_id"])
            # AGENTS.md §4: a plan may refine an `unknown` date from the body text, never weaken what discover found
            rank = {"low": 0, "medium": 1, "high": 2}
            if rec["source_date"] != "unknown":
                if s["source_date"] != rec["source_date"] or \
                        rank[s["date_confidence"]] < rank[rec["source_date_confidence"]]:
                    raise PlanError("DATE_DOWNGRADE", f"discover found {rec['source_date']} "
                                    f"({rec['source_date_confidence']}); the plan must keep it "
                                    f"(got {s['source_date']} / {s['date_confidence']})", op["operation_id"])
            elif s["date_confidence"] == "high":
                raise PlanError("DATE_DOWNGRADE", "a date read from the body text is medium or low, not high",
                                op["operation_id"])
    return sources


def _section_text(page: Page | None, path: list[str]) -> str | None:
    if page is None:
        return None
    try:
        node, (s, e), _ = page.resolve(path)
    except PlanError:
        return None
    return "\n".join(page.lines[s:e])


def _human_check(state: State, pages_reg: dict, orig: dict[str, Page], pid: str, op: dict,
                 flags: list[str], detected: set[str]) -> None:
    if pid not in orig or op["type"] == "create_page":
        return
    rec = pages_reg.get(pid)
    base_text = state.baseline(pid) if rec else None
    cur = orig[pid]
    if base_text is not None and rec and normalize(base_text) == normalize(cur.raw or cur.text()):
        if rec.get("human_touched"):
            flags.insert(0, "HUMAN-MAINTAINED PAGE")
        return
    detected.add(pid)
    flags.insert(0, "HUMAN-MAINTAINED PAGE")
    base = Page.parse(base_text) if base_text and base_text.strip() else None
    t = op["type"]
    if t == "add_section" and base is not None:
        return  # a new section does not touch the human's text
    if t in ("update_meta", "decision_change"):
        code, changed = "HUMAN_EDITED_META", base is None or base.fm != cur.fm
        diff_a = dump_frontmatter(base.fm) if base else ""
        diff_b = dump_frontmatter(cur.fm)
    elif t in ("restructure_page", "delete_page", "retract_source", "add_section"):
        # add_section only lands here for an external page (empty baseline): every change needs human_override
        code, changed = "HUMAN_EDITED_SECTION", True
        diff_a, diff_b = base_text or "", cur.raw or cur.text()
    else:
        a, b = _section_text(base, op["section_path"]), _section_text(cur, op["section_path"])
        code, changed = "HUMAN_EDITED_SECTION", a != b
        diff_a, diff_b = a or "", b or ""
    if not changed:
        return
    if isinstance(op.get("human_override"), dict):
        flags.append("HUMAN EDIT OVERRIDE")
        return
    diff = "\n".join(list(difflib.unified_diff(diff_a.splitlines(), diff_b.splitlines(), "baseline", "current",
                                               n=1, lineterm=""))[:40])
    where = "unregistered page (run maintain --fix)" if rec is None else "edited outside a plan"
    raise PlanError(code, f"{pid!r}: {where}; ask the user before overriding.\n{diff}")


def _plan_strings(value, path: str):
    """Every string inside an operation (nested meta / set / lists / override notes included), with its path."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _plan_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _plan_strings(v, f"{path}[{i}]")


def _scan_plan_secrets(plan: dict) -> None:
    """Every string in the plan: operation fields become wiki text, and the whole file (summary included)
    is kept in plans/applied/ as history."""
    top = {k: v for k, v in plan.items() if k != "operations"}
    for op_id, value in [(None, top)] + [(op.get("operation_id"), op) for op in plan["operations"]]:
        for key, text in _plan_strings(value, ""):
            hits = scan_secrets(text)
            if hits:
                rules = sorted({r for r, _ in hits})
                raise PlanError("SECRET_IN_PLAN", f"possible secret in {key} ({', '.join(rules)})", op_id)


# --- managed apply ----------------------------------------------------------------

def apply_managed(vault: Path, plan_path: Path, approve: str, allow_restructure: bool = False) -> Execution:
    state = State(vault)
    with Lock(vault, f"apply-plan {plan_path.name}"):
        state.check_no_incomplete()
        before = Fingerprint(vault)  # first, before the plan or any page is read
        raw = plan_path.read_bytes()
        plan_sha = sha256_bytes(raw)
        if approve != plan_sha:
            raise PlanError("APPROVAL_MISMATCH", "--approve does not match the plan's current sha256")
        plan = _load_json(raw)
        pending_rel, applied_rel = _plan_lifecycle_paths(vault, plan_path, plan)
        before.add_inputs(_evidence_paths(state.sources(), plan))
        wiki = Wiki(vault)
        ex = execute(wiki, plan, allow_restructure, state)
        sources, pages_reg = state.sources(), state.pages()
        outputs: dict[str, str | None] = {}
        detected: list[str] = []
        txn = Transaction(state, "apply-plan", plan_id=plan["plan_id"], plan_sha256=plan_sha)
        for pid in ex.created + ex.touched:
            p = ex.pages[pid]
            path = p.path or (wiki.root / TYPE_DIRS[p.page_type] / f"{pid}.md")
            rel = path.relative_to(vault).as_posix()
            text = p.text()
            outputs[rel] = text
            outputs[f"state/baselines/{pid}.md.base"] = text
            old = pages_reg.get(pid, {})
            pages_reg[pid] = {"path": rel, "page_type": p.page_type, "origin": old.get("origin", "plan"),
                              "human_touched": bool(old.get("human_touched")) or pid in ex.human_detected,
                              "baseline_hash": sha256_text(text), "last_txn": txn.id}
            if pid in ex.human_detected and not old.get("human_touched"):
                detected.append(pid)
        for pid in ex.deleted:
            outputs[ex.deleted_paths[pid].relative_to(vault).as_posix()] = None
            outputs[f"state/baselines/{pid}.md.base"] = None
            pages_reg.pop(pid, None)
        for sid in ex.retracted:
            reason = next(r.op["reason"] for r in ex.results if r.op["type"] == "retract_source" and r.page_id == sid)
            sources[sid].update(status="retracted", retracted_at=now_iso(), retract_reason=reason)
        for sid in ex.ingested:  # decided by execute on the final state (_settle_ingest)
            rec, sp = sources[sid], ex.pages[sid].fm
            dated = (sp.get("source_date", rec["source_date"]), sp.get("date_confidence", rec["source_date_confidence"]),
                     sp.get("date_basis", rec["source_date_basis"]))
            if dated != (rec["source_date"], rec["source_date_confidence"], rec["source_date_basis"]):
                rec["source_date_origin"] = "plan"  # level 4: the ingest plan refined the date
            rec.update(status="ingested", ingested_hash=rec["content_hash"], last_ingested_at=now_iso(),
                       source_date=dated[0], source_date_confidence=dated[1], source_date_basis=dated[2])
        outputs["wiki/index.md"] = normalize(build_index(ex.pages))
        log = wiki.root / "log.md"
        prev = normalize(log.read_text(encoding="utf-8")) if log.exists() else "# Log\n"
        outputs["wiki/log.md"] = normalize(prev + "\n" + build_log_entry(ex, plan, plan_sha))
        outputs[LOG_BASELINE] = outputs["wiki/log.md"]
        outputs["state/sources.json"] = registry_json("sources", sources)
        outputs["state/pages.json"] = registry_json("pages", pages_reg)
        # archiving is part of the transaction: the plan is in applied/ exactly when its writes are
        outputs[applied_rel] = raw.decode("utf-8")
        outputs[pending_rel] = None
        txn.commit(outputs, before)
        ex.txn_id = txn.id
        for pid in detected:  # only once the transaction is committed: a PLAN_STALE apply leaves no trace
            state.event("human_edit_detected", page_id=pid, txn_id=txn.id)
    return ex


def _evidence_paths(sources: dict, plan: dict) -> list[str]:
    """Raw files (and their .txt companions, present or not) the plan's source_versions bind: read-only inputs
    of the apply transaction. A retraction is exempt — its raw file may already be gone (§13)."""
    retracting = {op["page_id"] for op in plan["operations"] if op.get("type") == "retract_source"}
    paths = []
    for sid in plan["source_ids"]:
        if sid in sources and sid not in retracting:
            paths += [sources[sid]["path"], sources[sid]["path"] + ".txt"]
    return paths


def _plan_lifecycle_paths(vault: Path, plan_path: Path, plan: dict | None = None) -> tuple[str, str]:
    """Only plans/pending/<plan_id>.json can be applied or rejected; applied/ and rejected/ are write-once."""
    pending = (vault / "plans" / "pending").resolve()
    if plan_path.resolve().parent != pending:
        raise PlanError("PLAN_NOT_PENDING", f"{plan_path} is not in plans/pending/; applied and rejected plans "
                                            "are history and cannot be run again")
    if plan is not None and plan_path.name != f"{plan.get('plan_id')}.json":
        raise PlanError("SCHEMA", f"file name must be <plan_id>.json ({plan.get('plan_id')}.json), got {plan_path.name}")
    for folder in ("applied", "rejected"):
        if (vault / "plans" / folder / plan_path.name).exists():
            raise PlanError("PLAN_EXISTS", f"plans/{folder}/{plan_path.name} already exists; use a new plan_id")
    return f"plans/pending/{plan_path.name}", f"plans/applied/{plan_path.name}"


def reject_plan(vault: Path, plan_path: Path, reason: str) -> Path:
    state = State(vault)
    if not state.managed:
        return _reject(state, plan_path, reason)
    # same write lock as apply-plan: a plan must never end up applied to the wiki but filed under rejected/
    with Lock(vault, f"reject-plan {plan_path.name}"):
        state.check_no_incomplete()  # an interrupted apply may still move this plan (rollback restores pending/)
        return _reject(state, plan_path, reason)


def _reject(state: State, plan_path: Path, reason: str) -> Path:
    vault = state.vault
    if not plan_path.exists():
        raise PlanError("SCHEMA", f"{plan_path} does not exist (already applied or rejected?)")
    if state.managed:  # rejected/ holds plans only: plans/pending/<plan_id>.json, like apply
        plan = _load_json(plan_path.read_bytes())
        if not isinstance(plan, dict) or not isinstance(plan.get("plan_id"), str) \
                or not PLAN_ID_RE.match(plan["plan_id"]):
            raise PlanError("SCHEMA", f"{plan_path.name} is not a plan (no valid plan_id); remove it by hand")
        _plan_lifecycle_paths(vault, plan_path, plan)
    dest = vault / "plans" / "rejected" / plan_path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    sha = sha256_bytes(plan_path.read_bytes())
    shutil.move(str(plan_path), dest)
    if state.managed:
        state.event("plan_rejected", plan=plan_path.name, plan_sha256=sha, reason=reason)
    return dest


# --- status / source / maintain -------------------------------------------------------

def cmd_status(vault: Path) -> str:
    state = State(vault)
    if not state.managed:
        return "not a managed vault (run init)\n"
    groups: dict[str, list[str]] = {}
    for sid, rec in state.sources().items():
        groups.setdefault(rec["status"], []).append(sid)
    out = ["sources:"]
    for status in ("new", "changed", "ingested", "blocked_secret", "blocked_unscannable", "missing", "retracted"):
        ids = sorted(groups.get(status, []))
        out.append(f"  {status:<19} {len(ids):>3}" + (f"  {', '.join(ids)}" if ids and status != "ingested" else ""))
    pending = sorted(p.name for p in (vault / "plans" / "pending").glob("*.json"))
    out.append(f"pending plans: {len(pending)}" + (f"  {', '.join(pending)}" if pending else ""))
    human = sorted(pid for pid, rec in state.pages().items() if rec.get("human_touched"))
    out.append(f"pages: {len(state.pages())}  human-maintained: {len(human)}" + (f"  {', '.join(human)}" if human else ""))
    inc = state.incomplete_txns()
    out.append("incomplete transactions: " + (", ".join(inc) + "  → run rollback" if inc else "none"))
    lock = state.dir / ".lock"
    out.append("lock: " + (describe_lock(lock) if lock.exists() else "free"))
    return "\n".join(out) + "\n"


def cmd_source(vault: Path, sid: str) -> str:
    rec = State(vault).sources().get(sid)
    if rec is None:
        raise PlanError("UNKNOWN_SOURCE", f"{sid!r} is not registered")
    keys = ("status", "path", "source_type", "content_hash", "source_date", "source_date_confidence",
            "source_date_basis", "source_date_origin", "ingested_hash", "last_ingested_at")
    return f"{sid}\n" + "".join(f"  {k}: {rec.get(k)}\n" for k in keys)


def cmd_trace(vault: Path, sid: str) -> str:
    """Everything a retract_source plan has to clean up for `sid` (read-only)."""
    wiki = Wiki(vault)
    state = State(vault)
    retracted = {s for s, r in state.sources().items() if r["status"] == "retracted"} if state.managed else set()
    out = [f"trace {sid}"]
    if sid not in wiki.source_ids():
        out.append("  (no source page)")
    for pid, p in sorted(wiki.pages.items()):
        if pid == sid:
            continue
        cites = sid in (p.fm.get("sources") or [])
        origin = sid in _provenance(p.fm) if p.page_type == "decision" else False
        sections = []
        tree = p.tree()
        if tree.root is not None:
            s, e = tree.preamble
            if sid in wikilink_targets(p.lines[s:e]):  # exact target, code spans and fences ignored
                sections.append("__preamble__")

            def walk(node: Node, prefix: list[str]):
                for c in node.children:
                    own_end = c.children[0].start if c.children else c.end
                    if sid in wikilink_targets(p.lines[c.start:own_end]):
                        sections.append(" > ".join(prefix + [c.text]))
                    walk(c, prefix + [c.text])

            walk(tree.root, [])
        if not (cites or origin or sections):
            continue
        tags = []
        if p.page_type == "decision":
            only = origin and set(_provenance(p.fm)) <= retracted | {sid}  # the others were retracted earlier
            tags.append(f"decision {p.fm.get('status')}" + (" — ONLY origin: revoke it" if only and
                        p.fm.get("status") in ("active", "proposed") else (" — one of its origins" if origin else "")))
        others = sorted(set(p.fm.get("sources") or []) - {sid})
        tags.append(f"other sources: {', '.join(others) if others else 'none (consider delete_page)'}")
        out.append(f"  {pid}  [{'; '.join(tags)}]")
        for sec in sections:
            out.append(f"      links in: {sec}")
    return "\n".join(out) + "\n"


def _wiki_files_by_id(vault: Path) -> dict[str, list[Path]]:
    """page_id (frontmatter, else file stem) → every page file that claims it, across all type directories."""
    by_id: dict[str, list[Path]] = {}
    for d in TYPE_DIRS.values():
        for f in sorted((vault / "wiki" / d).glob("*.md")):
            try:
                pid = Page.parse(f.read_text(encoding="utf-8")).fm.get("page_id")
            except ValueError:
                pid = None
            pid = pid if valid_page_id(pid) else f.stem  # a malformed id is never used as a key
            by_id.setdefault(pid, []).append(f)
    return by_id


def revert_pages(vault: Path, page_ids: list[str]) -> list[str]:
    """Undo changes made outside a plan (e.g. an agent that bypassed the guard): restore each page to its
    baseline (= content after the last controlled write), or remove a page that was never registered.
    Runs as one transaction, so `rollback <txn_id>` brings the reverted content back for inspection."""
    state = State(vault)
    if not state.managed:
        raise PlanError("SCHEMA", "not a managed vault (run init)")
    report: list[str] = []
    with Lock(vault, "maintain --revert " + " ".join(page_ids)):
        state.check_no_incomplete()
        before = Fingerprint(vault)
        pages_reg = state.pages()
        on_disk = _wiki_files_by_id(vault)
        outputs: dict[str, str | None] = {}
        restored: list[str] = []
        regen_index = False
        for pid in page_ids:
            if pid in GENERATED and pid not in pages_reg:
                if pid == "log":
                    if not (vault / LOG_BASELINE).exists():
                        raise PlanError("REVERT_UNAVAILABLE", "no log baseline in this vault")
                    base = normalize((vault / LOG_BASELINE).read_text(encoding="utf-8"))
                    log = vault / "wiki" / "log.md"
                    if log.exists() and normalize(log.read_text(encoding="utf-8")) == base:
                        report.append("UNCHANGED   log")
                    else:
                        outputs["wiki/log.md"] = base
                        report.append("REVERTED    log  → as last written by a transaction")
                else:
                    regen_index = True
                continue
            rec = pages_reg.get(pid)
            copies = [f.relative_to(vault).as_posix() for f in on_disk.get(pid, [])]
            if rec is None:
                if not copies:
                    raise PlanError("UNKNOWN_PAGE", f"{pid!r} is neither registered nor present in wiki/")
                for rel in copies:
                    outputs[rel] = None
                    report.append(f"REMOVED     {pid}  ({rel}: created outside a plan, never registered)")
                continue
            if rec.get("origin") == "external":
                raise PlanError("REVERT_UNAVAILABLE", f"{pid!r} was registered as an external page; it has no "
                                "controlled version to go back to")
            base = state.baseline(pid)
            if base is None:
                raise PlanError("REVERT_UNAVAILABLE", f"{pid!r} has no baseline")
            target = rec["path"]
            strays = [rel for rel in copies if rel != target]
            for rel in strays:
                outputs[rel] = None  # renamed / copied outside a plan: drop the copy
            if (vault / target).exists() and normalize((vault / target).read_text(encoding="utf-8")) == normalize(base):
                if not strays:
                    report.append(f"UNCHANGED   {pid}")
                    continue
            outputs[target] = normalize(base)
            restored.append(pid)
            report.append(f"REVERTED    {pid}  → content of the last controlled write ({rec.get('last_txn') or 'n/a'})")
        if outputs or regen_index:
            remaining = {}
            for d in TYPE_DIRS.values():
                for f in (vault / "wiki" / d).glob("*.md"):
                    rel = f.relative_to(vault).as_posix()
                    if outputs.get(rel, "") is None:
                        continue
                    text = outputs.get(rel) or f.read_text(encoding="utf-8")
                    try:
                        page = Page.parse(text, f)
                        remaining[page.fm.get("page_id") or f.stem] = page
                    except ValueError:
                        pass
            for rel, text in outputs.items():
                if text is not None and rel.startswith("wiki/") and not (vault / rel).exists():
                    page = Page.parse(text)
                    remaining[page.fm.get("page_id")] = page
            index_text = normalize(build_index(remaining))
            index = vault / "wiki" / "index.md"
            if not index.exists() or normalize(index.read_text(encoding="utf-8")) != index_text:
                outputs["wiki/index.md"] = index_text
                if regen_index:
                    report.append("REVERTED    index  → regenerated from the pages")
            elif regen_index:
                report.append("UNCHANGED   index")
        if outputs:
            txn = Transaction(state, "maintain-revert", pages=page_ids)
            for pid in restored:  # the registry names the last controlled write of every page: this one
                pages_reg[pid]["last_txn"] = txn.id
            if restored:
                outputs["state/pages.json"] = registry_json("pages", pages_reg)
            txn.commit(outputs, before)
            state.event("page_reverted", pages=page_ids, txn_id=txn.id)
            report.append(f"txn {txn.id}  (rollback {txn.id} restores what was reverted)")
    return report


def _effective(vault: Path, outputs: dict[str, str | None]) -> dict[str, str | None]:
    """Only the writes that change bytes on disk (a delete of a missing file or a rewrite of equal text is none)."""
    out = {}
    for rel, text in outputs.items():
        path = vault / rel
        if text is None:
            if path.exists():
                out[rel] = None
        elif not path.exists() or path.read_bytes() != text.encode("utf-8"):
            out[rel] = text
    return out


def maintain(vault: Path, fix: bool = False) -> list[str]:
    state = State(vault)
    if not state.managed:
        raise PlanError("SCHEMA", "not a managed vault (run init)")
    report: list[str] = []
    ctx = Lock(vault, "maintain --fix") if fix else None
    if ctx:
        ctx.__enter__()
    try:
        if fix:
            state.check_no_incomplete()
            before = Fingerprint(vault)  # under the lock, before anything is read: --fix commits against it
        pages_reg = state.pages()
        sources = state.sources()
        live_sources = {s: r["status"] != "retracted" for s, r in sources.items()}
        outputs: dict[str, str | None] = {}
        seen_files: dict[str, str] = {}
        valid: dict[str, Page] = {}
        edited: list[str] = []
        rewritten: list[str] = []  # registered pages whose file --fix writes (their last_txn becomes this txn)
        dups = {pid: fs for pid, fs in _wiki_files_by_id(vault).items() if len(fs) > 1}
        for pid, fs in sorted(dups.items()):
            report.append(f"DUPLICATE_PAGE_ID       {pid}: " + ", ".join(f.relative_to(vault).as_posix() for f in fs)
                          + "  (keep one, or maintain --revert " + pid + ")")
        for ptype, d in TYPE_DIRS.items():
            for f in sorted((vault / "wiki" / d).glob("*.md")):
                rel = f.relative_to(vault).as_posix()
                try:
                    page = Page.parse(f.read_text(encoding="utf-8"), f)
                    page.raw = f.read_text(encoding="utf-8")
                except ValueError as e:
                    report.append(f"NOT_MANAGEABLE          {rel}: {e}")
                    continue
                problems = page_problems(page, ptype, live_sources)
                if problems:
                    report.append(f"NOT_MANAGEABLE          {rel}: " + "; ".join(msg for _, msg in problems))
                    continue
                pid = page.fm["page_id"]
                seen_files[pid] = rel
                valid[pid] = page
                if f.stem != pid:
                    target = f"wiki/{d}/{pid}.md"
                    occupied = (vault / target).exists()
                    report.append(f"PAGE_ID_FILENAME_MISMATCH {rel} (page_id {pid})" + (
                        f"  ({target} is taken by another page: not renamed)" if occupied else
                        " → renamed back" if fix else ""))
                    if fix and not occupied:
                        outputs[rel] = None
                        outputs[target] = normalize(page.raw)
                        seen_files[pid] = target
                        if pid in pages_reg:
                            pages_reg[pid]["path"] = target
                            rewritten.append(pid)
                if pid not in pages_reg:
                    report.append(f"UNREGISTERED            {pid}" + (" → registered as external, human_touched" if fix else ""))
                    if fix:
                        pages_reg[pid] = {"path": seen_files[pid], "page_type": page.page_type, "origin": "external",
                                          "human_touched": True, "baseline_hash": sha256_text(""), "last_txn": None}
                        outputs[f"state/baselines/{pid}.md.base"] = ""
                    continue
                base = state.baseline(pid)
                if base is not None and normalize(base) != normalize(page.raw):
                    first = not pages_reg[pid].get("human_touched")
                    report.append(f"HUMAN_EDITED            {pid}" + (" → human_touched" if fix and first else "")
                                  + f"  (not your edit? maintain --revert {pid})")
                    if fix and first:
                        pages_reg[pid]["human_touched"] = True
                        edited.append(pid)
                src = sources.get(page.fm.get("source_id") or pid) if page.page_type == "source" else None
                if src is not None and src["status"] != "retracted":
                    synced = copy.deepcopy(page)
                    changes = _sync_source_fm(synced.fm, src)
                    if changes:
                        report.append(f"SOURCE_PAGE_OUTDATED    {pid}: " + "; ".join(changes)
                                      + (" → synced" if fix else "  (maintain --fix)"))
                    if changes and fix:
                        # page and baseline get the same frontmatter change, so a human edit stays visible
                        outputs[seen_files[pid]] = synced.text()
                        if base and base.strip():
                            b = Page.parse(base)
                            _sync_source_fm(b.fm, src)
                            outputs[f"state/baselines/{pid}.md.base"] = b.text()
                            pages_reg[pid]["baseline_hash"] = sha256_text(b.text())
                        valid[pid] = synced
                        rewritten.append(pid)
        for pid, rec in pages_reg.items():
            if pid not in seen_files:
                report.append(f"PAGE_MISSING            {pid} ({rec['path']})")
        known = set(seen_files)
        for pid, page in valid.items():
            try:
                check_links("\n".join(page.lines), known, pid)
            except PlanError as e:
                report.append(f"BROKEN_LINK             {pid}: {e.message.split('unknown: ')[-1]}")
        index = vault / "wiki" / "index.md"
        if not index.exists() or normalize(index.read_text(encoding="utf-8")) != normalize(build_index(valid)):
            report.append("INDEX_OUT_OF_DATE       wiki/index.md" + (" → regenerated" if fix else
                          "  (maintain --fix or maintain --revert index)"))
        log_base = vault / LOG_BASELINE
        log = vault / "wiki" / "log.md"
        if log_base.exists() and (not log.exists() or
                                  normalize(log.read_text(encoding="utf-8")) != normalize(log_base.read_text(encoding="utf-8"))):
            report.append("LOG_EDITED              wiki/log.md  (log is append-only via transactions; maintain --revert log)")
        if fix and dups:
            report.append("FIX_SKIPPED             resolve DUPLICATE_PAGE_ID first; nothing was changed")
        elif fix:
            txn = Transaction(state, "maintain")
            for pid in rewritten:
                pages_reg[pid]["last_txn"] = txn.id
            outputs["state/pages.json"] = registry_json("pages", pages_reg)
            outputs["wiki/index.md"] = normalize(build_index(valid))
            outputs = _effective(vault, outputs)
            # every page --fix writes must read back as a manageable page (the codec must round-trip)
            for rel, text in outputs.items():
                if text is not None and rel.startswith("wiki/") and rel.count("/") == 2:
                    try:
                        problems = page_problems(Page.parse(text))
                    except ValueError as e:
                        problems = [("SCHEMA", str(e))]
                    if problems:
                        raise PlanError("SCHEMA", f"maintain --fix would write an invalid {rel} "
                                                  f"({problems[0][1]}); nothing was changed")
            if outputs:  # a clean vault gets no transaction, no snapshot, no audit noise
                txn.commit(outputs, before)
            for pid in edited:
                state.event("human_edit_detected", page_id=pid, via="maintain")
    finally:
        if ctx:
            ctx.__exit__(None, None, None)
    return report or ["no issues"]


# ---------------------------------------------------------------------------
# CLI

def cmd_hash(vault: Path, page_id: str, sections: bool) -> str:
    wiki = Wiki(vault)
    if page_id not in wiki.pages:
        raise PlanError("UNKNOWN_PAGE", f"page {page_id!r} does not exist")
    p = wiki.pages[page_id]
    out = [f"{page_id}  {sha256_text(p.raw or p.text())}"]
    state = State(vault)
    if state.managed:  # stage1-design §10: tell the agent up front whether human edits are in play
        rec = state.pages().get(page_id)
        base = state.baseline(page_id) if rec else None
        if rec is None:
            baseline = "unregistered (run maintain)"
        elif base is None or not base.strip():
            baseline = "none (external page: every change needs human_override)"
        elif normalize(base) == normalize(p.raw or p.text()):
            baseline = "same"
        else:
            baseline = "DIFFERS (edited outside a plan; changed sections need human_override)"
        out.append(f"  human_touched: {'true' if rec and rec.get('human_touched') else 'false'}")
        out.append(f"  baseline: {baseline}")
    if sections:
        tree = p.tree()
        for code, msg in tree.errors:
            out.append(f"  ! {code}: {msg}")
        if tree.root is not None:
            s, e = tree.preamble
            out.append(f"  __preamble__  {sha256_text(chr(10).join(p.lines[s:e]))}")

            def walk(node: Node, prefix: list[str]):
                for c in node.children:
                    path = prefix + [c.text]
                    out.append(f"  {' > '.join(path)}  {sha256_text(chr(10).join(p.lines[c.start:c.end]))}")
                    walk(c, path)

            walk(tree.root, [])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# install / upgrade: the project (this repo) is the one source of the rule files and tools in every vault

PROJECT = Path(__file__).resolve().parents[1]
KIT_TOOLS = ("second_brain.py", "agent_guard.py")
KIT_RECORD = "state/kit.json"  # what install/upgrade last wrote: vault path -> hash


def kit_files(project: Path = PROJECT) -> dict[str, Path]:
    """Vault-relative path -> source file: everything in vault-template/, plus the tools."""
    template = project / "vault-template"
    if not template.is_dir():
        raise PlanError("SCHEMA", f"{project} has no vault-template/: run install / upgrade from the second-brain "
                                  "project, not from a vault's copy of the tool")
    files = {p.relative_to(template).as_posix(): p for p in sorted(template.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts}
    files.update({f"tools/{name}": project / "tools" / name for name in KIT_TOOLS})
    return files


def _kit_payload(project: Path) -> dict[str, bytes]:
    """Vault path -> the exact bytes the kit ships, read once: what is written and what is recorded come from
    this one snapshot, never from re-reading the project or the vault afterwards."""
    return {rel: src.read_bytes() for rel, src in kit_files(project).items()}


def _write_kit_record(target: Path, kit: dict[str, bytes], project: Path, event: str) -> None:
    """Record the hashes of what the kit wrote — not of the files as they are now: an edit made in the vault
    right after a file was written must show up as a local change (CONFLICT) at the next upgrade."""
    record = {"schema": 1, "project": str(project), "updated_at": now_iso(),
              "files": {rel: "sha256:" + sha256_bytes(data) for rel, data in sorted(kit.items())}}
    _atomic_write(target / KIT_RECORD, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    State(target).event(event, files=len(kit))


def install_vault(target: Path, project: Path = PROJECT) -> list[str]:
    """A new vault at `target`, which must not exist yet: the vault is built completely in a sibling staging
    directory (rule files, tools, `init`, kit record) and then renamed into place in one step. Nothing is ever
    written into an existing directory, so nothing there can be overwritten; if `target` appears meanwhile,
    the rename fails and the staging directory is removed."""
    kit = _kit_payload(project)
    if State(target).managed:
        raise PlanError("SCHEMA", f"{target} is already a vault; use upgrade")
    if target.exists():
        raise PlanError("SCHEMA", f"{target} already exists; install creates a new vault directory "
                                  "(to update an existing vault, use upgrade)")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.installing-", dir=target.parent))
    try:
        for rel, data in kit.items():
            _atomic_write_bytes(staging / rel, data)
        init_vault(staging)
        _write_kit_record(staging, kit, project, "kit_installed")
        try:
            os.rename(staging, target)  # atomic; fails if target appeared (a non-empty dir or any file)
        except OSError as e:
            raise PlanError("SCHEMA", f"{target} appeared during install; nothing was written to it ({e})")
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return [f"installed {len(kit)} files into {target}", "next: put sources into raw/ and run "
            "`python tools/second_brain.py discover` there (see README.md)"]


def upgrade_vault(target: Path, dry_run: bool = False, force: bool = False, project: Path = PROJECT) -> list[str]:
    """Bring a vault's rule files and tools up to the project's version. A file changed in the vault since
    install/upgrade wrote it is a CONFLICT (never overwritten without --force). Only kit files are touched:
    raw/, wiki/, state/ (except kit.json) and plans/ never are."""
    if not State(target).managed:
        raise PlanError("SCHEMA", f"{target} is not a vault; use install")
    kit = _kit_payload(project)
    if dry_run:
        actions = _kit_actions(target, kit)
        return [f"{a:<9}{rel}" for a, rel, _ in actions] + (["(dry run: nothing written)"] if actions else []) \
            or ["up to date"]
    with Lock(target, "upgrade"):
        actions = _kit_actions(target, kit)  # scanned under the lock; `seen` = each target's hash as scanned
        report = [f"{a:<9}{rel}" for a, rel, _ in actions] or ["up to date"]
        conflicts = [rel for a, rel, _ in actions if a == "CONFLICT"]
        if conflicts and not force:
            raise PlanError("KIT_CONFLICT", "changed in the vault since the last install/upgrade (keep your change "
                            "by moving it into the project, or overwrite with --force): " + ", ".join(conflicts))
        # a changed file the kit no longer ships is left in place, even with --force
        todo = [(a, rel, seen) for a, rel, seen in actions if a == "REMOVE" or rel in kit]
        now = lambda rel: file_hash(target / rel) if (target / rel).exists() else None
        wrote = lambda a, rel: None if a == "REMOVE" else "sha256:" + sha256_bytes(kit[rel])
        before = {rel: (target / rel).read_bytes() if (target / rel).exists() else None for _, rel, _ in todo}
        written: list[tuple[str, str]] = []  # (action, vault path)

        def undo(stale: str) -> None:
            """An edit arrived while upgrading: put back what this upgrade wrote, record nothing, refuse.
            A written file that has been edited again since is newer work: it is left alone and named."""
            kept = []
            for a, rel in reversed(written):
                if now(rel) != wrote(a, rel):
                    kept.append(rel)
                elif before[rel] is None:
                    (target / rel).unlink()
                else:
                    _atomic_write_bytes(target / rel, before[rel])
            raise PlanError("KIT_CONFLICT", f"{stale} changed in the vault during upgrade; nothing was written"
                            + (f" (except {', '.join(kept)}, edited again since: check them)" if kept else ""))

        # each target must still be exactly as scanned (an ADD target must still not exist) right before it is
        # replaced; `before` must be that same version, or it could not be put back
        for a, rel, seen in todo:
            if now(rel) != seen or (before[rel] is not None and "sha256:" + sha256_bytes(before[rel]) != seen):
                undo(rel)
            if a == "REMOVE":
                (target / rel).unlink()
            else:
                _atomic_write_bytes(target / rel, kit[rel])
            written.append((a, rel))
        # read back: a file edited right after it was written is newer work — kept, and (because the record
        # holds what the kit wrote, not what is on disk) reported as a CONFLICT by the next upgrade
        report += [f"EDITED   {rel}  (changed in the vault right after the upgrade wrote it: kept as a local "
                   "change)" for a, rel in written if now(rel) != wrote(a, rel)]
        _write_kit_record(target, kit, project, "kit_upgraded")  # only after a complete upgrade
    return report


def _kit_actions(target: Path, kit: dict[str, bytes]) -> list[tuple[str, str, str | None]]:
    """(action, vault path, the target's hash as scanned) for every kit file that differs in the vault:
    ADD (absent), UPDATE (unchanged since last recorded), REMOVE (dropped from the kit, unchanged), CONFLICT."""
    rec_path = target / KIT_RECORD
    recorded = json.loads(rec_path.read_text(encoding="utf-8"))["files"] if rec_path.exists() else {}
    actions: list[tuple[str, str, str | None]] = []
    for rel, data in kit.items():
        dest = target / rel
        cur = file_hash(dest) if dest.exists() else None
        if cur == "sha256:" + sha256_bytes(data):
            continue
        if cur is None:
            actions.append(("ADD", rel, None))
        elif cur == recorded.get(rel):
            actions.append(("UPDATE", rel, cur))
        else:  # changed in the vault (or never recorded: a vault from before install/upgrade existed)
            actions.append(("CONFLICT", rel, cur))
    for rel, h in recorded.items():
        dest = target / rel
        if rel not in kit and dest.exists():
            cur = file_hash(dest)
            actions.append(("REMOVE" if cur == h else "CONFLICT", rel, cur))
    return actions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="second_brain.py", description=__doc__.split("\n")[0])
    ap.add_argument("--vault", type=Path, default=Path.cwd())
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hash")
    h.add_argument("page_id")
    h.add_argument("--sections", action="store_true")
    for name in ("validate-plan", "render-plan"):
        v = sub.add_parser(name)
        v.add_argument("plan", type=Path)
        v.add_argument("--allow-restructure", action="store_true")
    a = sub.add_parser("apply-plan")
    a.add_argument("plan", type=Path)
    a.add_argument("--approve", required=True)
    a.add_argument("--allow-restructure", action="store_true")
    sub.add_parser("init")
    d = sub.add_parser("discover")
    d.add_argument("--link", nargs=2, metavar=("RAW_PATH", "SOURCE_ID"))
    sub.add_parser("status")
    s = sub.add_parser("source")
    s.add_argument("source_id")
    tr = sub.add_parser("trace")
    tr.add_argument("source_id")
    r = sub.add_parser("reject-plan")
    r.add_argument("plan", type=Path)
    r.add_argument("--reason", required=True)
    rb = sub.add_parser("rollback")
    rb.add_argument("txn_id")
    m = sub.add_parser("maintain")
    m.add_argument("--fix", action="store_true")
    m.add_argument("--revert", nargs="+", metavar="PAGE_ID",
                   help="restore pages changed outside a plan to their last controlled version")
    ins = sub.add_parser("install", help="create a new vault at PATH from this project (rule files, tools, init)")
    ins.add_argument("path", type=Path)
    up = sub.add_parser("upgrade", help="update the rule files and tools of the vault at PATH from this project")
    up.add_argument("path", type=Path)
    up.add_argument("--dry-run", action="store_true")
    up.add_argument("--force", action="store_true", help="also overwrite files changed in the vault")
    u = sub.add_parser("unlock")
    u.add_argument("--force", action="store_true", required=True)
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    vault = args.vault
    state = State(vault)
    try:
        if args.cmd == "hash":
            sys.stdout.write(cmd_hash(vault, args.page_id, args.sections))
        elif args.cmd in ("validate-plan", "render-plan"):
            raw = args.plan.read_bytes()
            ex = execute(Wiki(vault), _load_json(raw), args.allow_restructure, state if state.managed else None)
            if args.cmd == "validate-plan":
                print(f"OK  {len(ex.results)} operations  plan sha256 {sha256_bytes(raw)}")
            else:
                sys.stdout.write(render(ex, sha256_bytes(raw)))
        elif args.cmd == "apply-plan":
            ex = apply(vault, args.plan, args.approve, args.allow_restructure)
            print(f"APPLIED  created {len(ex.created)}, updated {len(ex.touched)}, "
                  f"decision changes {len(ex.decision_changes)}, deleted {len(ex.deleted)}"
                  + (f"  txn {ex.txn_id}" if ex.txn_id else ""))
            if ex.ingested:
                print("ingested: " + ", ".join(ex.ingested))
            for w in ex.warnings:
                print("WARNING  " + w)
        elif args.cmd == "init":
            init_vault(vault)
            print(f"initialised {vault}")
        elif args.cmd == "discover":
            print("\n".join(discover(vault, tuple(args.link) if args.link else None)))
        elif args.cmd == "status":
            sys.stdout.write(cmd_status(vault))
        elif args.cmd == "source":
            sys.stdout.write(cmd_source(vault, args.source_id))
        elif args.cmd == "trace":
            sys.stdout.write(cmd_trace(vault, args.source_id))
        elif args.cmd == "reject-plan":
            print(f"REJECTED → {reject_plan(vault, args.plan, args.reason)}")
        elif args.cmd == "rollback":
            print("\n".join(rollback(vault, args.txn_id)))
        elif args.cmd == "maintain":
            if args.revert:
                print("\n".join(revert_pages(vault, args.revert)))
            else:
                print("\n".join(maintain(vault, args.fix)))
        elif args.cmd == "install":
            print("\n".join(install_vault(args.path)))
        elif args.cmd == "upgrade":
            print("\n".join(upgrade_vault(args.path, args.dry_run, args.force)))
        elif args.cmd == "unlock":
            lock = state.dir / ".lock"
            if lock.exists():
                info = lock.read_text(encoding="utf-8")
                lock.unlink()
                state.event("lock_forced", lock=info)
                print("lock removed")
            else:
                print("no lock")
    except PlanError as e:
        print(f"REJECTED {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"REJECTED SCHEMA: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

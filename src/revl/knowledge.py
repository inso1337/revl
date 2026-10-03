"""A derived index over the comments a composition already has (issue #1745).

Knowledge slice 2 of the design study ("Knowledge served, not embedded",
§6-§7). A comment is a cache that never invalidates: of 163 `// expected
error:` blocks, 158 are reproduced by the compiler and 5 had silently drifted,
and none was pinned by a test. This module turns the comments of the files a
session reads into an index it can check and serve:

* each comment block (consecutive comment-only lines) is ANCHORED to the
  declaration it describes: the top-level declaration or nested member whose
  code starts on the first code line after it, else the declaration that
  contains it, else the file. The anchor is a symbol path, as `revl_source`
  addresses it, and a FINGERPRINT of the anchor's canonical, comment-free code
  (none for a block about the file as a whole, which no code edit stales);
* each block is CLASSIFIED by rule. An `expected error:` block, or a
  `REFUSED` / `REJECTED` / `ADMITTED` head, is `derived`: a reference to the
  diagnostic, re-checked against the compile and reported `live` or
  `refuted`. A block that cites a guarantee code or a `docs/` path is
  `served`: a reference, expanded by `revl explain` or the doc. Neither copies
  its body. Everything else is `doc`, with its body;
* an entry goes `stale` when its anchor's fingerprint changes. A comment-only
  edit or a reformat changes no fingerprint, so it stales nothing.

Pure: the caller supplies the compile verdict, and nothing here touches a
session.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

DOC_BODY_LIMIT = 2048
_VERDICT_HEAD = re.compile(r"^(REFUSED|REJECTED|ADMITTED)\b")
_DOCS_PATH = re.compile(r"\bdocs/[\w./-]+\.md\b")
_CODE = re.compile(r"\b(?:[AGT]\d{1,2}|G-[A-Z]+(?:-[A-Z]+)*|T-UNRESOLVED)\b")


@dataclass(frozen=True)
class Block:
    first: int          # 1-based line of the first comment line
    last: int
    lines: tuple[str, ...]   # the comment text, `//` and one space removed

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def comment_blocks(text: str) -> list[Block]:
    """Every run of consecutive comment-only lines."""
    blocks: list[Block] = []
    current: list[tuple[int, str]] = []
    for number, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("//"):
            body = stripped[2:]
            current.append((number, body[1:] if body.startswith(" ") else body))
            continue
        if current:
            blocks.append(Block(current[0][0], current[-1][0],
                                tuple(b for _, b in current)))
            current = []
    if current:
        blocks.append(Block(current[0][0], current[-1][0],
                            tuple(b for _, b in current)))
    return blocks


# ---------------------------------------------------------------- classify

def expected_lines(block: Block) -> list[str] | None:
    """The lines of an `expected error:` block, or None."""
    for index, line in enumerate(block.lines):
        if line.strip() == "expected error:":
            out = []
            for rest in block.lines[index + 1:]:
                if not rest.strip():
                    break
                out.append(rest.strip())
            return out or None
    return None


def classify(block: Block, known_codes) -> dict:
    """{tier, kind, ref?, body?} by the study's rules."""
    expected = expected_lines(block)
    if expected is not None:
        return {"tier": "derived", "kind": "expected-error",
                "ref": {"kind": "diagnostic"}, "_expected": expected}
    head = next((line for line in block.lines if line.strip()), "")
    verdict = _VERDICT_HEAD.match(head.strip())
    if verdict:
        return {"tier": "derived", "kind": "verdict",
                "ref": {"kind": "diagnostic",
                        "verdict": verdict.group(1).lower()}}
    codes = sorted({c for c in _CODE.findall(block.text) if c in known_codes})
    docs = sorted(set(_DOCS_PATH.findall(block.text)))
    if codes or docs:
        ref = {"kind": "served"}
        if codes:
            ref["explain"] = codes
        if docs:
            ref["docs"] = docs
        return {"tier": "served", "kind": "reference", "ref": ref}
    return {"tier": "doc", "kind": "doc", "body": block.text[:DOC_BODY_LIMIT]}


# ---------------------------------------------------------------- re-check

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def reproduces(expected: list[str], report: dict | None) -> bool:
    """Whether a compile's structured report says every expected line. A
    `file:line:` prefix is dropped, since the report carries those as fields;
    a `None` report means the file compiled clean, which reproduces nothing."""
    if report is None:
        return False
    haystack = _norm(json.dumps(report, ensure_ascii=False)).replace('\\"', '"')
    for line in expected:
        wanted = _norm(re.sub(r"^\S+\.rvl:\d+:\s*", "", line)).replace('\\"', '"')
        if wanted and wanted not in haystack:
            return False
    return True


def recheck(entry: dict, report: dict | None) -> str:
    """`live` or `refuted` for a derived entry, against the compile."""
    if entry["kind"] == "expected-error":
        return "live" if reproduces(entry["_expected"], report) else "refuted"
    if entry["kind"] == "verdict":
        refused = entry["ref"].get("verdict") in ("refused", "rejected")
        return "live" if (report is not None) == refused else "refuted"
    return "live"


# ---------------------------------------------------------------- anchor

def _first_code_line(text: str, after: int) -> int | None:
    lines = text.split("\n")
    for number in range(after + 1, len(lines) + 1):
        stripped = lines[number - 1].strip()
        if stripped and not stripped.startswith("//"):
            return number
    return None


def anchor_of(text: str, name: str, block: Block, decls: list,
              nodes: dict) -> tuple[str | None, int, int]:
    """(symbol path or None for the file, first line, last line) of what a
    block describes. `decls` are the file's top-level declarations and `nodes`
    a cache of their parsed nodes."""
    from .mcp import nested  # noqa: PLC0415 — the symbol model is there

    line = _first_code_line(text, block.last)
    if line is None:
        return None, 1, len(text.split("\n"))
    containing = [d for d in decls if d.start <= line <= d.end]
    if not containing:
        return None, 1, len(text.split("\n"))
    decl = containing[0]
    if decl.start == line:
        return decl.name, decl.start, decl.end
    try:
        key = (decl.kind, decl.name)
        if key not in nodes:
            nodes[key] = nested.node_of(text, name, decl.kind, decl.name)
        node = nodes[key]
        for member in nested.members(node, decl.kind):
            if member.line == line:
                first, last = nested.span(text, name, node, decl.kind, member)
                return ".".join((decl.name,) + member.path), first, last
    except nested.NestedError:
        pass
    return decl.name, decl.start, decl.end


def fingerprint(text: str, name: str, first: int, last: int) -> str:
    """sha256 of the span's canonical, comment-free code."""
    from .formatter import FormatError, format_source  # noqa: PLC0415

    span = "\n".join(text.split("\n")[first - 1:last]) + "\n"
    try:
        code = format_source(span, name, comments=False)
    except FormatError:
        code = "\n".join(line for line in span.split("\n")
                         if not line.strip().startswith("//"))
    return "sha256:" + hashlib.sha256(code.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the index

def index_text(path: str, text: str, report: dict | None, known_codes) -> list[dict]:
    """Every comment block of one file as an index entry. `report` is the
    structured diagnostic of compiling it, or None when it compiled clean."""
    from .mcp import symbols  # noqa: PLC0415

    try:
        decls = symbols.declarations(text, path)
    except symbols.SymbolError:
        decls = []
    nodes: dict = {}
    entries = []
    for block in comment_blocks(text):
        record = classify(block, known_codes)
        symbol, first, last = anchor_of(text, path, block, decls, nodes)
        entry = {**record,
                 "id": hashlib.sha256(f"{path}\0{symbol}\0{block.text}"
                                      .encode("utf-8")).hexdigest()[:16],
                 # a block about the file as a whole has no code of its own to
                 # go stale against (anchored to no declaration)
                 "anchor": {"path": path, "symbol": symbol,
                            "fingerprint": (fingerprint(text, path, first, last)
                                            if symbol is not None else None)},
                 "line": block.first, "status": "live"}
        if record["tier"] == "derived":
            entry["status"] = recheck(entry, report)
        entries.append(entry)
    return entries


def merge(previous: list[dict], current: list[dict]) -> list[dict]:
    """The index after a change: every entry the new text has, with an entry
    whose anchor's code changed marked `stale` (a refuted one stays refuted,
    and a stale one stays stale until it is confirmed). An entry whose comment
    is gone is dropped: the comment was edited or removed, and what the text
    says now is the new entry, so a comment-only edit stales nothing."""
    by_id = {entry["id"]: entry for entry in previous}
    out = []
    for entry in current:
        old = by_id.pop(entry["id"], None)
        if old is not None and old["status"] in ("stale", "refuted") \
                and entry["status"] == "live":
            entry = {**entry, "status": old["status"]}
        if old is not None \
                and old["anchor"]["fingerprint"] != entry["anchor"]["fingerprint"] \
                and entry["status"] != "refuted":
            entry = {**entry, "status": "stale", "staleSince": "its code changed"}
        out.append(entry)
    return out


def public(entry: dict, *, body_limit: int = 400) -> dict:
    """An entry as it is served: no internal fields, a bounded body."""
    out = {k: v for k, v in entry.items() if not k.startswith("_")}
    body = out.get("body")
    if body is not None and len(body) > body_limit:
        out["body"], out["more"] = body[:body_limit], True
    return out


def counts(entries: list[dict]) -> dict:
    out = {"entries": len(entries), "live": 0, "stale": 0, "refuted": 0}
    for entry in entries:
        out[entry["status"]] = out.get(entry["status"], 0) + 1
    return out


__all__ = ["Block", "comment_blocks", "classify", "reproduces", "recheck",
           "anchor_of", "fingerprint", "index_text", "merge", "public", "counts"]

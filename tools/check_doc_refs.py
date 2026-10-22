#!/usr/bin/env python3
"""Advisory gate: hand-written `path:line` references in docs and comments.

WHY THIS EXISTS (issue #2844). Prose and comments cite source locations as
`path:line`, and nothing checks them. A scan of this tree on main found 4,090
distinct reference sites, of which 56 pointed past the end of the cited file
and 1,051 did not resolve as written (shorthand, sibling-relative spellings,
or files that moved). The generated blocks cannot rot -- `tools/docgen.py
--check` byte-compares those -- but the hand-written ones can, and they are
the measured bulk. A stale anchor is a reader trap and a review round trip.

WHAT IS SCANNED. Markdown under `docs/` (the prose that carries most of the
cross-references) and comments and docstrings in Python under `src/` and
`tests/`, where the scan is cheap and the anchors are dense. Other comment
syntaxes are deliberately out of scope for this slice; the reference regex
already accepts targets in six languages, so widening the sources later is a
constant, not a redesign. A hidden directory is not part of the documented
tree either: `.cordis-py` is a vendored checkout in the jobs that run
`backends/python/setup.sh`, and a local `.venv` may exist too. A file under
one is skipped as a citing file and a reference into one is neither ok nor a
finding, so whether such a checkout exists locally or in a CI job cannot
change the verdict or the ledger.

THREE CLASSES. Every reference is one of:
  * ok       -- the path resolves and the cited line exists;
  * missing  -- no such file from the repo root or from the citing file's
                directory;
  * stale    -- the file resolves but the cited line is past its end.
Resolution is root-relative first, then citing-file-relative, which is how
the tree actually writes them. A reference that resolves both ways is not a
finding.

THE LEDGER IS A BASELINE, NOT AN ALLOWLIST. `tools/doc_refs_ledger.json`
records the findings this tree already has, so the check can be green on the
day it is adopted and fail only on a finding that is not recorded. Every
entry carries a `reason`; an entry without one is a malformed ledger, not a
suppression. `--write-ledger` (alias `--baseline`) writes the current
findings, keeping any reason already on file, and DROPS entries that no
longer match a finding, so the ledger shrinks as anchors are repaired. The
reason it writes for a new entry is a class-level triage label -- missing or
stale -- not an individual justification, and the PR that adopts it says so.

WHAT THIS SLICE DOES NOT DO. There is no `--write` repair mode. A bare
`path:line` does not say which text it meant: renumbering it from the line
alone would be guessing, and guessing is worse than a recorded finding. A
repair mode needs the cited snippet or symbol as an input the reference
syntax does not carry today, so it is deferred rather than faked.

ADVISORY. `tools/pre_pr.sh` runs this check but does not fail the pre-PR on
it; CI does not run it at all yet. It becomes a hard check once the ledger is
small and the tree is used to it.

Usage:
    python3 tools/check_doc_refs.py                 # report only
    python3 tools/check_doc_refs.py --check         # exit 1 on a new finding
    python3 tools/check_doc_refs.py --write-ledger  # author the baseline
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import pathlib
import re
import sys
import tokenize
from collections import Counter
from dataclasses import dataclass

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LEDGER = REPO_ROOT / "tools" / "doc_refs_ledger.json"

#: Where the sourced references are, relative to the repo root.
MARKDOWN_ROOTS = ("docs",)
PYTHON_ROOTS = ("src", "tests")

#: One reference: a repo-relative path with a known source extension and a
#: 1-based line. The path alphabet excludes spaces and backticks on purpose so
#: a reference inside a sentence stops at the punctuation that ends it.
REF_PATTERN = re.compile(
    r"[A-Za-z0-9_./-]+\.(?:py|rvl|ts|go|java|rs|md):[0-9]+")
REF_SHAPE = re.compile(
    r"^(?P<path>[A-Za-z0-9_./-]+\.(?:py|rvl|ts|go|java|rs|md))"
    r":(?P<line>[0-9]+)$")

#: One entry per adopted finding, so a reviewer reads a diff rather than a blob.
LEDGER_HEADER = (
    "Issue #2844. The hand-written `path:line` references this tree already "
    "gets wrong, so the advisory check is green on adoption and fails only on "
    "a finding that is not recorded. Written by `python3 "
    "tools/check_doc_refs.py --write-ledger`, which no routine regeneration "
    "command calls; widening it is an edit a reviewer reads. Every entry "
    "carries a reason. One entry per distinct `path:line` spelling, however "
    "many citing files use it. Shrink-only: repair an anchor and the next "
    "--write-ledger drops its entry. The reasons are class-level triage "
    "labels written at adoption, not per-reference justifications, and the "
    "adopting PR says so."
)

#: What `--write-ledger` records for a finding it has not seen before. It is a
#: triage label: the entry exists so the check can be green now, and it is
#: meant to be repaired and dropped, not to be a permanent exception.
REASONS = {
    "missing": (
        "unresolved as written: shorthand, sibling-relative or moved file; "
        "triaged for repair (issue #2844)"),
    "stale": (
        "line past end of file: anchor shifted or file shortened; triaged for "
        "repair (issue #2844)"),
}


@dataclass(frozen=True)
class Reference:
    """One occurrence of a `path:line` spelling in a citing file."""

    source: str   # the citing file, relative to the repo root
    cited: str    # the cited path exactly as written
    line: int     # the cited line, 1-based


@dataclass(frozen=True)
class Finding:
    """A reference that is not `ok`, with the class and the observed extent."""

    source: str
    cited: str
    line: int
    kind: str        # "missing" | "stale"
    detail: str      # e.g. "no such file" or "file has 2043 lines"


@dataclass(frozen=True)
class Report:
    """One scan and its resolution against one ledger."""

    files: tuple
    references: tuple
    findings: tuple
    ledger: tuple
    ledger_error: str | None

    @property
    def counts(self) -> Counter:
        return Counter(f.kind for f in self.findings)

    @property
    def new_findings(self) -> list:
        known = {(e["path"], e["line"]) for e in self.ledger}
        return [f for f in self.findings if (f.cited, f.line) not in known]

    @property
    def obsolete(self) -> list:
        present = {(f.cited, f.line) for f in self.findings}
        return [e for e in self.ledger if (e["path"], e["line"]) not in present]


# ------------------------------------------------------------------ scanning

def _text(path: pathlib.Path) -> str:
    """The file's text, tolerant of a stray non-UTF-8 byte: a reference in the
    rest of the file is still a reference."""
    return path.read_text(encoding="utf-8", errors="replace")


def _markdown_refs(path: pathlib.Path) -> list:
    return [m.group(0) for m in REF_PATTERN.finditer(_text(path))]


def _python_refs(path: pathlib.Path) -> list:
    """References in a Python file's comments and docstrings.

    Comments come from the tokenizer. Docstrings come from the AST rather than
    from every STRING token, because Python 3.12 splits f-strings into
    different tokens than 3.11 does, and a checker whose findings depend on
    the interpreter version cannot have a stable ledger. Ordinary code
    literals are not prose and are not scanned. A file the parser rejects is
    scanned whole rather than silently skipped: a finding lost to a parse
    error is worse than one found in a literal.
    """
    source = _text(path)
    out = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [m.group(0) for m in REF_PATTERN.finditer(source)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                out.extend(m.group(0) for m in REF_PATTERN.finditer(doc))
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                out.extend(m.group(0) for m in REF_PATTERN.finditer(token.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [m.group(0) for m in REF_PATTERN.finditer(source)]
    return out


def _hidden(rel: str) -> bool:
    """True when a path, as written, has a hidden segment such as `.venv` or
    `.cordis-py`.

    A hidden directory is not part of the documented tree: the jobs that run
    `backends/python/setup.sh` create `.cordis-py`, and a local `.venv` may
    exist too. Whether one is there must not change the verdict, so it is
    neither scanned as a citing file nor resolved as a cited one. A segment
    made only of dots is a relative step (`../src/x.py`) or an ellipsis
    shorthand (`.../x.py`), not a hidden name, so it is scanned as before.
    """
    return any(part.startswith(".") and part.strip(".")
               for part in rel.split("/"))


def scan(root: pathlib.Path = REPO_ROOT) -> tuple:
    """`(files, references)` for the tree under `root`, in a stable order."""
    files = []
    for base in MARKDOWN_ROOTS:
        if (root / base).is_dir():
            files.extend(("markdown", p) for p in sorted((root / base).rglob("*.md"))
                         if not _hidden(p.relative_to(root).as_posix()))
    for base in PYTHON_ROOTS:
        if (root / base).is_dir():
            files.extend(("python", p) for p in sorted((root / base).rglob("*.py"))
                         if not _hidden(p.relative_to(root).as_posix()))
    references = []
    for kind, path in files:
        rel = path.relative_to(root).as_posix()
        raws = _markdown_refs(path) if kind == "markdown" else _python_refs(path)
        seen = set()
        for raw in raws:
            match = REF_SHAPE.match(raw)
            if match is None:
                continue
            cited = match.group("path")
            if _hidden(cited):
                continue
            ref = Reference(rel, cited, int(match.group("line")))
            key = (ref.cited, ref.line)
            if key in seen:
                continue
            seen.add(key)
            references.append(ref)
    return tuple(p for _, p in files), tuple(references)


# --------------------------------------------------------------- resolution

def resolve(reference: Reference, root: pathlib.Path = REPO_ROOT,
            line_counts: dict | None = None) -> tuple:
    """`(kind, detail, cited_file)` for one reference.

    Root-relative first, then the citing file's directory, the two spellings
    this tree uses. The FIRST candidate that names an existing file wins, so a
    root-relative file whose line is past EOF is `stale` even if a sibling file
    of the same name has that line: falling through would guess that the
    reference meant the sibling, which is the one thing this check must not do.
    `kind` is "ok", "missing" or "stale"; `cited_file` is the repository-
    relative path that resolved, or `None`.
    """
    if line_counts is None:
        line_counts = {}
    root = root.resolve()
    candidates = [
        root / reference.cited,                            # repo-relative
        (root / reference.source).parent / reference.cited,  # sibling-relative
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if not resolved.is_file():
            continue
        key = rel.as_posix()
        if key not in line_counts:
            line_counts[key] = len(resolved.read_bytes().splitlines())
        total = line_counts[key]
        if 1 <= reference.line <= total:
            return "ok", f"file has {total} lines", key
        return "stale", f"file has {total} lines", key
    return "missing", "no such file", None


def examine(root: pathlib.Path = REPO_ROOT) -> tuple:
    """`(files, references, findings)` for the tree under `root`."""
    files, references = scan(root)
    line_counts: dict = {}
    findings = []
    for reference in references:
        kind, detail, _ = resolve(reference, root, line_counts)
        if kind != "ok":
            findings.append(Finding(reference.source, reference.cited,
                                    reference.line, kind, detail))
    findings.sort(key=lambda f: (f.source, f.cited, f.line))
    return files, references, tuple(findings)


# ------------------------------------------------------------------- ledger

def load_ledger(path: pathlib.Path = LEDGER) -> tuple:
    """`(entries, error)`. A ledger that is missing, unreadable, or has an
    entry without a path, a positive line and a reason is an error, never an
    empty check. `kind` is optional when reading, so a hand-edited ledger
    without it still loads, but every entry this tool writes carries it: it is
    what lets a missing -> stale flip refresh the class-level reason instead
    of keeping the wrong one."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], f"the ledger is missing: {path}"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], f"the ledger is unreadable: {path}: {exc}"
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        return [], f"the ledger has no `findings` list: {path}"
    entries = []
    for entry in doc["findings"]:
        if not isinstance(entry, dict):
            return [], f"a ledger entry is malformed: {entry!r}"
        cited = entry.get("path")
        line = entry.get("line")
        reason = entry.get("reason")
        kind = entry.get("kind")
        if not isinstance(cited, str) or not cited.strip():
            return [], f"a ledger entry has no path: {entry!r}"
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            return [], f"a ledger entry has no positive line: {entry!r}"
        if not isinstance(reason, str) or not reason.strip():
            return [], (f"a ledger entry has no reason: {entry!r}; every "
                        "recorded finding says why it is still a finding")
        if kind not in (None, "missing", "stale"):
            return [], f"a ledger entry has an unknown kind: {entry!r}"
        loaded = {"path": cited, "line": line}
        if kind is not None:
            loaded["kind"] = kind
        loaded["reason"] = reason
        entries.append(loaded)
    entries.sort(key=lambda e: (e["path"], e["line"]))
    return entries, None


def ledger_text(entries: list) -> str:
    """The ledger's one on-disk spelling, byte-identical under any run."""
    doc = {"//": LEDGER_HEADER, "findings": entries}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def write_ledger(findings: list, path: pathlib.Path = LEDGER) -> list:
    """Record `findings` as the baseline, keeping reasons already on file and
    dropping entries that no longer match. One entry per distinct `path:line`
    spelling: the same broken spelling cited from several files is one entry.
    A finding whose class changed (missing -> stale or back) gets that class's
    fresh reason rather than the old one. Returns the entries written."""
    previous = {}
    if path.exists():
        old, err = load_ledger(path)
        if err is not None:
            raise SystemExit(f"refusing to overwrite a broken ledger: {err}")
        previous = {(e["path"], e["line"]): e for e in old}
    entries = []
    seen = set()
    for finding in findings:
        key = (finding.cited, finding.line)
        if key in seen:
            continue
        seen.add(key)
        old = previous.get(key)
        reason = (old["reason"]
                  if old is not None and old.get("kind", finding.kind) == finding.kind
                  else REASONS[finding.kind])
        entries.append({
            "path": finding.cited,
            "line": finding.line,
            "kind": finding.kind,
            "reason": reason,
        })
    entries.sort(key=lambda e: (e["path"], e["line"]))
    path.write_text(ledger_text(entries), encoding="utf-8")
    return entries


# ----------------------------------------------------------------- reporting

def build_report(root: pathlib.Path = REPO_ROOT,
                 ledger_path: pathlib.Path = LEDGER) -> Report:
    files, references, findings = examine(root)
    entries, error = load_ledger(ledger_path)
    return Report(files, references, findings, tuple(entries), error)


def _describe(finding: Finding) -> str:
    return (f"{finding.kind:7}  {finding.source}  cites "
            f"{finding.cited}:{finding.line} ({finding.detail})")


def report_text(report: Report, limit: int = 40) -> str:
    """The summary and the findings, as text. Findings recorded in the ledger
    are not printed again; they are already one entry each on disk."""
    lines = []
    markdown = sum(1 for p in report.files if p.suffix == ".md")
    python = len(report.files) - markdown
    kinds = report.counts
    ok = len(report.references) - len(report.findings)
    lines.append(
        f"scanned {len(report.files)} files "
        f"({markdown} markdown, {python} python)")
    lines.append(
        f"references: {len(report.references)} examined "
        f"({ok} ok, {kinds.get('missing', 0)} missing, "
        f"{kinds.get('stale', 0)} stale)")
    if report.ledger_error is not None:
        lines.append(f"ledger: ERROR: {report.ledger_error}")
    else:
        lines.append(f"ledger: {len(report.ledger)} entries "
                     f"for {len(report.findings)} finding occurrences")
    new = report.new_findings
    if new:
        lines.append(f"not in the ledger: {len(new)}")
        for finding in new[:limit]:
            lines.append("  " + _describe(finding))
        if len(new) > limit:
            lines.append(f"  ... and {len(new) - limit} more")
    elif report.ledger_error is None:
        lines.append("not in the ledger: 0")
    obsolete = report.obsolete
    if obsolete and report.ledger_error is None:
        lines.append(
            f"ledger entries that no longer match a finding: {len(obsolete)} "
            "(run --write-ledger to drop them)")
        for entry in obsolete[:5]:
            lines.append(f"  {entry['path']}:{entry['line']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- CLI

def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check hand-written path:line references (issue #2844).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                     help="exit 1 if a finding is not in the ledger")
    mode.add_argument("--write-ledger", "--baseline", dest="write",
                     action="store_true",
                     help="record the current findings as the baseline")
    parser.add_argument("--root", type=pathlib.Path, default=REPO_ROOT,
                        help="repository root to scan (default: this tree)")
    parser.add_argument("--ledger", type=pathlib.Path, default=LEDGER,
                        help="ledger path (default: tools/doc_refs_ledger.json)")
    args = parser.parse_args(argv)

    report = build_report(args.root, args.ledger)

    if args.write:
        entries = write_ledger(list(report.findings), args.ledger)
        print(report_text(Report(report.files, report.references,
                                 report.findings, tuple(entries), None)))
        print(f"\nwrote {len(entries)} findings to {args.ledger}")
        return 0

    if report.ledger_error is not None:
        print(report_text(report))
        print(f"\nRED: {report.ledger_error}", file=sys.stderr)
        print("    A ledger that cannot be read is a RED, not an empty check.",
              file=sys.stderr)
        return 2

    print(report_text(report))
    if args.check and report.new_findings:
        print("\nRED: the findings above are not in the ledger.", file=sys.stderr)
        print("    Repair the reference, or record it with a reason:",
              file=sys.stderr)
        print("    python3 tools/check_doc_refs.py --write-ledger",
              file=sys.stderr)
        return 1
    if args.check:
        print("\nOK: every finding is in the ledger.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

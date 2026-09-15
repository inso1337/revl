#!/usr/bin/env python3
"""Resolves the roadmap's falsifiable claims ABOUT THE TREE against the tree.

`tools/check_roadmap_markers.py` says the limit of the existing gate out loud,
in its own output: "This gate checked that the markers do not CONTRADICT git.
It did not, and cannot, check that any landed branch actually closed the
finding it is attached to." Git answers questions about branches and shas. It
does not answer "does the test this sentence cites still exist", and that is
where the remaining decay lives.

Measured on 2026-09-15, five separate agent reports opened by correcting a
roadmap sentence that git agreed with and the working tree did not. The
sentences were not wrong when written. They went stale when a test was renamed,
a function moved to a shared module, or a named xfail registry was emptied
because the gap it held CLOSED, and nothing connected the edit to the paragraph
that cited it.

WHAT IS CHECKED. Four claim shapes, each one a CITATION: a sentence that points
a reader at an artifact as evidence. A citation is falsifiable in a way prose
is not, and it is the shape the roadmap uses most.

  test    A cited pytest test exists. Node ids (`tests/x.py::test_y`) and bare
          backticked names (`test_y`), which the roadmap uses interchangeably.
          A bare name also resolves to a module `test_y.py`, because the
          roadmap cites files that way too.

  symbol  A backticked identifier cited ALONGSIDE a backticked path occurs in
          that file. Four grammatical shapes carry this pairing:
          "`sym` (`path`)", "`path`'s `sym`", "`sym` in `path`", "`path`: `sym`".
          This is the shape that catches a function that MOVED: item 434's
          `_v3_self_rebind_locals` is still cited at `backends/go/emit.py`
          and has lived in `src/revl/ownership.py` since item 445 shared it.

  path    A `path:line` citation resolves to a file that exists. Only the file
          half is judged. Line numbers drift under every edit and a drifted
          line is not a false claim about the tree, it is a stale coordinate.

  absent  A SCOPED absence claim is actually true: "`path` has no `sym`" and
          "`sym` is absent from `container`". Scoped is the whole point; see
          the next section.

WHAT IS DELIBERATELY NOT CHECKED, AND WHY, WITH THE COUNT THAT DECIDED IT.
The obvious rule here is the unscoped one: find "no `X`" / "`X` is absent" and
fail when `X` is defined anywhere in the tree. It was written, run against the
roadmap, and thrown away. The roadmap contains 98 unscoped "no `X`" subjects.
Filtering to identifier-shaped tokens leaves 58. Of those 58, a definition-site
search finds 54 defined somewhere in the tree — and essentially all 54 are
CORRECT sentences about the revl LANGUAGE, not false claims about the
repository: "no `break`", "no `while`", "no `await`", "no `Secret[T]`". The
identifiers are defined because a compiler's lexer necessarily names every
keyword it refuses. A rule with a 54-out-of-58 false-positive rate reds CI on a
1.4 MB document nobody can quickly fix, which is a bigger tax than the one it
was written to remove. So absence is judged only where the sentence names the
artifact the symbol is supposed to be absent FROM, which makes it a question
about one file instead of a question about English.

WHAT IS SKIPPED, ON PURPOSE.

  Historical citations. "`test_format_emits_string_format` is renamed and
  inverted" is an accurate sentence whose subject deliberately no longer
  exists. A citation inside HISTORY_WINDOW characters of a retrospective cue
  (renamed, deleted, removed, superseded, used to, formerly) is not judged.
  This is the same excision `check_roadmap_markers.py` applies with
  RETROSPECTIVE_RE, for the same reason: a closed finding quotes its own
  original text, and punishing that teaches writers to delete the history.

  Ambiguous and elided paths. The roadmap abbreviates ("`typescript/runtime.ts`"
  for `backends/typescript/runtime.ts`) and elides ("`go/.../bridge.go`"). A
  path is resolved by exact match first, then by unique-or-not suffix match
  over `git ls-files`; when several files match, the claim PASSES if any one
  of them satisfies it. A path containing `...` is not judged at all.

  Hypothetical paths. The roadmap writes example user projects
  (`src/components/agent.rvl`, `./types.ts`, `root/app.rvl`). Backticked paths
  are therefore never judged on their own — only as the anchor of a `symbol`
  claim or with a `:line` attached, both of which are citation shapes a
  hypothetical never takes.

THE ALLOW-LIST. `tools/roadmap_claim_allowlist.json` holds citations that are
genuinely unresolvable in this tree: a file that belongs to a sibling repo, a
test that lives on an unmerged branch. Every entry carries a written reason and
the rule it suppresses, in the shape `.github/ci/known-red.json` uses in the
harness repo. An entry that no longer matches anything is reported, so the
allow-list cannot quietly outlive its reason.

THIS GATE DOES NOT EDIT THE ROADMAP, for the reason the marker gate gives: a
gate that rewrites the thing it checks is a laundering step, not a gate. It
would turn every stale citation into a fresh-looking one with nobody reading
the diff.

Usage:

    python3 tools/check_roadmap_claims.py            # report, always exit 0
    python3 tools/check_roadmap_claims.py --check    # exit 1 on any finding
    python3 tools/check_roadmap_claims.py --rule test --rule symbol
    python3 tools/check_roadmap_claims.py --quiet    # counts only
    python3 tools/check_roadmap_claims.py --roadmap <path> --root <dir>

The default is advisory ON PURPOSE. As of 2026-09-15 the roadmap carries
findings this gate reports, and wiring a red gate into `lint` would block every
open PR on a document only the owner edits. `--check` is the CI mode, to be
switched on once the reported findings are paid down.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROADMAP = ROOT / "docs" / "v2.0-roadmap.md"
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "roadmap_claim_allowlist.json"

RULES = ("test", "symbol", "path", "absent")

# Source extensions a citation can name. Kept narrow: a claim about a `.png`
# or a `.txt` is not a claim this gate knows how to resolve.
SOURCE_EXT = (
    "py|rvl|rs|ts|go|java|mjs|js|json|toml|yml|yaml|sh|wat|wit|md"
)

# A repo-relative path: at least one directory segment, then a file with one of
# the extensions above. The leading segment may start with a dot
# (`.github/workflows/ci.yml`).
PATH = r"(?:[.A-Za-z0-9_-]+/)+[A-Za-z0-9_.-]+\.(?:" + SOURCE_EXT + r")"

# A plain identifier. Anything with brackets, dots, quotes or spaces is prose
# or a type expression, not a symbol this gate can look for.
SYMBOL = r"[A-Za-z_][A-Za-z0-9_]*"

# `tests/test_x.py::test_y`, `test_x.py::test_y[param]`.
NODE_ID_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./-]+\.py)::(?P<name>[A-Za-z0-9_]+)(?:\[[^\]\s]*\])?"
)

# A bare backticked test name. The roadmap cites tests both ways and the bare
# form is the more common one.
BARE_TEST_RE = re.compile(r"`(test_[A-Za-z0-9_]+)`")

# `path:line` and `path:line-line`.
PATH_LINE_RE = re.compile(r"(?P<path>" + PATH + r"):(?P<line>\d+(?:-\d+)?)")

# The four pairings that cite a symbol together with the file it lives in.
# Each entry is (compiled regex, symbol group, path group).
SYMBOL_SITE_RES = (
    (re.compile(r"`(" + SYMBOL + r")`\s*\(`(" + PATH + r")(?::\d+(?:-\d+)?)?`[^)]{0,40}\)"), 1, 2),
    (re.compile(r"`(" + PATH + r")`(?:'s|’s)\s+`(" + SYMBOL + r")`"), 2, 1),
    (re.compile(r"`(" + SYMBOL + r")`\s+in\s+`(" + PATH + r")(?::\d+(?:-\d+)?)?`"), 1, 2),
    (re.compile(r"`(" + PATH + r")`\s*:\s*`(" + SYMBOL + r")`"), 2, 1),
)

# A scoped absence claim. Both forms name the artifact the symbol should be
# missing from, which is what makes them decidable.
ABSENT_RES = (
    re.compile(
        r"`(?P<where>" + PATH + r")`[^.`\n]{0,60}?\bhas\s+no\s+`(?P<sym>" + SYMBOL + r")`"
    ),
    re.compile(
        r"`(?P<sym>" + SYMBOL + r")`\s+(?:is|are)\s+(?:still\s+|now\s+)?absent\s+from\s+"
        r"(?:its\s+|the\s+)?`(?P<where>" + PATH + r")`"
    ),
)

# Retrospective cues. A citation near one of these is narrating the artifact's
# own history and is not judged.
#
# The set is deliberately SMALL. Each cue was checked against what it
# suppresses in the real roadmap: `dropped` and `removed` were tried and taken
# back out, because both are ordinary roadmap vocabulary ("rather than dropped
# from the corpus") and each one silenced a citation that is genuinely stale.
# A cue earns its place by naming the CITED ARTIFACT's own lifecycle.
HISTORY_RE = re.compile(
    r"""(
          \brenamed\b | \brename[sd]?\s+to\b
        | \bdelet(?:e|es|ed|ing|ion)\b
        | \bsuperseded\b | \breplaced\s+by\b | \bin\s+its\s+place\b
        | \bused\s+to\b | \bformerly\b | \bpreviously\b
        | \bno\s+longer\s+exists\b
        | \bwas\s+the\s+name\b | \bold\s+name\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# How far either side of a citation a retrospective cue still governs it. One
# long roadmap sentence; short enough not to reach the next finding block.
HISTORY_WINDOW = 260


# --------------------------------------------------------------------------
# The tree the claims are resolved against.
# --------------------------------------------------------------------------
class Tree:
    """The repository, indexed once for the whole run."""

    def __init__(self, root: Path, files: Sequence[str]) -> None:
        self.root = root
        self.files = list(files)
        self._by_suffix: Dict[str, List[str]] = {}
        for rel in self.files:
            parts = rel.split("/")
            for i in range(len(parts)):
                self._by_suffix.setdefault("/".join(parts[i:]), []).append(rel)
        self._text: Dict[str, str] = {}

    @classmethod
    def from_git(cls, root: Path) -> "Tree":
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout.split("\n")
        return cls(root, [line for line in out if line])

    def text(self, rel: str) -> str:
        if rel not in self._text:
            try:
                self._text[rel] = (self.root / rel).read_text(errors="ignore")
            except OSError:
                self._text[rel] = ""
        return self._text[rel]

    def resolve(self, cited: str) -> Optional[List[str]]:
        """Candidate real paths for a cited path, or None when unjudgeable.

        None means the citation elides a middle segment (`go/.../bridge.go`),
        which no lookup can honestly expand. An empty list means the path
        matched nothing, which IS a finding.
        """
        if "..." in cited:
            return None
        if cited in self._by_suffix and cited in set(self.files):
            return [cited]
        return list(self._by_suffix.get(cited, ()))

    def defines_test(self, name: str) -> bool:
        pat = re.compile(r"^\s*(?:async\s+)?def\s+" + re.escape(name) + r"\b", re.M)
        return any(pat.search(self.text(rel)) for rel in self.files if rel.endswith(".py"))

    def has_module(self, name: str) -> bool:
        return bool(self._by_suffix.get(name + ".py"))

    def mentions_test(self, name: str) -> bool:
        """A test name carried by a non-python tier (rust `#[test] fn test_x`,
        a go `t.Run("test_x")`, a parametrised id built from data)."""
        word = re.compile(r"\b" + re.escape(name) + r"\b")
        return any(
            word.search(self.text(rel))
            for rel in self.files
            if rel.endswith((".rs", ".go", ".ts", ".java", ".mjs", ".rvl", ".py"))
        )


# --------------------------------------------------------------------------
# Claims.
# --------------------------------------------------------------------------
class Claim:
    """One falsifiable citation, with where it sits so a human can find it."""

    def __init__(self, rule: str, key: str, line: int, text: str) -> None:
        self.rule = rule
        self.key = key
        self.line = line
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Claim(%s, %r, line=%d)" % (self.rule, self.key, self.line)


def _line_of(source: str, index: int) -> int:
    return source.count("\n", 0, index) + 1


def _is_historical(source: str, index: int) -> bool:
    lo = max(0, index - HISTORY_WINDOW)
    hi = min(len(source), index + HISTORY_WINDOW)
    return bool(HISTORY_RE.search(source[lo:hi]))


def collect_test_claims(source: str) -> List[Claim]:
    claims: List[Claim] = []
    seen: Set[str] = set()
    for m in NODE_ID_RE.finditer(source):
        path, name = m.group("path"), m.group("name")
        base = path.rsplit("/", 1)[-1]
        # `lower.py::_link` is prose shorthand for a function in a module, not
        # a pytest node. Only a test file naming a test function is judged.
        if not (base.startswith("test_") and name.startswith("test_")):
            continue
        if _is_historical(source, m.start()):
            continue
        key = path + "::" + name
        if key in seen:
            continue
        seen.add(key)
        claims.append(Claim("test", key, _line_of(source, m.start()), m.group(0)))
    for m in BARE_TEST_RE.finditer(source):
        name = m.group(1)
        if _is_historical(source, m.start()):
            continue
        if name in seen:
            continue
        seen.add(name)
        claims.append(Claim("test", name, _line_of(source, m.start()), m.group(0)))
    return claims


def collect_symbol_claims(source: str) -> List[Claim]:
    claims: List[Claim] = []
    seen: Set[str] = set()
    for pattern, sym_group, path_group in SYMBOL_SITE_RES:
        for m in pattern.finditer(source):
            sym, path = m.group(sym_group), m.group(path_group)
            if _is_historical(source, m.start()):
                continue
            key = sym + "@" + path
            if key in seen:
                continue
            seen.add(key)
            claims.append(Claim("symbol", key, _line_of(source, m.start()), m.group(0)))
    return claims


def collect_path_claims(source: str) -> List[Claim]:
    claims: List[Claim] = []
    seen: Set[str] = set()
    for m in PATH_LINE_RE.finditer(source):
        path = m.group("path")
        if _is_historical(source, m.start()):
            continue
        if path in seen:
            continue
        seen.add(path)
        claims.append(Claim("path", path, _line_of(source, m.start()), m.group(0)))
    return claims


def collect_absent_claims(source: str) -> List[Claim]:
    claims: List[Claim] = []
    seen: Set[str] = set()
    for pattern in ABSENT_RES:
        for m in pattern.finditer(source):
            sym, where = m.group("sym"), m.group("where")
            if _is_historical(source, m.start()):
                continue
            key = sym + "@" + where
            if key in seen:
                continue
            seen.add(key)
            claims.append(Claim("absent", key, _line_of(source, m.start()), m.group(0)))
    return claims


COLLECTORS = {
    "test": collect_test_claims,
    "symbol": collect_symbol_claims,
    "path": collect_path_claims,
    "absent": collect_absent_claims,
}


# --------------------------------------------------------------------------
# Judging.
# --------------------------------------------------------------------------
def judge_test(claim: Claim, tree: Tree) -> Optional[str]:
    if "::" in claim.key:
        path, name = claim.key.split("::", 1)
        candidates = tree.resolve(path)
        if candidates is None:
            return None
        if not candidates:
            base = path.rsplit("/", 1)[-1]
            candidates = tree.resolve(base) or []
        if not candidates:
            return "cites %s, and no file in the tree matches %s" % (claim.key, path)
        pat = re.compile(r"^\s*(?:async\s+)?def\s+" + re.escape(name) + r"\b", re.M)
        if any(pat.search(tree.text(rel)) for rel in candidates):
            return None
        return "cites %s, and %s defines no %s" % (
            claim.key, " / ".join(candidates), name,
        )
    name = claim.key
    if tree.defines_test(name) or tree.has_module(name) or tree.mentions_test(name):
        return None
    return "cites the test `%s`, which the tree defines nowhere" % name


def judge_symbol(claim: Claim, tree: Tree) -> Optional[str]:
    sym, path = claim.key.split("@", 1)
    candidates = tree.resolve(path)
    if candidates is None:
        return None
    if not candidates:
        return "cites `%s` at `%s`, and no file in the tree matches that path" % (sym, path)
    word = re.compile(r"\b" + re.escape(sym) + r"\b")
    if any(word.search(tree.text(rel)) for rel in candidates):
        return None
    return "cites `%s` in `%s`, which does not contain it" % (
        sym, " / ".join(candidates),
    )


def judge_path(claim: Claim, tree: Tree) -> Optional[str]:
    candidates = tree.resolve(claim.key)
    if candidates is None or candidates:
        return None
    return "cites `%s`, which is not a file in the tree" % claim.key


def judge_absent(claim: Claim, tree: Tree) -> Optional[str]:
    sym, path = claim.key.split("@", 1)
    candidates = tree.resolve(path)
    if candidates is None or not candidates:
        return None
    word = re.compile(r"\b" + re.escape(sym) + r"\b")
    present = [rel for rel in candidates if word.search(tree.text(rel))]
    if not present:
        return None
    return "says `%s` is absent from `%s`, which contains it" % (
        sym, " / ".join(present),
    )


JUDGES = {
    "test": judge_test,
    "symbol": judge_symbol,
    "path": judge_path,
    "absent": judge_absent,
}


# --------------------------------------------------------------------------
# The allow-list.
# --------------------------------------------------------------------------
def load_allowlist(path: Path) -> List[dict]:
    if not path.exists():
        return []
    entries = json.loads(path.read_text())["entries"]
    for entry in entries:
        missing = {"rule", "claim", "reason"} - set(entry)
        if missing:
            raise ValueError(
                "allow-list entry %r is missing %s; every entry carries a "
                "written reason" % (entry, sorted(missing))
            )
        if not entry["reason"].strip():
            raise ValueError("allow-list entry %r has an empty reason" % entry)
    return entries


def run(
    source: str,
    tree: Tree,
    rules: Iterable[str] = RULES,
    allowlist: Optional[Sequence[dict]] = None,
) -> Tuple[Dict[str, int], List[Tuple[Claim, str]], List[dict]]:
    """Return (denominator per rule, findings, allow-list entries that matched
    nothing)."""
    allowlist = list(allowlist or ())
    suppressed = {(e["rule"], e["claim"]): e for e in allowlist}
    used: Set[Tuple[str, str]] = set()
    denominator: Dict[str, int] = {}
    findings: List[Tuple[Claim, str]] = []
    for rule in rules:
        claims = COLLECTORS[rule](source)
        denominator[rule] = len(claims)
        for claim in claims:
            verdict = JUDGES[rule](claim, tree)
            if verdict is None:
                continue
            if (rule, claim.key) in suppressed:
                used.add((rule, claim.key))
                continue
            findings.append((claim, verdict))
    unused = [e for e in allowlist if (e["rule"], e["claim"]) not in used
              and e["rule"] in set(rules)]
    return denominator, findings, unused


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--roadmap", type=Path, default=DEFAULT_ROADMAP)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--rule", action="append", choices=RULES, dest="rules")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 on any finding (the CI mode)")
    parser.add_argument("--quiet", action="store_true", help="counts only")
    args = parser.parse_args(argv)

    rules = args.rules or list(RULES)
    tree = Tree.from_git(args.root)
    source = args.roadmap.read_text()
    denominator, findings, unused = run(
        source, tree, rules, load_allowlist(args.allowlist)
    )

    by_rule: Dict[str, List[Tuple[Claim, str]]] = {rule: [] for rule in rules}
    for claim, verdict in findings:
        by_rule[claim.rule].append((claim, verdict))

    print("%s: %d claims checked over %d rules"
          % (args.roadmap.name, sum(denominator.values()), len(rules)))
    for rule in rules:
        print("  %-7s %4d claims, %d stale"
              % (rule, denominator[rule], len(by_rule[rule])))

    if not args.quiet:
        for rule in rules:
            for claim, verdict in by_rule[rule]:
                print("\n%s:%d  [%s]\n  %s\n  %s"
                      % (args.roadmap.name, claim.line, rule, claim.text.strip(), verdict))
        for entry in unused:
            print("\nallow-list entry matches nothing and can be removed: "
                  "[%s] %s" % (entry["rule"], entry["claim"]))

    print("\nThis gate resolved CITATIONS. It did not, and cannot, check that a "
          "sentence whose citations all resolve is TRUE.")
    if findings and not args.check:
        print("Advisory run: %d finding(s), exit 0. Use --check for the CI mode."
              % len(findings))
    return 1 if (args.check and findings) else 0


if __name__ == "__main__":
    sys.exit(main())

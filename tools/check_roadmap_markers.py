#!/usr/bin/env python3
"""Staleness gate for the in-progress markers in docs/v2.0-roadmap.md.

The roadmap is the project's state-of-record and it decays silently, because
its state is PROSE that a human has to remember to edit. Measured on
2026-09-02: twelve per-finding markers still read "FIXING on `fix/<branch>`"
for branches that had already landed on main, five items were already closed
behind a stale in-progress marker, and rediscovering that cost an agent a full
investigation each time. Two agents fixed the same defect independently because
neither could see the other's scope.

This is the same contract the repo already applies to generated artifacts
(`tools/conformance.py --check-readme`, `tools/check_site_wheel.py`,
`tools/build_gate_crate.py --check`, the formal layer's non-vacuity registry):
a claim that can be checked mechanically must be. Here the claim is "this
finding is currently being fixed on branch X", and git is the oracle.

Three things fail the gate:

  1. A marker says the work is in flight and names a branch that IS an
     ancestor of the base ref (default `origin/main`). The work landed; the
     marker lies.
  2. A marker names a branch that no longer exists on the remote. A reader
     cannot follow it, so the marker points at nothing.
  3. A marker cites a commit sha that is a commit in THIS repo but is not
     reachable from the base ref.

WHAT THIS GATE CANNOT KNOW, and will not pretend to know. It cannot tell
whether a landed branch actually CLOSED the finding its marker is attached to.
A branch merges for many reasons: it can fix one instance of a defect and leave
forty-seven exposed, it can de-collide one pair of test roots while the suite
still dies at collection, it can be a partial slice, or it can be reverted
later in a way ancestry still reports as merged. So a finding here means "this
sentence contradicts git", never "this item is done". The fix is to go READ the
branch's diff and record which instances it covers, then reword the marker to
say what is actually true. Never rewrite the marker to silence this gate.

For the same reason this gate does NOT edit the roadmap. A gate that rewrites
the thing it checks is not a gate, it is a laundering step: it would convert
every stale claim into a fresh-looking claim without anyone reading the diff.

The second gate, OFF by default:

    python3 tools/check_roadmap_markers.py --require-issue

requires every OPEN or PARTIAL top-level item to cite a GitHub issue. The
project is moving to GitHub issues as the state-of-record, with the roadmap
keeping the reasoning, evidence and cross-references. This flag is what makes
that stick. It cannot be turned on until the migration happens, so it ships
disabled. See CONTRIBUTING.md, "Tracking work".

Usage:

    python3 tools/check_roadmap_markers.py               # the staleness gate
    python3 tools/check_roadmap_markers.py --require-issue
    python3 tools/check_roadmap_markers.py --base <ref>  # default origin/main
    python3 tools/check_roadmap_markers.py --no-fetch    # offline; may be stale
    python3 tools/check_roadmap_markers.py --roadmap <path>

Exit status is 0 when every marker agrees with git, 1 when any does not, and
2 when the environment cannot answer the question (no git, no base ref).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROADMAP = ROOT / "docs" / "v2.0-roadmap.md"
DEFAULT_BASE = "origin/main"

# Phrases that assert work is CURRENTLY under way. A marker only produces a
# finding when a branch reference follows one of these inside WINDOW
# characters, so a broad phrase ("fixing item 420") costs nothing: it names no
# branch, so there is nothing for git to contradict.
#
# The 🚧 glyph is NOT here. It is a status glyph, and the roadmap quotes it in
# past tense inside closed items ("✅ LANDED (was 🚧; ... on main)"), which is
# an accurate sentence, not a stale one. It is picked up separately, in
# collect_markers, only where it is an item's leading status glyph.
MARKER_RE = re.compile(
    r"""(
          \bFIXING\b
        | \bbeing\s+fixed\b
        | \bfixing\s+on\b
        | \bin\s+flight\b
        | \bin[\s-]progress\b
        | \bunderway\b
        | \bWIP\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# How far after a marker phrase a branch reference still counts as that
# marker's branch. Long enough for "Being fixed on branch <name>." across a
# wrapped line, short enough not to reach the next sentence's evidence.
WINDOW = 200

# A branch reference, optionally in backticks. Filtered further by
# _looks_like_branch: repo paths, filenames, and ordinary prose alternatives
# ("undo/compensate", "emit/effect") match this shape too.
BRANCH_RE = re.compile(r"`?((?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+)`?")

# Branch namespaces this repo actually uses (CONTRIBUTING.md, "The branch
# model"). The live set on origin is unioned in at run time, so the list grows
# by itself; the baseline is here so a namespace whose every branch has been
# deleted is still recognised, which is exactly the case rule 2 exists for.
BASELINE_NAMESPACES = frozenset({
    "agent", "audit", "bench", "chore", "design", "feat", "fix", "hardening",
    "hotfix", "integrate", "item", "perf", "refactor", "release", "revert",
    "test", "wave", "wip",
})

# A backticked hex token. Foreign-repo pins (cordis-py, cordis-wasm, stc-go)
# are spelled the same way, so a token that is not a commit in THIS repo is
# ignored rather than guessed at: see _sha_findings.
SHA_RE = re.compile(r"`([0-9a-f]{7,40})`")

# `use "stdlib/x.rvl"`, `docs/design/foo.md`, `backends/python/emit.py:279`.
FILE_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")

# A top-level item: "417. ◑ **...**". Numbers restart per section, so an item
# is identified by its line number as well as its number.
ITEM_RE = re.compile(r"^(\d+)\.\s+(.*)$")
DONE_GLYPHS = ("✅",)          # done
PARTIAL_GLYPHS = ("◑",)       # partly done
INFLIGHT_GLYPHS = ("\U0001F6A7",)  # in progress
DROPPED_GLYPHS = ("➖",)       # declined

# An explicit GitHub ISSUE citation. A bare "#58" is not accepted: the roadmap
# already cites merged PRs that way, and a PR reference is not a tracked state
# record.
ISSUE_RE = re.compile(
    r"""(
          github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/issues/\d+
        | \bGH-\d+\b
        | \bissues?\s+\#\d+
        | \bissues?\#\d+
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Sections whose items are historical record, not tracked work.
UNTRACKED_SECTIONS = ("Done (dependency order as built)", "Declined, deliberately")


class Git:
    """The oracle. Every question this gate asks git goes through here."""

    def __init__(self, repo: Path, base: str, allow_fetch: bool) -> None:
        self.repo = repo
        self.base = base
        self.allow_fetch = allow_fetch
        self.fetched = False
        self._remote_heads: set[str] | None = None
        self._notes: list[str] = []

    def run(self, *args: str) -> tuple[int, str]:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout.strip()

    def ok(self, *args: str) -> bool:
        return self.run(*args)[0] == 0

    def is_shallow(self) -> bool:
        return self.run("rev-parse", "--is-shallow-repository")[1] == "true"

    def fetch(self) -> None:
        """Fetch every remote head once, deepening a shallow clone.

        CI checks out at depth 1 for a single branch, so `origin/main` and the
        marker branches are simply absent and every ancestry answer would be
        wrong. Doing this inside the tool keeps the CI change to one step.
        """
        if self.fetched or not self.allow_fetch:
            return
        self.fetched = True
        args = ["fetch", "--no-tags", "--prune", "origin",
                "+refs/heads/*:refs/remotes/origin/*"]
        if self.is_shallow():
            args.insert(1, "--unshallow")
        code, _ = self.run(*args)
        if code != 0:
            self._notes.append(
                "note: `git fetch` failed (offline?); answers below come from "
                "the remote-tracking refs already in this checkout and may be stale"
            )
        self._remote_heads = None

    def rev(self, ref: str) -> str | None:
        code, out = self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return out or None if code == 0 else None

    def base_rev(self) -> str | None:
        got = self.rev(self.base)
        if got is None and self.allow_fetch:
            self.fetch()
            got = self.rev(self.base)
        return got

    def remote_heads(self) -> set[str]:
        """Branch names on origin, from `git ls-remote` when reachable."""
        if self._remote_heads is not None:
            return self._remote_heads
        heads: set[str] = set()
        if self.allow_fetch:
            code, out = self.run("ls-remote", "--heads", "origin")
            if code == 0 and out:
                for line in out.splitlines():
                    parts = line.split("\trefs/heads/")
                    if len(parts) == 2:
                        heads.add(parts[1])
                self._remote_heads = heads
                return heads
            self._notes.append(
                "note: `git ls-remote` did not answer; branch existence below is "
                "read from local remote-tracking refs and may be stale"
            )
        code, out = self.run("for-each-ref", "--format=%(refname:strip=3)",
                             "refs/remotes/origin/")
        if code == 0:
            heads = {b for b in out.splitlines() if b and b != "HEAD"}
        self._remote_heads = heads
        return heads

    def is_ancestor(self, ref: str) -> bool | None:
        """True/False, or None when the commit is not in this checkout."""
        rev = self.rev(ref)
        if rev is None:
            self.fetch()
            rev = self.rev(ref)
        if rev is None:
            return None
        return self.ok("merge-base", "--is-ancestor", rev, self.base)

    def is_commit(self, sha: str) -> bool:
        return self.run("cat-file", "-t", sha) == (0, "commit")

    def containing_refs(self, sha: str) -> list[str]:
        code, out = self.run("for-each-ref", "--contains", sha,
                             "--format=%(refname:strip=2)", "refs/remotes/origin/")
        return [r for r in out.splitlines() if r] if code == 0 else []

    def notes(self) -> list[str]:
        return list(self._notes)


def repo_dirs(repo: Path) -> set[str]:
    """Top-level directory names, which a branch prefix must not collide with.

    `docs/design/411-sandbox-placement.md` and `fix/421-f7-temporal-residue`
    are the same shape. The difference that holds is that one starts with a
    directory that exists in the tree and the other does not.
    """
    return {p.name for p in repo.iterdir() if p.is_dir()}


def _clean_branch(token: str) -> str:
    """Drop the sentence punctuation a branch name picks up in prose.

    "Being fixed on branch agent/any-field-provide-method." names the branch
    without the full stop.
    """
    return token.rstrip(".,;:!?)]}'\"")


def _looks_like_branch(token: str, dirs: set[str], namespaces: set[str],
                       heads: set[str]) -> bool:
    """Is this token a branch reference rather than a path or a prose slash?

    A token that names a live branch on origin is one, full stop. Otherwise
    three filters, in order of how much noise each removes:
      - the namespace must be one the repo uses for branches, which rejects
        prose alternatives such as "undo/compensate" and "emit/effect";
      - it must not start with a directory that exists in the tree, which
        rejects "docs/design/411-sandbox-placement.md" and friends;
      - the last segment must not look like a filename.

    KNOWN LIMITATIONS, both of the same shape: a DELETED branch is only
    recognisable from its spelling, so one whose namespace collides with a
    tree directory (`docs/reconcile-finding-markers`) or that has no namespace
    at all (`item-300-ungate-cold-load`, a few of which are on origin) reads
    as prose once it is gone. Namespace your branches, outside `docs/`.
    """
    if token in heads:
        return True
    head = token.split("/", 1)[0]
    if head in dirs or head.startswith("."):
        return False
    if head not in namespaces:
        return False
    if FILE_SUFFIX_RE.search(token.rsplit("/", 1)[-1]):
        return False
    return len(token) >= 8


def paragraphs(text: str) -> list[tuple[int, str]]:
    """Blank-line-separated blocks as (first line number, joined text).

    Items wrap across lines, so "Being fixed on branch\\nagent/x" has to read
    as one string or the branch is invisible to the window scan.
    """
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if not buf:
                start = lineno
            buf.append(line.strip())
        elif buf:
            out.append((start, " ".join(buf)))
            buf = []
    if buf:
        out.append((start, " ".join(buf)))
    return out


def _first_branch(window: str, dirs: set[str], namespaces: set[str],
                  heads: set[str]) -> str | None:
    for b in BRANCH_RE.finditer(window):
        token = _clean_branch(b.group(1))
        if _looks_like_branch(token, dirs, namespaces, heads):
            return token
    return None


def collect_markers(text: str, dirs: set[str], namespaces: set[str],
                    heads: set[str]) -> list[dict]:
    """Every (in-progress claim, branch it names) pair in the file.

    Two sources: an in-progress PHRASE followed within WINDOW characters by a
    branch, and a top-level item whose leading status glyph is 🚧.
    """
    found: list[dict] = []
    for start, para in paragraphs(text):
        for m in MARKER_RE.finditer(para):
            branch = _first_branch(para[m.end(): m.end() + WINDOW], dirs,
                                   namespaces, heads)
            if branch is None:
                continue
            found.append({
                "line": start,
                "phrase": m.group(1).strip(),
                "branch": branch,
                "quote": para[max(0, m.start() - 40): m.end() + 120],
            })
    for it in items(text):
        if it["status"] != "in-flight":
            continue
        branch = _first_branch(it["body"][:WINDOW * 2], dirs, namespaces, heads)
        if branch is None:
            continue
        found.append({
            "line": it["line"],
            "phrase": f"item {it['number']} carries the in-progress glyph",
            "branch": branch,
            "quote": re.sub(r"\s+", " ", it["body"])[:160],
        })
    return found


def _dedupe(markers: list[dict]) -> list[dict]:
    seen: set[tuple[int, str]] = set()
    out = []
    for mk in markers:
        key = (mk["line"], mk["branch"])
        if key not in seen:
            seen.add(key)
            out.append(mk)
    return out


def branch_findings(markers: list[dict], git: Git) -> list[str]:
    findings: list[str] = []
    heads = git.remote_heads()
    for mk in _dedupe(markers):
        branch = mk["branch"]
        where = f"{mk['line']}"
        if branch not in heads:
            findings.append(
                f"L{where}: marker says {mk['phrase']!r} on branch {branch!r}, "
                f"which does not exist on origin.\n"
                f"    quote: ...{mk['quote']}...\n"
                f"    A reader cannot follow this marker. The branch was deleted, "
                f"renamed, or never pushed."
            )
            continue
        anc = git.is_ancestor(f"origin/{branch}")
        if anc is None:
            findings.append(
                f"L{where}: branch {branch!r} exists on origin but its commits are "
                f"not in this checkout, so ancestry cannot be decided. Run without "
                f"--no-fetch, or fetch it."
            )
        elif anc:
            findings.append(
                f"L{where}: marker says {mk['phrase']!r} on branch {branch!r}, "
                f"but origin/{branch} is an ANCESTOR of {git.base}. It landed.\n"
                f"    quote: ...{mk['quote']}...\n"
                f"    Read the branch's diff, record which instances of the finding "
                f"it covers and which it does not, then reword the marker in the "
                f"past tense (for example: 'landed via {branch}')."
            )
    return findings


def sha_findings(text: str, git: Git) -> list[str]:
    """Shas cited in the roadmap that are commits here but not on the base ref.

    A hex token that is not a commit in this repo is IGNORED, not guessed at:
    the roadmap pins foreign repos the same way (`inso1337/cordis-py@... 1c5e6f1`,
    the cordis-wasm B3 commit), and `ed25519` is a valid hex string. This gate
    can only speak for this repository's history.
    """
    findings: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in SHA_RE.finditer(line):
            sha = m.group(1)
            if sha in seen or not git.is_commit(sha):
                continue
            seen.add(sha)
            if git.ok("merge-base", "--is-ancestor", sha, git.base):
                continue
            refs = git.containing_refs(sha) or ["(no origin branch)"]
            findings.append(
                f"L{lineno}: cites commit `{sha}`, which is NOT reachable from "
                f"{git.base}.\n"
                f"    reachable from: {', '.join(refs[:5])}\n"
                f"    Either the work is not on main and the text should not imply "
                f"it is, or the commit was rebased away and the sha needs replacing "
                f"with the one that landed."
            )
    return findings


def items(text: str) -> list[dict]:
    """Top-level roadmap items with their status glyph and body."""
    lines = text.splitlines()
    section = ""
    out: list[dict] = []
    for lineno, line in enumerate(lines, 1):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = ITEM_RE.match(line)
        if not m:
            continue
        rest = m.group(2)
        head = rest[:2]
        if any(head.startswith(g) for g in DONE_GLYPHS):
            status = "done"
        elif any(head.startswith(g) for g in PARTIAL_GLYPHS):
            status = "partial"
        elif any(head.startswith(g) for g in INFLIGHT_GLYPHS):
            status = "in-flight"
        elif any(head.startswith(g) for g in DROPPED_GLYPHS):
            status = "dropped"
        else:
            status = "open"
        out.append({
            "line": lineno,
            "number": m.group(1),
            "status": status,
            "section": section,
            "text": rest,
        })
    # An item's body runs to the next item or the next section heading.
    starts = [it["line"] for it in out] + [len(lines) + 1]
    for idx, it in enumerate(out):
        body = []
        for lineno in range(it["line"], starts[idx + 1]):
            if lineno > it["line"] and lines[lineno - 1].startswith("## "):
                break
            body.append(lines[lineno - 1])
        it["body"] = "\n".join(body)
    return out


def issue_findings(text: str) -> list[str]:
    findings: list[str] = []
    for it in items(text):
        if it["status"] not in ("open", "partial"):
            continue
        if it["section"] in UNTRACKED_SECTIONS:
            continue
        if ISSUE_RE.search(it["body"]):
            continue
        title = re.sub(r"\s+", " ", it["text"])[:96]
        findings.append(
            f"L{it['line']}: item {it['number']} ({it['status']}, section "
            f"{it['section']!r}) cites no GitHub issue.\n    {title}"
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fail when docs/v2.0-roadmap.md's markers contradict git.",
    )
    ap.add_argument("--roadmap", type=Path, default=DEFAULT_ROADMAP)
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help="ref the roadmap's claims are measured against "
                         f"(default {DEFAULT_BASE})")
    ap.add_argument("--no-fetch", action="store_true",
                    help="never touch the network; verdicts may be stale")
    ap.add_argument("--require-issue", action="store_true",
                    help="ALSO require every open or partial top-level item to "
                         "cite a GitHub issue. Off until the issue migration "
                         "happens; see CONTRIBUTING.md 'Tracking work'.")
    args = ap.parse_args(argv)

    roadmap = args.roadmap
    if not roadmap.is_file():
        print(f"error: no such roadmap file: {roadmap}", file=sys.stderr)
        return 2
    text = roadmap.read_text(encoding="utf-8")

    git = Git(ROOT, args.base, allow_fetch=not args.no_fetch)
    if not git.ok("rev-parse", "--git-dir"):
        print(f"error: {ROOT} is not a git checkout; this gate needs git as its "
              f"oracle", file=sys.stderr)
        return 2
    if git.base_rev() is None:
        print(f"error: base ref {args.base!r} is not resolvable. Fetch it, or "
              f"pass --base with a ref that exists.", file=sys.stderr)
        return 2

    dirs = repo_dirs(ROOT)
    heads = git.remote_heads()
    namespaces = set(BASELINE_NAMESPACES)
    namespaces |= {h.split("/", 1)[0] for h in heads if "/" in h}
    namespaces -= dirs
    markers = collect_markers(text, dirs, namespaces, heads)
    findings = branch_findings(markers, git) + sha_findings(text, git)
    if args.require_issue:
        extra = issue_findings(text)
        if extra:
            findings.append(
                f"--require-issue: {len(extra)} open/partial item(s) cite no "
                f"GitHub issue:\n" + "\n".join("  " + e for e in extra)
            )

    for note in git.notes():
        print(note)

    scanned = len(_dedupe(markers))
    if not findings:
        print(f"roadmap markers OK: {scanned} in-progress marker(s) with a named "
              f"branch, all consistent with {args.base}.")
        print("This gate checked that the markers do not CONTRADICT git. It did "
              "not, and cannot, check that any landed branch actually closed the "
              "finding it is attached to.")
        return 0

    print(f"{roadmap}: {len(findings)} finding(s) against {args.base} "
          f"({scanned} in-progress marker(s) named a branch)\n")
    for f in findings:
        print(f"  - {f}\n")
    print(
        "The roadmap's prose contradicts git. Fix the PROSE, not this gate.\n"
        "\n"
        "This gate cannot tell whether a landed branch actually closed the\n"
        "finding its marker sits on: a merge can fix one instance and leave\n"
        "the rest exposed. Do not read a finding here as 'the item is done'.\n"
        "Read the branch's diff, write down which instances it covers and\n"
        "which it does not, and reword the marker to say that.\n"
        "\n"
        "This gate deliberately does not rewrite markers for you. Auto-\n"
        "rewriting would turn every stale claim into a fresh-looking claim\n"
        "with nobody having read the diff, which is the failure it exists to\n"
        "catch."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

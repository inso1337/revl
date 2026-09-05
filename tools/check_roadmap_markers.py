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

A fourth fails it only in the PULL REQUEST context, under `--head-branch`:

  4. A marker says the work is IN FLIGHT on the PR's OWN head branch. Rules 1
     to 3 catch a marker that is already stale. This catches the one that is
     about to become stale: the merge deletes the branch, so the marker the PR
     introduced reddens main's `lint` on landing, and with it every open PR
     whose merge-ref carries the new text. Past-tense self-naming ("landed via
     `fix/x`") is a historical statement that survives the deletion and does
     NOT fail: only the in-flight phrasing does. See `self_branch_findings`.

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

FIVE MORE GATES, each OFF by default and each with its own flag, added
2026-09-02 after five real fall-throughs that the git oracle above is
structurally blind to. Git can only answer questions about branches and shas.
These five answer questions about the roadmap's prose against ITSELF and
against the tree, which is where the remaining decay lives.

  --check-contradiction   (A) SELF-CONTRADICTION. Item 422's header read
      "ALL SEVEN FINDINGS FIXED" while its own body said F1-F4 were untouched
      on the typescript tier. The header was believed and a disclosed CRITICAL
      sat unowned. Fails when a closure claim's own scope of text contradicts
      it. A closed finding legitimately QUOTES its original text in past
      tense, so retrospective regions are excised before the scan; see
      RETROSPECTIVE_RE and _past_tense_governed.

  --check-delegation      (B) DANGLING DELEGATION. Item 425 F3 read "folded
      into the item-416c fix" and item 427 F5 said the same, while the
      residual was refiled onto owners that were about a different defect.
      The finding looked tracked, was closed nowhere, and had no live owner.
      Fails when a delegation that carries a closure claim points at a
      target that does not exist, at a target that is itself CLOSED, or at a
      target that is open and has no branch, issue, sha or owner of its own.
      All three are the same defect: a pointer at something nobody is
      working. Item 416's block (c) is the third kind, headed "HYPOTHESES,
      unexecuted", which is where 425 F3 and 427 F5 both point today.

  --check-orphan          (C) ORPHAN. A per-finding check, deliberately NOT
      the same question as --require-issue (which is per-ITEM and demands a
      GitHub issue specifically). This one asks whether an open or delegated
      FINDING names anything at all that a reader could follow to a live
      owner: a branch, an issue, an explicit owner, or a sha cited as a FIX.
      A sha cited only as the revision the finding was RE-VERIFIED on is
      recorded but does not count: it evidences that the finding is still
      live, not that anyone is closing it. It is available now, before the
      issue migration lands.

  --check-duplicate-headers  (E) DUPLICATE ITEM HEADER. Merge `bd0f4d19`
      resolved a roadmap hunk by keeping BOTH sides, so item 106 carried a
      truncated copy of its own header, marked in progress, one line above
      the done entry that had replaced it. The stale line had no body and was
      the ONLY reason a landed item read as open; nothing caught it because
      the staleness gate above validates markers that NAME A BRANCH and this
      one named nothing. Fails when one item NUMBER carries more than one
      block inside one section, and says which of the three shapes it is:
      merge residue, conflicting statuses, or two different items sharing a
      number. Numbers restart per section by design, so cross-section reuse
      is never reported. See duplicate_header_findings. It also asks the same
      question one level down, inside a single item: an F-labelled finding
      block written twice, the identical merge-both-sides shape below the item
      header. Item 428 carries F5-F13 twice and item 433 carries F1-F10 twice,
      each kept in duplicate when a merge resolved the finding-set hunk by
      keeping both sides, and again the first copy is the one a reader
      believes. A lettered sub-finding (item 421's F6 beside its F6(b)) is a
      distinct block and is not reported. See duplicate_finding_blocks.

  --check-tier-parity     (D) CROSS-TIER PARITY, the shape only. Three
      defects were fixed on the python tier on 2026-09-02 and left live
      everywhere else: item 422's confinement guard (python at `1602cc94`,
      typescript still exposed, CRITICAL), item 421 F6's secret marking, and
      the self-host taint stage. A gate CANNOT decide semantic parity, and
      this one does not try. It flags one shape and one only: a finding that
      claims closure, cites `backends/<tier>/` paths for exactly ONE tier,
      speaks about a guarantee this project states language-wide, and never
      names a second tier anywhere in its own text. Naming the second tier is
      the escape hatch, and it is also the discipline: if a fix really is one
      tier wide, the finding has to say so. It does NOT see the self-host
      tier, which is not a `backends/` directory, so the taint case above is
      outside it. See TIER_SUBJECTS and tier_parity_findings for the exact
      bound and all three blind spots.

None of the five decides whether a fix is CORRECT, and none of them rewrites
anything, for the same reason the staleness gate does not: a gate that edits
the thing it checks launders stale claims into fresh-looking ones.

Usage:

    python3 tools/check_roadmap_markers.py               # the staleness gate
    python3 tools/check_roadmap_markers.py --require-issue
    python3 tools/check_roadmap_markers.py --check-contradiction
    python3 tools/check_roadmap_markers.py --check-delegation
    python3 tools/check_roadmap_markers.py --check-orphan
    python3 tools/check_roadmap_markers.py --check-tier-parity
    python3 tools/check_roadmap_markers.py --check-duplicate-headers
    python3 tools/check_roadmap_markers.py --base <ref>  # default origin/main
    python3 tools/check_roadmap_markers.py --no-fetch    # offline; may be stale
    python3 tools/check_roadmap_markers.py --roadmap <path>

Exit status is 0 when every marker agrees with git, 1 when any does not, and
2 when the environment cannot answer the question (no git, no base ref).
"""
from __future__ import annotations

import argparse
import difflib
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


def self_branch_findings(markers: list[dict], branch: str) -> list[str]:
    """A marker that says work is IN FLIGHT on the branch this PR is FROM.

    `branch_findings` catches a marker naming a branch that is already gone.
    This catches the one that CREATES one. A PR whose roadmap text says
    "FIXING on `fix/277-rust-vec-char`" is green while it is open, because
    that branch exists. The merge deletes the branch, so the marker the PR
    just introduced is stale the instant it lands: main's `lint` goes red,
    and because `lint` runs on branch tips, every open PR whose merge-ref
    carries the new text goes red with it until each is retriggered by hand.
    One merge, N stalled PRs; four occurrences on 2026-09-02 alone.

    NARROW ON PURPOSE, and the narrowing is the whole check. A marker naming
    its own branch in the PAST tense is legitimate and common - two open PRs
    add ``LANDED SO FAR (`fix/391-selfhost-parity`)`` and are entirely
    correct - because a sentence about what a branch DID stays true after the
    branch is deleted. Only a sentence about what a branch IS DOING goes
    stale. So the input here is `collect_markers`'s output and nothing else:
    the in-flight phrasing is exactly MARKER_RE's (`FIXING`, `being fixed`,
    `in flight`, `in-progress`, `underway`, `WIP`) plus an item's leading
    in-progress glyph, and the branch is exactly the one BRANCH_RE and WINDOW
    attributed to it.

    Reusing the gate's own matcher is not a convenience, it is the correctness
    argument. THE INVARIANT: this fires exactly when `branch_findings` will
    fire on main once the merge deletes the branch (rule 2, "names a branch
    that no longer exists on origin"). Same markers, same branch attribution,
    one step earlier. A second matcher written to the same description would
    drift from this one, and then the PR-time check and the main-time check
    would disagree about which sentences are markers at all - the check meant
    to stop a red would start causing one, or miss the one it exists for. The
    gate's own regex is the oracle.

    Scope: the whole roadmap as the PR branch has it, not the diff. A marker
    naming this branch in flight goes stale on merge whether this PR wrote the
    line or inherited it, so restricting to added lines would only lose true
    positives. And only in the PR context: on main a marker naming a live
    branch is legitimate, and is already `branch_findings`' business.
    """
    findings: list[str] = []
    for mk in _dedupe(markers):
        if mk["branch"] != branch:
            continue
        findings.append(
            f"L{mk['line']}: marker says {mk['phrase']!r} on branch "
            f"{branch!r}, which is THIS PR's own head branch.\n"
            f"    quote: ...{mk['quote']}...\n"
            f"    Merging deletes this branch, so this marker is stale the "
            f"moment it lands: it reddens main's lint and every open PR whose "
            f"merge-ref carries it. Cite the PR instead (`PR #123`), or once "
            f"it has landed, the merge sha. A marker must not name a ref that "
            f"is about to stop existing.\n"
            f"    Naming this branch in the PAST tense is fine and is not what "
            f"this reports: 'landed via {branch}' stays true after the branch "
            f"is gone."
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


# ---------------------------------------------------------------------------
# Shared substrate for gates A-D: findings, and the claims attached to them.
#
# The roadmap's audit items are not flat. A top-level item carries a HEADER
# (its status glyph and its opening prose) and then a run of per-finding
# blocks, each of which carries its own status. The four gates below all ask
# questions of the form "does this block's own head agree with this block's
# own body", so they all need the same split.
# ---------------------------------------------------------------------------

# The two ways a finding block opens in this file: "**F3 HIGH, ..." in the
# audit items (421, 422, 425, 427, 428, 433, 436) and "(c) **..." in the
# codegen audits (432, 434, 437). Both are the FIRST thing on the block.
UNIT_LABEL_RE = re.compile(r"\*\*(F\d+[a-z]?)\b|(?<![A-Za-z0-9])\(([a-z])\)\s+\*\*")

# A bold span, which is where this file always writes a status.
BOLD_RE = re.compile(r"\*\*(.{1,400}?)\*\*", re.DOTALL)

# How far into an item's header a bold span still reads as the item's own
# status claim rather than as emphasis inside its argument. Item 422's
# "ALL SEVEN FINDINGS FIXED..." span opens around column 500.
HEAD_SCAN = 1500

# Words that assert the thing is finished.
CLOSURE_RE = re.compile(
    r"""(
          ✅                      # the done glyph
        | \bFIXED\b | \bLANDED\b | \bCLOSED\b | \bRESOLVED\b
        | \bSHIPPED\b | \bDONE\b | \bCOMPLETE[D]?\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Words that, INSIDE the same claim, take the closure back. A claim that
# qualifies itself is not the failure this gate is about: the reader is being
# told the truth in the same breath. Item 422's header today reads "ALL SEVEN
# FINDINGS FIXED ON THE PY TIER, AND F1-F4 ARE STILL OPEN ON THE TS TIER",
# which is an honest sentence and must not be a finding.
QUALIFIER_RE = re.compile(
    r"""(
          ◑ | ❌ | \U0001F6A7 | ➖   # partial, open, in flight, dropped
        | \bHALF\b | \bPARTIAL(?:LY)?\b | \bPARTLY\b
        | \bSTILL\b | \bREMAINS?\b | \bEXCEPT\b | \bNOT\b | \bNO\b
        | \bON\s+THE\s+\w+\s+TIER\b | \b\w+\s+TIER\s+ONLY\b
        | \bRESIDUAL\b | \bOPEN\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Phrases that say the work is NOT finished. This list is deliberately NARROW,
# and the narrowing was measured rather than guessed. The first draft accepted
# every natural-language way of saying "not done" ("untouched", "residual",
# "still carries", "are not X") and produced 40 findings on the file, of which
# 3 were real: the roadmap uses those words constantly for healthy things ("the
# other N-1 tenants provably untouched", "the residual ask is filed as item
# 78", "keyword statement positions untouched" describing a deliberate scope).
# A gate at that signal-to-noise gets ignored, which is worse than no gate.
#
# What survives is the roadmap's own STATUS vocabulary: the handful of
# spellings this file uses to mean "this specific work is not closed". Item
# 422's body said "STILL OPEN ON THE TS TIER", which is in here. General prose
# about scope is not, and the cost of that is written into the failure text:
# this gate finds contradicted STATUS, not contradicted meaning.
CONTRADICTION_RE = re.compile(
    r"""(
          ❌
        | \bstill\s+open\b | \bremains?\s+open\b | \bstays\s+open\b
        | \bstill\s+(?:exposed|vulnerable|unfixed|broken|red|failing)\b
        # "NOT fixed BY refusing", "NOT closed BY this" say how or by what,
        # not that this finding is open. Both spellings are in the file today
        # (421 F3, 422 F7) and both were false positives before this bound.
        | \bnot\s+(?:yet\s+)?fixed\b(?!\s+by\b) | \bnever\s+fixed\b | \bunfixed\b
        | \bnot\s+closed\b(?!\s+by\b) | \bleft\s+open\b
        | \bhalf[\s-](?:open|closed|done|fixed)\b
        | \bdid\s+not\s+land\b | \bnot\s+landed\b
        | \b(?:untouched|open|exposed|unfixed|not\s+fixed)\s+on\s+the\s+
          (?:py|python|ts|typescript|go|golang|java|jvm|rust|rs|wasm)\b
        | \bon\s+the\s+\w+\s+tier\s+only\b | \bonly\s+on\s+the\s+\w+\s+tier\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# A non-closure STATUS in a finding block's own head. Compared against the
# ITEM header's claim, glyph against glyph, which is the exact shape item 422
# failed at: the header said seven of seven while four of the seven per-finding
# heads underneath it said otherwise.
# CASE-SENSITIVE on purpose. This file writes statuses in capitals, and the
# lower-case words are topic names: item 104's block (c) is headed "(c)
# **partial import**", which is what the block is ABOUT, not a status. Matching
# case-insensitively made that a finding against a correctly closed item.
OPEN_HEAD_RE = re.compile(
    r"""(
          ❌ | ◑ | \U0001F6A7
        | \bSTILL\s+OPEN\b | \bNOT\s+(?:FIXED|DONE|LANDED|CLOSED)\b
        | \bUNFIXED\b | \bHALF\s+(?:OPEN|CLOSED)\b
        | \bPARTIAL(?:LY)?\b | \bOPEN\b
    )""",
    re.VERBOSE,
)

# A closed finding legitimately restates what it USED to say. In this file
# that restatement always runs from one of these markers to the end of the
# block, so the scan is cut there. The cost is a real contradiction written
# AFTER the historical restatement, which this gate will miss and says so.
RETROSPECTIVE_RE = re.compile(
    r"""(
          \boriginal\s+finding\b | \boriginally\b | \bthe\s+original\b
        | \bas\s+found\b | \bwhen\s+found\b | \bbefore\s+the\s+fix\b
        | \bpre-fix\b | \bthe\s+finding\s+was\b
        | \bwhat\s+was\s+wrong\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# "was still open" is history, not a contradiction. Checked in the 40
# characters before a hit.
PAST_TENSE_RE = re.compile(r"\b(?:was|were|had|used\s+to|previously|formerly)\b\W*$",
                           re.IGNORECASE)

# (B). A phrase that hands a finding to somebody else's work.
DELEGATION_RE = re.compile(
    r"""(
          folded\s+into | refiled\s+(?:under|onto|as|to|into)
        # (?<![a-z]) rather than \b: "a real defect UNCOVERED BY item 298"
        # says the target found this, which is the opposite of delegating to
        # it, and \b does not separate "un" from "covered".
        | tracked\s+(?:by|under|in|as) | (?<![a-z])covered\s+by
        | subsumed\s+by
        | rolled\s+into | handled\s+(?:by|under) | absorbed\s+(?:into|by)
        | deferred\s+to | carried\s+(?:into|to) | owned\s+by
        | see\s+item | filed\s+(?:under|as|onto)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# How far after a delegation phrase its target may sit, and where the window
# stops early. Both bounds are measured. At 160 characters with no early stop,
# "async/host-Map/spawn deferred to a slice 5) **Path B ... (through modern
# component, item 225)" reached 136 characters forward and reported a
# delegation to item 225 that the sentence never made. A delegation names its
# target immediately or it is not a delegation.
DELEGATION_WINDOW = 60
DELEGATION_STOP_RE = re.compile(r"\*\*|\)|\.\s+[A-Z(]|;\s")

# A delegation phrase the roadmap is QUOTING in order to disown it. Item 425
# F3 and item 427 F5 both read "AND THE MARKER `folded into the item-416c fix`
# WAS WRONG": the phrase is inside backticks because it is the marker being
# corrected, and the sentence around it says so. Reporting that as a live
# delegation punishes the correction this very check asked for, and it is the
# one shape where the delegation is provably not being made. The bound is
# deliberately two-part -- the phrase must sit INSIDE a backticked span AND be
# disowned within DISAVOWAL_WINDOW characters of that span's closing backtick.
# An unquoted "folded into the item-416c fix" is still a delegation, and so is
# a quoted one nobody disowns.
DISAVOWAL_RE = re.compile(
    r"""\A\W{0,4}(?:
          (?:was|were|is|are)\s+(?:wrong|incorrect|stale|mistaken)
        | (?:was|were|is|are)\s+(?:never\s+|not\s+)true
        | never\s+held
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)
DISAVOWAL_WINDOW = 40

# "item-416c", "item 416", "item 427 F3", "items 245/246". The word "item" is
# required: bare three-digit numbers in this file are line numbers and byte
# counts far more often than they are item references.
DELEGATION_TARGET_RE = re.compile(
    r"\bitems?[\s‑_-]*(\d{2,4})\s*(?:\(([a-z])\)|([a-z])(?![A-Za-z0-9]))?"
    r"(?:[\s'`]*(?:F(\d+)))?",
    re.IGNORECASE,
)

# (C). Evidence that somebody could follow this finding to a live owner.
OWNER_RE = re.compile(
    r"""(
          \bowner\s*[:=] | \bowned\s+by\s+[@A-Z] | \bassigned\s+to\b
        | @[A-Za-z][A-Za-z0-9-]{2,}
    )""",
    re.VERBOSE,
)

# A sha cited as the revision something was CHECKED on. This records that the
# finding is still live; it names nobody. See the --check-orphan docstring.
VERIFICATION_SHA_RE = re.compile(
    r"""(?:
          re-?verified | verified | re-?checked | checked
        | re-?executed | reproduced | re-?reproduced | observed
        | measured | re-?measured | as\s+of | audited | grepped
    )
    [^`]{0,60}`([0-9a-f]{7,40})`""",
    re.IGNORECASE | re.VERBOSE,
)

# (D). A path into one backend tier.
TIER_PATH_RE = re.compile(r"\bbackends/([a-z0-9_]+)/")

# Spellings a tier goes by in prose, so "the ts tier" counts as naming
# typescript. Keys are directory names under backends/.
TIER_ALIASES = {
    "python": ("python", "py", "cpython"),
    "typescript": ("typescript", "ts", "javascript", "js", "node"),
    "go": ("go", "golang"),
    "java": ("java", "jvm"),
    "rust": ("rust", "rs", "cargo"),
    "wasm": ("wasm", "webassembly", "wat"),
}

# The subjects this project states as language-wide guarantees rather than as
# properties of one emitter. A finding about ONE of these that cites exactly
# one tier is the shape that burned item 422, item 421 F6 and the self-host
# taint stage on 2026-09-02. Performance is deliberately absent: a codegen
# perf finding is about one emitter's output by construction, which is why
# items 432-437 do not fire here.
TIER_SUBJECTS = (
    "confinement", "jail", "sandbox", "capability", "attenuation",
    "secret", "redact", "redaction", "taint", "approval", "policy",
    "provenance", "authority", "audit surface", "traversal", "symlink",
    "privilege", "witnessed", "attestation",
)

# The word alone is not enough, and the counter-examples are in the file.
# "escape", "leak" and "guarantee" were in the list above until they were
# measured: "escape" matched item 106's `\u{...}` STRING escape and items 135
# and 269's escaping rules, "leak" matched item 150's wasm memory leak, and
# "guarantee" matched anything. They are gone. What replaced them for the words
# that stayed is this threshold: a finding that is really about a language-wide
# guarantee uses more than one of its words, and a finding that says
# "capability" once in passing does not. Measured on the file: the threshold
# takes the check from 18 findings to 6, and every one of the 12 it drops was
# a word used in another sense.
MIN_TIER_SUBJECTS = 2


def _bold_spans(text: str, limit: int | None = None) -> list[tuple[int, str]]:
    """(offset, inner text) for every bold span, with a short lookbehind.

    The lookbehind matters: this file writes item 422's status as
    "✅ **ALL SEVEN FINDINGS FIXED...**", with the glyph OUTSIDE the span.
    """
    out = []
    for m in BOLD_RE.finditer(text):
        if limit is not None and m.start() > limit:
            break
        out.append((m.start(), text[max(0, m.start() - 6): m.end()]))
    return out


def units(item: dict) -> list[dict]:
    """Split an item body into its header and its per-finding blocks.

    Returns dicts with: label ("header", "F3", "(c)"), text, and line, the
    absolute line number in the roadmap where the block starts.
    """
    body = item["body"]
    marks = [(m.start(), m.group(1) or f"({m.group(2)})")
             for m in UNIT_LABEL_RE.finditer(body)]
    bounds = [(0, "header")] + marks
    out = []
    for idx, (off, label) in enumerate(bounds):
        end = bounds[idx + 1][0] if idx + 1 < len(bounds) else len(body)
        text = body[off:end]
        if not text.strip():
            continue
        out.append({
            "label": label,
            "text": text,
            "line": item["line"] + body[:off].count("\n"),
            "item": item["number"],
        })
    return out


# A finding block's own status is written in the FIRST bold span of the block:
# "**F3 HIGH, ✅ FIXED 2026-09-02**", or "(c) **❌ STILL OPEN, RE-VERIFIED IN
# SOURCE ON `bd0f4d19`.**" where the label sits outside the span. LABEL_SLACK is
# how far in that span may start.
LABEL_SLACK = 12


def unit_head(unit: dict) -> str:
    """The block's own status line, or "" when it writes none."""
    spans = _bold_spans(unit["text"], LABEL_SLACK)
    return re.sub(r"\s+", " ", spans[0][1]).strip() if spans else ""


def closure_claim(unit: dict, item: dict) -> str | None:
    """The unit's own head, when that head claims closure WITHOUT qualifying it.

    A qualified claim ("HALF CLOSED", "FIXED ON THE PY TIER, STILL OPEN ON THE
    TS TIER") is not a finding here: it is the roadmap doing its job. Only a
    flat claim of closure can mislead a reader into skipping the body.
    """
    if unit["label"] == "header":
        # An ITEM-level closure claim has to carry the done glyph, either as
        # the item's leading status or inside/just before the bold span. Item
        # 434's header is "PARTIALLY LANDED ...: (d) and (g) are NOT", and it
        # contains a bold panel title "**WHAT LANDED**" further down. Without
        # the glyph requirement that panel title read as an unqualified
        # closure claim and the item was reported against its own honest
        # header.
        # The item's leading status, but only when that status IS the done
        # glyph. Reading the opening prose of a glyph-less item as a claim
        # made "416. **MCP ARGUMENT LEAK AUDIT.** Landed 2026-09-01." a
        # closure claim on the strength of the word "Landed" in a sentence
        # about something else.
        heads = ([re.sub(r"\s+", " ", unit["text"])[:120]]
                 if item["status"] == "done" else [])
        heads += [re.sub(r"\s+", " ", span) for _, span in
                  _bold_spans(unit["text"], HEAD_SCAN)
                  if "\u2705" in span]
    else:
        # Same glyph requirement as the item header, and for the same reason.
        # Item 416's block (c) is headed "**HYPOTHESES, unexecuted, same class
        # as the fixed secret-externalization leaks.**", where "fixed" is an
        # adjective about somebody else's work. Every actually-closed block in
        # this file carries ✅, so requiring it costs nothing and removes that
        # entire class of misreading. The cost is a false negative on a block
        # closed in words with no glyph, which the failure text says out loud.
        head = unit_head(unit)
        heads = [head] if head and "\u2705" in head else []
    for head in heads:
        if CLOSURE_RE.search(head) and not QUALIFIER_RE.search(head):
            return head.strip()
    return None


def _scan_region(text: str) -> str:
    """The part of a block a closure claim is actually answerable for.

    Cut at the first retrospective marker: everything from "Original finding:"
    onwards is a deliberate past-tense quotation of what the finding used to
    say, and reading it as a live contradiction would make this gate fire on
    almost every correctly closed finding in the file.
    """
    m = RETROSPECTIVE_RE.search(text)
    return text[: m.start()] if m else text


def _past_tense_governed(region: str, at: int) -> bool:
    return bool(PAST_TENSE_RE.search(region[max(0, at - 40): at]))


_ADVICE_A = (
    "    Either the claim is wrong, or the claim is right and the body is "
    "stale. This gate cannot tell which and will not guess. Decide by reading "
    "the code, then say in the HEAD what is closed and what is not, the way a "
    "qualified claim does: item 422's header now reads 'ALL SEVEN FINDINGS "
    "FIXED ON THE PY TIER, AND F1-F4 ARE STILL OPEN ON THE TS TIER', which "
    "passes this gate because it is true. Do not delete the body sentence to "
    "silence this."
)


def contradiction_findings(text: str) -> list[str]:
    """(A) A closure claim whose own scope of text says the work is not done.

    Two shapes, because the roadmap makes the claim at two levels and each
    fails differently.

    HEAD AGAINST HEADS. An item header that claims closure without qualifying
    it, over finding blocks whose own heads say open. This is what happened to
    item 422 on 2026-09-02: the header read "ALL SEVEN FINDINGS FIXED" while
    F1-F4's own markers said they were untouched on the typescript tier, and a
    disclosed CRITICAL sat unowned because the header was believed. Comparing
    a status head against status heads is nearly free of false positives: both
    sides are the roadmap's own glyph vocabulary, not prose.

    HEAD AGAINST BODY. A finding whose own head claims closure and whose own
    body then uses one of the status spellings in CONTRADICTION_RE. Here the
    false-positive risk is real, because a correctly closed finding QUOTES its
    original text in past tense. Two bounds: the scan stops at the first
    retrospective marker (_scan_region), and a hit governed by a past-tense
    auxiliary is dropped (_past_tense_governed).
    """
    findings: list[str] = []
    for it in items(text):
        if it["section"] in UNTRACKED_SECTIONS:
            continue
        blocks = units(it)
        header = blocks[0]
        header_claim = closure_claim(header, it)
        if header_claim is not None:
            open_heads = []
            for unit in blocks[1:]:
                head = unit_head(unit)
                if not head:
                    continue
                if closure_claim(unit, it) is None and OPEN_HEAD_RE.search(head):
                    open_heads.append((unit["label"], head))
            if open_heads:
                lines = [
                    f"L{header['line']}: item {it['number']}'s header claims "
                    f"closure while {len(open_heads)} of its own finding "
                    f"block(s) are marked open.",
                    f"    header: {header_claim[:200]}",
                ]
                for label, head in open_heads[:6]:
                    lines.append(f"    {label}: {head[:150]}")
                if len(open_heads) > 6:
                    lines.append(f"    ({len(open_heads) - 6} more)")
                lines.append(_ADVICE_A)
                findings.append("\n".join(lines))
        for unit in blocks[1:]:
            claim = closure_claim(unit, it)
            if claim is None:
                continue
            region = _scan_region(unit["text"])
            hits = []
            for m in CONTRADICTION_RE.finditer(region):
                if _past_tense_governed(region, m.start()):
                    continue
                quote = re.sub(r"\s+", " ", region[max(0, m.start() - 90):
                                                   m.end() + 90])
                hits.append((m.group(0).strip(), quote))
            if not hits:
                continue
            lines = [f"L{unit['line']}: item {it['number']} {unit['label']} "
                     f"claims closure, and its own body contradicts it.",
                     f"    claim: {claim[:180]}"]
            for phrase, quote in hits[:4]:
                lines.append(f"    contradicted by {phrase!r}: ...{quote}...")
            if len(hits) > 4:
                lines.append(f"    ({len(hits) - 4} more)")
            lines.append(_ADVICE_A)
            findings.append("\n".join(lines))
    return findings


def _is_disowned_quote(text: str, match: re.Match,
                       quoted: list[tuple[int, int]]) -> bool:
    """True when this delegation phrase is a QUOTED marker the prose disowns.

    See DISAVOWAL_RE. Both halves are required: inside a backticked span, and
    disowned right after that span closes.
    """
    for start, end in quoted:
        if start < match.start() and match.end() <= end:
            return DISAVOWAL_RE.match(text[end: end + DISAVOWAL_WINDOW]) is not None
    return False


def _delegation_targets(text: str) -> list[dict]:
    """Every delegation phrase in a block, with the target it names."""
    out = []
    quoted = [(m.start(), m.end()) for m in re.finditer(r"`[^`\n]+`", text)]
    for m in DELEGATION_RE.finditer(text):
        if _is_disowned_quote(text, m, quoted):
            continue
        window = text[m.end(): m.end() + DELEGATION_WINDOW]
        stop = DELEGATION_STOP_RE.search(window)
        if stop is not None:
            window = window[: stop.start()]
        t = DELEGATION_TARGET_RE.search(window)
        if t is None:
            continue
        out.append({
            "phrase": re.sub(r"\s+", " ", m.group(1)),
            "item": t.group(1),
            "part": t.group(2) or t.group(3),
            "finding": f"F{t.group(4)}" if t.group(4) else None,
            "quote": re.sub(r"\s+", " ",
                            text[max(0, m.start() - 60): m.end() + 120]),
        })
    return out


def _target_block(target: dict, part: str | None, finding: str | None) -> dict | None:
    """The block inside a target item that a delegation actually points at.

    "the item-416c fix" points at item 416's block (c), not at item 416.
    """
    want = finding or (f"({part})" if part else None)
    if want is None:
        return units(target)[0]
    for tu in units(target):
        if tu["label"] == want:
            return tu
    return None


def delegation_findings(text: str, dirs: set[str], namespaces: set[str],
                        heads: set[str]) -> list[str]:
    """(B) A closure that hands the finding to a target nobody is working.

    Item 425 F3 read "folded into the item-416c fix" and item 427 F5 said the
    same. The residual those two findings were really about was refiled onto
    owners that were about a different defect, so it looked tracked, was
    closed nowhere, and had no live owner.

    Three ways a delegation dangles, all decidable from the file alone:
      1. the target is not a top-level item here at all;
      2. the target is itself marked CLOSED, which makes the delegating
         finding an orphan by construction;
      3. the target is open and is itself unowned, which moves the finding
         from one orphan to another rather than tracking it. Item 416's
         block (c), where 425 F3 and 427 F5 both point, is headed
         "HYPOTHESES, unexecuted" and names no branch, issue, sha or owner.

    What this CANNOT do, and the failure text says so: decide whether the
    target's fix actually covered the delegating finding. A live, owned target
    passes here and can still be about a different defect entirely, which is
    what happened when 425 F3's residual was refiled onto 427 F2/F3/F4.
    """
    findings: list[str] = []
    seen: set[tuple[int, str]] = set()
    all_items = items(text)
    by_number: dict[str, list[dict]] = {}
    for it in all_items:
        by_number.setdefault(it["number"], []).append(it)
    for it in all_items:
        if it["section"] in UNTRACKED_SECTIONS:
            continue
        for unit in units(it):
            claim = closure_claim(unit, it)
            region = _scan_region(unit["text"])
            for dele in _delegation_targets(region):
                local = CLOSURE_RE.search(dele["quote"]) is not None
                if claim is None and not local:
                    continue
                # One item repeating the same delegation in two sentences is
                # one dangling delegation, not two.
                key = (unit["line"], dele["item"] + (dele["part"] or "")
                       + (dele["finding"] or ""))
                if key in seen:
                    continue
                seen.add(key)
                targets = by_number.get(dele["item"])
                name = "item " + dele["item"] + (dele["part"] or "")
                if dele["finding"]:
                    name += " " + dele["finding"]
                head = (f"L{unit['line']}: item {it['number']} "
                        f"{unit['label']} delegates its closure to {name}")
                if not targets:
                    findings.append(
                        f"{head}, which is not a top-level item in this "
                        f"roadmap.\n    quote: ...{dele['quote'][:220]}...\n"
                        f"    The finding points at nothing, so nobody owns it. "
                        f"Name a target that exists, or take the finding back "
                        f"and give it a branch, an issue or an owner.")
                    continue
                target = targets[-1]
                header_closed = (target["status"] == "done"
                                 or closure_claim(units(target)[0], target)
                                 is not None)
                block = _target_block(target, dele["part"], dele["finding"])
                if block is None:
                    # A named part that is not spelled out as its own block.
                    # Only a MISSING part in an item that labels its other
                    # parts is a real dangling reference; an item that writes
                    # its parts as running prose has no block to find, so the
                    # delegation falls back to the item as a whole. If the
                    # item is closed, the part is beside the point either way.
                    labelled = {u["label"] for u in units(target)} - {"header"}
                    if labelled and not header_closed:
                        findings.append(
                            f"{head}, but item {dele['item']} has no "
                            f"{dele['finding'] or dele['part']} block.\n"
                            f"    quote: ...{dele['quote'][:220]}...\n"
                            f"    The delegation names a part that does not "
                            f"exist, so nothing is tracking this.")
                        continue
                    block = units(target)[0]
                closed = (closure_claim(block, target) is not None
                          or (block["label"] == "header" and header_closed))
                if not closed:
                    # The target is live. That is only tracking if the target
                    # itself is owned. Item 425 F3 read "folded into the
                    # item-416c fix" and item 416's block (c) is headed
                    # "HYPOTHESES, unexecuted" with no branch, no issue, no
                    # owner and no sha: the delegation moved the finding from
                    # one orphan to another, which is why it was closed
                    # nowhere and nobody was working it.
                    own, _ = _tracking_evidence(_scan_region(block["text"]),
                                                dirs, namespaces, heads)
                    if own:
                        continue
                    findings.append(
                        f"{head}, and {name} is itself OPEN with no branch, no "
                        f"issue, no sha and no owner.\n"
                        f"    quote: ...{dele['quote'][:220]}...\n"
                        f"    target: L{block['line']} {unit_head(block)[:110] or re.sub(chr(92) + 's+', ' ', target['text'])[:110]}\n"
                        f"    Delegating to an unowned target does not track "
                        f"anything, it moves the finding from one orphan to "
                        f"another. Either give the TARGET an owner, or take "
                        f"this finding back and give it one.")
                    continue
                findings.append(
                    f"{head}, and {name} is itself marked CLOSED.\n"
                    f"    quote: ...{dele['quote'][:220]}...\n"
                    f"    target: L{target['line']} {re.sub(chr(92) + 's+', ' ', target['text'])[:110]}\n"
                    f"    A finding delegating to a closed target is an orphan "
                    f"by construction: it is closed nowhere and has no live "
                    f"owner. This gate CANNOT tell whether the target's fix "
                    f"actually covered this finding. Go read the target's diff. "
                    f"If it covered it, say so here in the past tense with the "
                    f"sha. If it did not, this finding is OPEN and needs a "
                    f"branch, an issue or an owner of its own.")
    return findings


def _tracking_evidence(text: str, dirs: set[str], namespaces: set[str],
                       heads: set[str]) -> tuple[list[str], list[str]]:
    """What a reader could follow from this block: (ownership, verification-only).

    Ownership evidence is a branch, a GitHub issue, an explicit owner, or a
    sha cited as a fix. A sha cited as the revision the finding was RE-VERIFIED
    on is verification-only: it is evidence the finding is still live, which is
    the opposite of evidence that somebody is closing it.
    """
    own: list[str] = []
    verify = {m.group(1) for m in VERIFICATION_SHA_RE.finditer(text)}
    for b in BRANCH_RE.finditer(text):
        token = _clean_branch(b.group(1))
        if _looks_like_branch(token, dirs, namespaces, heads):
            own.append(f"branch {token}")
    m = ISSUE_RE.search(text)
    if m:
        own.append(f"issue {m.group(1)}")
    m = OWNER_RE.search(text)
    if m:
        own.append(f"owner {m.group(1).strip()}")
    for s in SHA_RE.finditer(text):
        if s.group(1) not in verify:
            own.append(f"sha {s.group(1)}")
    return own, sorted(verify)


def orphan_findings(text: str, dirs: set[str], namespaces: set[str],
                    heads: set[str]) -> list[str]:
    """(C) An open or delegated finding that names nothing to follow.

    Distinct from --require-issue on purpose. That flag is per-ITEM and asks
    for a GitHub issue specifically, and it cannot be turned on until the issue
    migration lands. This one is per-FINDING, accepts any of four kinds of
    ownership evidence, and is usable today.
    """
    findings: list[str] = []
    for it in items(text):
        if it["section"] in UNTRACKED_SECTIONS or it["status"] == "dropped":
            continue
        for unit in units(it):
            # F-labelled blocks only. The "(a)/(b)/(c)" blocks are sub-parts of
            # ONE piece of work, tracked by their item; the F blocks are audit
            # findings the roadmap gives independent statuses to, and an audit
            # finding is the thing that goes unowned. Running this over the
            # lettered blocks too produced 68 findings on the file against 22
            # for the F blocks alone, and the extra 46 were all "item 74 (b)
            # cites no branch", which is true and means nothing.
            if not re.fullmatch(r"F\d+[a-z]?", unit["label"]):
                continue
            claim = closure_claim(unit, it)
            delegated = bool(_delegation_targets(_scan_region(unit["text"])))
            if claim is not None and not delegated:
                continue
            region = _scan_region(unit["text"])
            own, verify = _tracking_evidence(region, dirs, namespaces, heads)
            # A PARTIALLY closed finding cites the sha that closed the half it
            # closed. That sha owns the closed half and says nothing about the
            # open one, which is the whole shape of item 425 F3: `556e5d4f`
            # landed the declared-Secret redaction and the undeclared-param
            # residual it left behind had no owner at all. A branch, an issue
            # or a named owner still counts, because those name a person or a
            # piece of work in flight rather than a commit already made.
            partial = QUALIFIER_RE.search(unit_head(unit)) is not None
            if partial:
                own = [e for e in own if not e.startswith("sha ")]
            if own:
                continue
            head = unit_head(unit)[:140]
            note = (f"\n    cites `{verify[0]}` only as the revision it was "
                    f"re-verified on, which records that it is still live, not "
                    f"who is closing it." if verify else "")
            findings.append(
                f"L{unit['line']}: item {it['number']} {unit['label']} is "
                f"{'delegated' if delegated else 'open'} and names no branch, "
                f"no issue, no fix sha and no owner.{note}\n"
                f"    head: {head}\n"
                f"    Nobody is working this and nothing will notice. Give it "
                f"one of the four, or state in the finding that it is parked "
                f"and why.")
    return findings


# (E). How alike two headers under one number have to read before they are the
# same item written twice rather than two items that collided. Measured on the
# file: item 106's residue scores 1.0 against the entry that replaced it (it is
# a truncated copy of the same line), and the four real collisions in "Open, in
# rough priority order" score 0.06 to 0.28 against their namesakes. Nothing on
# the file lands between 0.28 and 1.0, so the threshold is not load-bearing;
# it only decides which SENTENCE the finding prints.
NEAR_IDENTICAL = 0.85

# The shortest prefix worth comparing. Item 106's residue is 60 characters, so
# a bound above that would classify the exact case this check exists for as a
# collision.
MIN_COMPARE = 32


def _header_text(it: dict) -> str:
    """An item's header line with its status glyph and whitespace normalised
    away, so two blocks differing ONLY in their marker compare as equal."""
    text = re.sub(r"\s+", " ", it["text"]).strip()
    for glyph in DONE_GLYPHS + PARTIAL_GLYPHS + INFLIGHT_GLYPHS + DROPPED_GLYPHS:
        text = text.replace(glyph, " ")
    return re.sub(r"\s+", " ", text).strip()


def _has_no_body(it: dict) -> bool:
    """The block is a header line and nothing else."""
    return not any(line.strip() for line in it["body"].splitlines()[1:])


def _near_identical(a: str, b: str) -> bool:
    """Do these two headers say the same thing, as far as the shorter one runs?

    Compared over the SHORTER header's length on purpose. Merge residue is a
    TRUNCATED copy: item 106's stale line stops mid-sentence at "collides
    with", and comparing full strings would score it 0.55 against the 3.6 kB
    entry it duplicates and read as a collision.
    """
    n = min(len(a), len(b))
    if n < MIN_COMPARE:
        return False
    return difflib.SequenceMatcher(None, a[:n], b[:n]).ratio() >= NEAR_IDENTICAL


def duplicate_header_findings(text: str) -> list[str]:
    """(E) One item number carrying more than one block inside one section.

    Item numbers RESTART per section by design -- item 1 exists five times in
    this file, once per section -- so the question is only ever asked WITHIN a
    section. Cross-section reuse is the documented convention and is never a
    finding here; that carve-out is what keeps a legitimate second life of a
    number out of the report.

    Inside one section it is always a defect, and it comes in three shapes,
    all three measured on the file on 2026-09-02:

      1. MERGE RESIDUE. Item 106 carried a truncated copy of its own header,
         one line above the entry that replaced it and identical to it apart
         from the marker: `38c84207` flipped the glyph from in-progress to
         done and the merge `bd0f4d19` resolved the hunk by keeping BOTH
         sides. The stale line had no body of its own and was the only reason
         a landed item still read as in progress. Nothing else in the repo
         could see it, because the staleness gate only validates markers that
         NAME A BRANCH and this one named nothing.

      2. CONFLICTING STATUS. The same number carrying done in one block and
         in-progress in another. A reader who meets the in-progress block
         first re-does work that landed, which is the cost this whole file
         exists to avoid.

      3. COLLISION. Two DIFFERENT items filed under one number, which makes
         every "item 100" reference in the tree ambiguous -- and there are
         nineteen of those in src/, tests/ and docs/ for items 100-103 alone.

    Like every other check here it does not rewrite anything, and it cannot
    decide WHICH block should keep the number. That is a reading job.
    """
    findings: list[str] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for it in items(text):
        groups.setdefault((it["section"], it["number"]), []).append(it)
    for (section, number), blocks in groups.items():
        if len(blocks) < 2:
            continue
        heads = [_header_text(b) for b in blocks]
        residue = [b for b, h in zip(blocks, heads)
                   if _has_no_body(b)
                   and any(other is not b and _near_identical(h, oh)
                           for other, oh in zip(blocks, heads))]
        statuses = {b["status"] for b in blocks}
        alike = all(_near_identical(heads[0], h) for h in heads[1:])
        where = ", ".join(f"L{b['line']} ({b['status']})" for b in blocks)
        if residue:
            shape = (
                f"one of them is MERGE RESIDUE: L{residue[0]['line']} repeats "
                f"the header it sits beside and carries no body of its own.\n"
                f"    Delete the residue line. It is a conflict resolution "
                f"that kept both sides, and while it stands it is the item's "
                f"status.")
        elif len(statuses) > 1:
            shape = (
                f"they carry CONFLICTING statuses ({'/'.join(sorted(statuses))}"
                f"), so the item reads open and closed at once.\n"
                f"    Decide which block owns the number, then renumber or "
                f"close the other. Do not leave both.")
        elif alike:
            shape = (
                "they are the SAME item written twice under one number.\n"
                "    Fold them into one block, keeping the evidence from "
                "both.")
        else:
            shape = (
                "they are DIFFERENT items sharing one number, so every "
                "reference to it is ambiguous.\n"
                "    Renumber one of them and fix the references that meant "
                "it.")
        findings.append(
            f"L{blocks[0]['line']}: item {number} has {len(blocks)} blocks in "
            f"section {section!r} -- {where}.\n"
            f"    {shape}\n"
            f"    Item numbers restart per SECTION in this file, so this is "
            f"only ever asked inside one section; the same number in another "
            f"section is the documented convention and is not reported.")
    return findings


# The finding label at the head of a block, with its sub-part kept ATTACHED so
# a genuine sub-finding does not read as a duplicate of its parent. `units()`
# splits on UNIT_LABEL_RE, whose capture stops at `F6` and so labels both the
# `F6` finding and its `F6(b)` sub-finding "F6" (item 421 carries exactly that
# pair). Re-reading the label off the block's own opening text, this time
# keeping the `(b)`, is what tells the two apart: item 421's F6 and F6(b) are
# DISTINCT blocks, while item 428's two `F5` blocks are the same block twice.
FINDING_LABEL_RE = re.compile(r"\A\s*\*\*(F\d+(?:\([0-9a-z]+\))?[a-z]?)")


def duplicate_finding_blocks(text: str) -> list[str]:
    """(E, one level down) One item whose body carries the same F-block twice.

    `duplicate_header_findings` asks its question per item NUMBER, so it sees a
    number that opens two entries (item 106's merge residue) and is blind to
    the identical shape one level down: a single item whose body carries the
    same F-labelled finding block twice. It is the same cause, a merge that
    resolved a roadmap hunk by keeping BOTH sides, one level below the item
    header, and prose still has no syntax that makes it a build error.

    Two instances on main, both from 2026-09-02. Item 428's F5-F13 are written
    twice, the reconciliation pass beside `fix/428-attestation-tail`'s own
    updates; item 433's F1-F10 are written twice, the measured verdicts beside
    the original audit text. Both read consistent today, but a reader still
    meets every finding twice and the FIRST copy is the one believed, which is
    the cost item 106 paid one level up: its stale F7/F8/F10/F11/F13 copies
    said STILL OPEN for findings that had already been fixed and merged.

    Decidable from the file alone: within one item, an F label that opens more
    than one block is a duplicate. A lettered SUB-finding is not, and telling
    the two apart is the whole subtlety here (see FINDING_LABEL_RE): item 421's
    F6 sits beside its own F6(b), a distinct block, and must not be reported.

    Like every other check here it cannot decide WHICH copy to keep. That is a
    reading job: fold each label back to one block, keeping the copy that is
    current and the evidence from both.
    """
    findings: list[str] = []
    for it in items(text):
        by_label: dict[str, list[dict]] = {}
        for unit in units(it):
            if unit["label"] == "header":
                continue
            m = FINDING_LABEL_RE.match(unit["text"])
            if m is None:
                continue
            by_label.setdefault(m.group(1), []).append(unit)
        dupes = [(lbl, us) for lbl, us in by_label.items() if len(us) > 1]
        if not dupes:
            continue
        dupes.sort(key=lambda kv: kv[1][0]["line"])
        where = "; ".join(
            f"{lbl} at " + "/".join(f"L{u['line']}" for u in us)
            for lbl, us in dupes)
        findings.append(
            f"L{it['line']}: item {it['number']} carries the same finding block "
            f"more than once: {where}.\n"
            f"    A merge that kept both sides of a roadmap hunk duplicated a "
            f"finding below the item header, where prose has no syntax to catch "
            f"it, and the FIRST copy is the one a reader believes. Fold each "
            f"label back to a single block, keeping the copy that is current "
            f"and the evidence from both. Like the per-number check above, this "
            f"does not decide which copy that is.")
    return findings


def _tiers_named(text: str) -> set[str]:
    """Every tier this block names, by path or by prose alias."""
    named = set(TIER_PATH_RE.findall(text))
    low = text.lower()
    for tier, aliases in TIER_ALIASES.items():
        for alias in aliases:
            # The spellings this file actually uses for "the X tier": "on the
            # ts tier", "the go emitter", "@py bodies", "py parity", "py-only",
            # "classifier py=ts byte-identical". Item 374 says "at py parity"
            # and "shipped @py-only", and it was reported as naming only
            # typescript until these were added.
            if (re.search(rf"\b{alias}\s*[\u2261=]\s*\w+", low)
                    or re.search(rf"@{alias}\b", low)
                    or re.search(rf"\b{alias}[\s-]+(?:tier|backend|emitter|runtime"
                                 rf"|side|parity|only|version|twin|counterpart)\b", low)
                    or re.search(rf"\b(?:tier|backend|emitter)\s+{alias}\b", low)):
                named.add(tier)
                break
    return named


def tier_parity_findings(text: str, backends: set[str]) -> list[str]:
    """(D) A one-tier fix for a guarantee this project states language-wide.

    WHAT THIS CANNOT DO. It cannot decide semantic parity. Nothing textual
    can: whether item 422's confinement fix needs a typescript twin is a
    question about what `revl_fs_ts.ts` does, not about what the roadmap says.

    WHAT IT DOES. It flags exactly one shape, and requires all four of:
      1. the block claims closure, so somebody has stopped looking at it;
      2. it cites `backends/<tier>/` paths for exactly ONE tier;
      3. its subject is one of TIER_SUBJECTS, the guarantees this project
         states for the language rather than for an emitter;
      4. it never names a second tier ANYWHERE in its own text.

    Condition 4 is what keeps this quiet, and it is also the discipline being
    asked for: a fix that really is one tier wide only has to say which tier,
    the way item 422's header now does. Condition 3 is what keeps the codegen
    performance audits (432-437) out: those are about one emitter's output by
    construction, and none of them is about a language-wide guarantee.

    THREE BLIND SPOTS, stated so nobody trusts this further than it goes.
      - A finding that names a second tier merely to say "the ts tier is out
        of scope" is exempt, by design: that IS the discipline being asked
        for, and whether the scope call was right is not a textual question.
      - A guarantee whose subject word is not in TIER_SUBJECTS is invisible.
      - The SELF-HOST tier is not a `backends/<tier>/` directory, so a gap
        between `src/revl/` and `selfhost/` is structurally outside this
        check. That is where the third of the three 2026-09-02 cases lives:
        `Untrusted`, `Trusted` and `Secret` appear zero times in
        `selfhost/lower.rvl` and `selfhost/checker.rvl` while
        `src/revl/taint.py` stamps all three. Item 429 records it in prose
        with a strict xfail, and nothing here would have found it.

    This finds a SHAPE. Parity is still decided by reading.
    """
    findings: list[str] = []
    for it in items(text):
        if it["section"] in UNTRACKED_SECTIONS:
            continue
        for unit in units(it):
            # F-labelled audit findings only. An ordinary work item's header
            # (item 374's ts witnessed catalogs, item 430's CI suite) answers
            # the tier question in its design rather than as an audit residual,
            # and running this over item headers produced four findings that
            # were all of that shape. The cost, stated so it can be fixed: an
            # audit written without F labels is not covered here.
            if not re.fullmatch(r"F\d+[a-z]?", unit["label"]):
                continue
            if closure_claim(unit, it) is None:
                continue
            region = unit["text"]
            paths = set(TIER_PATH_RE.findall(region)) & backends
            if len(paths) != 1:
                continue
            named = _tiers_named(region) & backends
            if len(named) > 1:
                continue
            low = region.lower()
            subjects = sorted({s for s in TIER_SUBJECTS if s in low})
            if len(subjects) < MIN_TIER_SUBJECTS:
                continue
            tier = next(iter(paths))
            others = sorted(backends - {tier})
            where = ("item %s's header" % it["number"] if unit["label"] == "header"
                     else "item %s %s" % (it["number"], unit["label"]))
            findings.append(
                f"L{unit['line']}: {where} claims closure, cites only "
                f"backends/{tier}/, and never names another tier.\n"
                f"    subject(s) this project states language-wide: "
                f"{', '.join(subjects[:6])}\n"
                f"    sibling tiers not mentioned: {', '.join(others)}\n"
                f"    This gate cannot decide parity and is not claiming a "
                f"defect. It is claiming that the finding does not SAY. Check "
                f"the sibling emitters; then either record the parity fix, or "
                f"write in this finding which tiers are out of scope and why. "
                f"Naming the other tier is the whole escape hatch.")
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
    ap.add_argument("--head-branch", default="",
                    help="the PR's own head branch. ALSO fail when an "
                         "in-progress marker names THIS branch: merging "
                         "deletes it, so such a marker is stale on landing "
                         "and reddens main plus every open PR. Empty (the "
                         "default, and what a push to main passes) turns the "
                         "check off. Past-tense mentions of the branch are "
                         "unaffected.")
    ap.add_argument("--require-issue", action="store_true",
                    help="ALSO require every open or partial top-level item to "
                         "cite a GitHub issue. Off until the issue migration "
                         "happens; see CONTRIBUTING.md 'Tracking work'.")
    ap.add_argument("--check-contradiction", action="store_true",
                    help="(A) ALSO fail when a closure claim's own scope of "
                         "text says the work is not done. The item 422 shape.")
    ap.add_argument("--check-delegation", action="store_true",
                    help="(B) ALSO fail when a closure delegates to a target "
                         "that does not exist, is itself closed, or is open "
                         "and unowned. The item 425 F3 shape.")
    ap.add_argument("--check-orphan", action="store_true",
                    help="(C) ALSO fail when an open or delegated FINDING "
                         "names no branch, issue, fix sha or owner. Per-finding, "
                         "and usable before the issue migration.")
    ap.add_argument("--check-tier-parity", action="store_true",
                    help="(D) ALSO fail when a closed finding about a "
                         "language-wide guarantee cites exactly one "
                         "backends/<tier>/ path and never names another tier.")
    ap.add_argument("--check-duplicate-headers", action="store_true",
                    help="(E) ALSO fail when one item NUMBER carries more than "
                         "one block inside one section: merge residue, "
                         "conflicting statuses, or two different items sharing "
                         "a number (the item 106 shape); and, one level down, "
                         "when an F-labelled finding block is written twice "
                         "inside a single item (item 428's F5-F13, item 433's "
                         "F1-F10).")
    ap.add_argument("--check-all", action="store_true",
                    help="turn on A, B, C, D and E. Does NOT turn on "
                         "--require-issue, which waits on the issue migration.")
    args = ap.parse_args(argv)
    if args.check_all:
        args.check_contradiction = True
        args.check_delegation = True
        args.check_orphan = True
        args.check_tier_parity = True
        args.check_duplicate_headers = True

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
    # PR context only. `--head-branch` is passed as `github.head_ref`, which is
    # empty on a push to main, so main gets the same run it always got.
    head_branch = args.head_branch.strip()
    if head_branch:
        findings += self_branch_findings(markers, head_branch)
    if args.require_issue:
        extra = issue_findings(text)
        if extra:
            findings.append(
                f"--require-issue: {len(extra)} open/partial item(s) cite no "
                f"GitHub issue:\n" + "\n".join("  " + e for e in extra)
            )
    for flag, label, produce in (
        (args.check_contradiction, "--check-contradiction",
         lambda: contradiction_findings(text)),
        (args.check_delegation, "--check-delegation",
         lambda: delegation_findings(text, dirs, namespaces, heads)),
        (args.check_orphan, "--check-orphan",
         lambda: orphan_findings(text, dirs, namespaces, heads)),
        (args.check_duplicate_headers, "--check-duplicate-headers",
         lambda: duplicate_header_findings(text) + duplicate_finding_blocks(text)),
        (args.check_tier_parity, "--check-tier-parity",
         lambda: tier_parity_findings(
             text, {p.name for p in (ROOT / "backends").iterdir() if p.is_dir()}
             if (ROOT / "backends").is_dir() else set())),
    ):
        if not flag:
            continue
        extra = produce()
        if extra:
            findings.append(
                f"{label}: {len(extra)} finding(s):\n"
                + "\n".join("  " + e for e in extra)
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

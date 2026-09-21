#!/usr/bin/env python3
"""Static gate: a CLOSED VOCABULARY declared in two places is recorded, and
every recorded copy still spells the same vocabulary.

WHY THIS EXISTS (issue #1285). Three unrelated lanes found the same structural
defect in one day, in three unrelated subsystems, none of them by a gate:

  1. `mcp/schema._JSON_TYPES` and `export_openapi._SCALARS` are two independent
     surface-type -> JSON Schema mappings (issue #1272).
  2. `bundle._canonical_ir`, `registry._normalize_ir_for_attest` and
     `truc.reproduce._normalized_ir` each carried their own copy of the IR path
     normalization (issue #1276). TWO HAD ALREADY DRIFTED, and one drift had
     left `truc reproduce`'s attestation tier structurally dead.
  3. `policy.TAINT_FOLD_ORIGINS` is a hand-kept mirror of
     `taint._SOURCE_CLASS_SCOPES`, and `mcp/approval.py::static_taint`
     INTERSECTS against it, so a missing entry silently drops an origin from
     the taint an auto-approve decision is made against (issue #1195).

Each copy was correct when written. They drift. Nothing reports the drift,
because the only thing that would notice is a consumer comparing two of them,
and no consumer does. That is the same argument the reference/self-host
differential rests on, applied to the compiler's own helpers.

WHAT COUNTS AS A MIRROR, and why this signal rather than another. All three
instances share one shape: a SET OR MAPPING OVER A CLOSED VOCABULARY that a
second module re-declares rather than imports. So the unit here is a
VOCABULARY SITE -- a named declaration whose vocabulary can be read statically:

  * a `const` site: a module-level assignment whose right-hand side is a
    collection display (set/list/tuple/dict, or `frozenset(...)`/`set(...)`
    over one) with at least MIN_TOKENS string-literal elements or keys.
    `policy.TAINT_FOLD_ORIGINS` and `taint._SOURCE_CLASS_SCOPES` are these.

  * a `func` site: a module-level function or a method, whose body reads at
    least MIN_TOKENS distinct string literals AS MAPPING KEYS (`d["k"]`,
    `d.get("k")`, `.setdefault`, `.pop`) or inside a collection display.
    `bundle._canonical_ir` and `registry._normalize_ir_for_attest` are these:
    neither declares a constant, both walk the same four IR field names.

Two sites MIRROR each other when their vocabularies are EXACTLY EQUAL and they
live in different modules. A class is the set of all sites sharing one
vocabulary.

EXACT EQUALITY IS THE POINT, not a thing to relax. A fuzzier relation was
measured on this tree and rejected with numbers (see `docs/process.md` and the
issue): Jaccard >= 0.9 over all string literals gives 117 pairs, most of them a
large function incidentally containing a small constant's tokens; a strict
SUBSET relation with a 0.5 size guard gives 424 pairs over const+func sites and
159 over const sites alone; and taking the transitive closure of any threshold
below 0.7 chains 27 to 30 unrelated declarations into one blob through a single
shared token (`rust`). Exact equality has no knob to turn, cannot chain, is
byte-stable, and is what "re-declares rather than imports" actually means.

MIN_TOKENS is 4 because instance 2's vocabulary is exactly four field names.
Three would be a coincidence budget, not a vocabulary: it raises the class
count on this tree from 42 to 68 for token sets that small.

A SECOND, NARROWER RULE: CLAIM-ANCHORED NEAR MISSES (issue #1336)
-----------------------------------------------------------------
Exact equality is blind in one direction, and issue #1336 is the instance.
`tools/evolution_controller.py::Verdict` carried a THIRD copy of item 536's
four verdict field names and its docstring said so. It had already grown a
fifth key, `code`, so equality grouped the two copies that still agreed and
never looked at the one that had left. The rule is strongest against copies
still in step and blind to the one that has already drifted. The false claim
sat in the place a reader is most likely to trust.

RELAXING THE RELATION IS THE WRONG REPAIR, and it was measured rather than
assumed. A STRICT SUPERSET with a bounded difference, at the tightest setting
that still contains issue #1336's case -- a difference of ONE token and a
minimum vocabulary of MIN_TOKENS -- over the tree as it stood at 9649f21c:

    131 ordered pairs, of which ONE is the case the issue was filed for.
    Transitive closure: 27 components, the largest 12 sites.

A two-token difference gives 248 pairs and a 30-site component, which is PR
#1295's blob rebuilt. Raising the minimum vocabulary to five drops to 63 pairs
but LOSES the target, whose smaller side is exactly four tokens. No setting
keeps the one true positive and suppresses the other 130: a 99% false-positive
rate is not a gate, and it is the same finding PR #1295 already recorded.

What works is not a fuzzier relation but a DIFFERENT ANCHOR. A vocabulary that
SAYS it mirrors another is a strictly easier case than one that merely happens
to, and this repository already resolves written citations
(`tools/check_roadmap_claims.py`). So:

  * a CLAIM is a vocabulary site whose own prose -- its docstring, the comment
    block directly above it, or its class's docstring and comment for a method
    -- contains one of CLAIM_CUES and names, in backticks, a module this scan
    covers. Three citation spellings resolve, because the tree uses all three:
    a path, a path with a declaration on it, and a dotted reference whose head
    is an UNAMBIGUOUS module basename.

  * the claim is SATISFIED when some site in a cited module spells exactly the
    claiming site's vocabulary; a NEAR MISS when none does and the closest
    differs by at most CLAIM_SLACK tokens; UNANCHORED otherwise.

  * ONLY A NEAR MISS REDS. Unanchored is not a finding: most prose containing
    "mirrors" is about something that is not a vocabulary, and firing on it is
    the blob by another route.

Measured on this tree (1590 sites): 100 sites carry a cue, 28 of those also
resolve a module, and at CLAIM_SLACK = 1 that is 4 SATISFIED claims, 1 NEAR
MISS and 23 UNANCHORED. Slack 2 gives 2 near misses and slack 3 gives 3:
linear, not a cliff. It CANNOT CHAIN AT ALL -- a claim is one arrow from a
named site to a named module, and nothing here takes a transitive closure --
so the failure mode that rejected every similarity threshold does not exist
for this rule. CLAIM_SLACK is 1 because one token is the smallest difference
that is a difference, and issue #1336's case is exactly one token.

The two rules are disjoint by construction. A satisfied claim is an equal pair,
so the class rule already holds it; the claim rule earns its place entirely in
the near-miss band, where the class rule says nothing. `--self-test` asserts
that on every claim case, by reporting whether exact equality sees the pair.

WHAT THE LEDGER IS, and why it is not an allowlist. `tests/fixtures/
vocabulary_mirror_ledger.json` records every class this tree already has:
the site names, the vocabulary they agree on, and a written reason. It is a
RATCHET in the shape PR #1280 established, and it differs from an allowlist in
the way that matters -- an allowlist stops checking an entry, and this keeps
checking every entry, in both directions:

  * an UNNAMED DIVERGENCE reds. Add `clipboard` to `taint._SOURCE_CLASS_SCOPES`
    and not to `policy.TAINT_FOLD_ORIGINS` and the recorded class no longer
    holds: the gate names the token each side has and the other lacks. That is
    instance 3's defect, caught.
  * a NAMED DIVERGENCE NO LONGER OBSERVED reds too, and the fix is to DELETE
    the entry. Resolve a mirror into one definition with the others as
    consumers and its class disappears from the scan; the stale entry is a
    lie about the tree and must go. That is what makes the ledger shrink-only.
  * a NEW class not in the ledger reds. That is the third exit clause of issue
    #1285: a fourth instance cannot arrive silently.

`--write` exists to author the ledger and is deliberately NOT called by any
routine regeneration command (`tools/regen_goldens.py`, `tools/pre_pr.sh`,
`make`). A ratchet that widens as a side effect of normal work is not a
ratchet: widening it is an edit somebody has to write a reason into and a
reviewer has to read.

A MISSING OR UNREADABLE LEDGER REDS. So does a VACUOUS SCAN -- zero sites, or
zero classes, means the walk broke, not that the tree is clean. Both are the
failure PR #1280 names: a check that reads "nothing to check" as "nothing
wrong" is one of the fourteen checks this repository has measured that ran on
every PR and could not fail.

NO SUBPROCESS, NO `import revl`. This file is stdlib-only and parses source
text with `ast`, so it runs in the `lint` job, which installs no revl at all,
and it cannot be fooled by the dev venv's editable meta-path finder into
measuring some other checkout.

Usage:
    python3 tools/check_vocabulary_mirrors.py             # the inventory
    python3 tools/check_vocabulary_mirrors.py --check     # the gate (exit 1)
    python3 tools/check_vocabulary_mirrors.py --write     # author the ledger
    python3 tools/check_vocabulary_mirrors.py --self-test # prove it can fire

`--self-test` runs the gate against the three issue-#1285 instances themselves
-- a new mirror introduced, a recorded one drifting apart on one side, a
resolved one left stale -- plus a missing ledger, a vacuous scan and a clean
baseline, and asserts the verdict on each. It then runs the claim rule against
issue #1336's shape -- a claim that holds, the same claim off by one token,
that near miss recorded and then resolved, its difference moved, a cue with no
resolvable citation and a claim citing a module with no comparable vocabulary
-- and asserts, for each, both the verdict AND whether exact equality sees the
pair at all. It runs in the `lint` job beside the gate. Never add `|| true`.
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys
from dataclasses import dataclass

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LEDGER = REPO_ROOT / "tests" / "fixtures" / "vocabulary_mirror_ledger.json"

#: The roots issue #1285 scopes the inventory to.
SCAN_ROOTS = ("src/revl", "tools")

#: The smallest token count that is a vocabulary rather than a coincidence.
#: Pinned by instance 2, whose vocabulary is exactly four IR field names.
MIN_TOKENS = 4

#: Mapping-key readers whose first positional argument is the key.
_KEY_METHODS = ("get", "setdefault", "pop")

#: Literal phrases that make a declaration's prose a CLAIM about another
#: declaration. Literal rather than a regex on purpose: the list is the rule,
#: and a reader has to be able to audit it without running it.
CLAIM_CUES = (
    "mirror", "copy of", "copies of", "restate", "restated", "restates",
    "re-declare", "redeclare", "same set", "same shape", "same vocabulary",
    "same field", "same fields", "same key", "same keys", "same name",
    "same names", "field names are", "must match", "in step with",
    "kept in step", "hand-kept",
)

#: How far a claiming site's vocabulary may sit from the closest one in the
#: module it cites and still be reported. One token is the smallest difference
#: that is a difference, and issue #1336's case is exactly one token.
CLAIM_SLACK = 1


@dataclass(frozen=True)
class Site:
    """One named declaration and the closed vocabulary it spells."""

    module: str          # path relative to the repo root
    name: str            # `NAME`, `fn`, or `Class.method`
    kind: str            # "const" | "func"
    tokens: frozenset    # the vocabulary
    lineno: int

    @property
    def sid(self) -> str:
        """The ledger's stable id: names only, no line number (a line number
        churns on every edit above it and says nothing about the mirror)."""
        return f"{self.module}::{self.name}"


@dataclass(frozen=True)
class Claim:
    """One site's written assertion that its vocabulary is another module's.

    `site` is the claiming site's `sid`, `tokens` its vocabulary, `cue` the
    phrase from `CLAIM_CUES` that made the prose a claim, and `cited` the
    modules the prose names in backticks that this scan actually covers.
    """

    site: str
    tokens: frozenset
    cue: str
    cited: tuple


# --------------------------------------------------------------- extraction

def _display_strings(node: ast.AST) -> list[str]:
    """The top-level string elements (or dict keys) of a collection display,
    including one wrapped in `frozenset(...)`/`set(...)`/`tuple(...)`/
    `list(...)`/`dict(...)`. Nested values are deliberately NOT read: the
    vocabulary of `{"Str": {"type": "string"}}` is `Str`, not `type`."""
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if isinstance(node, ast.Dict):
        return [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("frozenset", "set", "tuple", "list", "dict")
            and node.args):
        return _display_strings(node.args[0])
    return []


def _key_vocabulary(node: ast.AST) -> list[str]:
    """String literals used as a MAPPING KEY, or spelled inside a collection
    display, anywhere under `node`. A function's vocabulary is the set of
    fields it names, not every string it happens to contain: comparing against
    `"call"` is dispatch, reading `ir["components"]` is a field name."""
    out: list[str] = []
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant)
                and isinstance(sub.slice.value, str)):
            out.append(sub.slice.value)
        elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
              and sub.func.attr in _KEY_METHODS and sub.args
              and isinstance(sub.args[0], ast.Constant)
              and isinstance(sub.args[0].value, str)):
            out.append(sub.args[0].value)
        elif isinstance(sub, (ast.Set, ast.List, ast.Tuple, ast.Dict)):
            out.extend(_display_strings(sub))
    return out


def _without_docstring(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = fn.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        return body[1:]
    return body


def sites_in_source(source: str, module: str) -> list[Site]:
    """Every vocabulary site in one module's source text."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[Site] = []

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and not prefix:
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                name = next((t.id for t in targets if isinstance(t, ast.Name)), None)
                if name is None or node.value is None:
                    continue
                tokens = frozenset(_display_strings(node.value))
                if len(tokens) >= MIN_TOKENS:
                    found.append(Site(module, name, "const", tokens, node.lineno))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tokens: frozenset = frozenset(
                    tok for stmt in _without_docstring(node)
                    for tok in _key_vocabulary(stmt))
                if len(tokens) >= MIN_TOKENS:
                    found.append(Site(module, prefix + node.name, "func",
                                      tokens, node.lineno))
            elif isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")

    walk(tree.body, "")
    return found


def scan(root: pathlib.Path = REPO_ROOT) -> list[Site]:
    """Every vocabulary site under the scanned roots, in a stable order."""
    found: list[Site] = []
    for base in SCAN_ROOTS:
        for path in sorted((root / base).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            found.extend(sites_in_source(path.read_text(encoding="utf-8"), rel))
    found.sort(key=lambda s: (s.module, s.name))
    return found


# ------------------------------------------------------------ claim reading

def _prose_index(source: str) -> dict:
    """`lineno -> the prose attached to the declaration that starts there`.

    A declaration's prose is its own docstring, the comment block immediately
    above it, and -- for a method -- its class's docstring and comment block
    too. The class docstring counts because that is where a dataclass says what
    its fields are, which is exactly where issue #1336's false claim lived.
    """
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict = {}

    def lead(lineno: int) -> str:
        i, buf = lineno - 2, []
        while i >= 0 and lines[i].strip().startswith("#"):
            buf.append(lines[i].strip().lstrip("#").strip())
            i -= 1
        return " ".join(reversed(buf))

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                out[node.lineno] = lead(node.lineno)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[node.lineno] = (ast.get_docstring(node) or "") + " " + lead(node.lineno)
            elif isinstance(node, ast.ClassDef):
                owned = (ast.get_docstring(node) or "") + " " + lead(node.lineno)
                walk(node.body)
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out[sub.lineno] = out.get(sub.lineno, "") + " " + owned

    walk(tree.body)
    return out


def _cue_in(text: str) -> str:
    low = text.lower()
    return next((c for c in CLAIM_CUES if c in low), "")


def _cited_modules(text: str, self_module: str, modules: set,
                   by_stem: dict) -> tuple:
    """The scanned modules a claim's prose names, in backticks.

    Three spellings resolve, because the tree uses all three: a repository path
    (`tools/evolution_reward.py`), the same path with a declaration on it
    (`tools/evolution_reward.py::Verdict`), and a dotted reference whose head
    is a module basename (`policy.TAINT_FOLD_ORIGINS`). A basename resolves
    ONLY when it is unambiguous across the scanned roots: a guess about which
    of two modules was meant is a finding this gate has no business reporting.
    """
    out = set()
    spans = text.split("`")
    for raw in spans[1::2]:
        for sep in ",;()[]":
            raw = raw.replace(sep, " ")
        for word in raw.split():
            word = word.strip().strip(".,:;'\"")
            if not word:
                continue
            if word in modules:
                out.add(word)
                continue
            head = word.split("::")[0]
            if head.endswith(".py"):
                head = head[:-3]
            stem = head.rsplit("/", 1)[-1].split(".")[0]
            candidates = by_stem.get(stem) or ()
            if len(candidates) == 1:
                out |= set(candidates)
    out.discard(self_module)
    return tuple(sorted(out))


def claims_in_source(source: str, module: str, sites: list[Site],
                     modules: set, by_stem: dict) -> list[Claim]:
    """Every claim one module's sites make. A site with no cue, or with a cue
    and no resolvable citation, makes no claim and is not reported."""
    prose = _prose_index(source)
    found: list[Claim] = []
    for site in sites:
        text = prose.get(site.lineno) or ""
        cue = _cue_in(text)
        if not cue:
            continue
        cited = _cited_modules(text, module, modules, by_stem)
        if cited:
            found.append(Claim(site.sid, site.tokens, cue, cited))
    return found


def scan_claims(sites: list[Site], root: pathlib.Path = REPO_ROOT) -> list[Claim]:
    """Every claim the scanned tree makes, in a stable order."""
    modules = {s.module for s in sites}
    by_stem: dict = {}
    for module in modules:
        by_stem.setdefault(pathlib.PurePosixPath(module).stem, []).append(module)
    by_stem = {k: tuple(sorted(v)) for k, v in by_stem.items()}

    by_module: dict = {}
    for site in sites:
        by_module.setdefault(site.module, []).append(site)

    found: list[Claim] = []
    for module in sorted(by_module):
        source = (root / module).read_text(encoding="utf-8")
        found.extend(claims_in_source(source, module, by_module[module],
                                      modules, by_stem))
    found.sort(key=lambda c: c.site)
    return found


def near_misses(sites: list[Site], claims: list[Claim]) -> list[dict]:
    """The claims that are NEAR MISSES: no site in a cited module spells the
    claiming site's vocabulary, and the closest one differs by at most
    CLAIM_SLACK tokens.

    A SATISFIED claim (some cited site spells it exactly) and an UNANCHORED one
    (nothing in the cited modules is within slack) are both absent from this
    list, for opposite reasons: the first is the claim holding, and the second
    is prose about something that is not a vocabulary."""
    by_module: dict = {}
    for site in sites:
        by_module.setdefault(site.module, []).append(site)
    out = []
    for claim in claims:
        pool = [t for m in claim.cited for t in by_module.get(m, ())
                if t.sid != claim.site]
        if not pool or any(t.tokens == claim.tokens for t in pool):
            continue
        best = min(pool, key=lambda t: (len(claim.tokens ^ t.tokens), t.sid))
        if len(claim.tokens ^ best.tokens) > CLAIM_SLACK:
            continue
        out.append({
            "site": claim.site,
            "mirrors": best.sid,
            "only_here": sorted(claim.tokens - best.tokens),
            "only_there": sorted(best.tokens - claim.tokens),
        })
    out.sort(key=lambda e: (e["site"], e["mirrors"]))
    return out


def classes(sites: list[Site]) -> list[dict]:
    """The mirror classes: sites whose vocabularies are exactly equal, spanning
    at least two distinct modules. A class is the inventory entry."""
    by_vocab: dict[frozenset, list[Site]] = {}
    for site in sites:
        by_vocab.setdefault(site.tokens, []).append(site)
    out = []
    for tokens, members in by_vocab.items():
        if len(members) < 2 or len({m.module for m in members}) < 2:
            continue
        out.append({"sites": sorted(m.sid for m in members),
                    "tokens": sorted(tokens)})
    out.sort(key=lambda c: (c["sites"][0], c["sites"]))
    return out


# --------------------------------------------------------------- the ledger

def _read_ledger(path: pathlib.Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, f"the ledger is missing: {path}"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"the ledger is unreadable: {path}: {exc}"
    return doc, None


def load_ledger(path: pathlib.Path) -> tuple[list[dict] | None, str | None]:
    """The ledger's CLASS entries, or `(None, why)`. A ledger that cannot be
    read is a failure, never an empty check."""
    doc, err = _read_ledger(path)
    if doc is None:
        return None, err
    entries = doc.get("classes")
    if not isinstance(entries, list):
        return None, f"the ledger has no `classes` list: {path}"
    for entry in entries:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("sites"), list)
                or not isinstance(entry.get("tokens"), list)):
            return None, f"a ledger entry is malformed: {entry!r}"
    return entries, None


def load_claim_ledger(path: pathlib.Path) -> tuple[list[dict] | None, str | None]:
    """The ledger's CLAIM entries: the near misses this tree already has, each
    with the reason the difference is allowed to stand."""
    doc, err = _read_ledger(path)
    if doc is None:
        return None, err
    entries = doc.get("claims")
    if not isinstance(entries, list):
        return None, f"the ledger has no `claims` list: {path}"
    for entry in entries:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("site"), str)
                or not isinstance(entry.get("mirrors"), str)
                or not isinstance(entry.get("only_here"), list)
                or not isinstance(entry.get("only_there"), list)):
            return None, f"a claim entry is malformed: {entry!r}"
    return entries, None


def ledger_text(entries: list[dict], claim_entries: list[dict] | None = None) -> str:
    """The ledger's one on-disk spelling, byte-identical under any interpreter
    that can run this file: names and tokens only, both sorted, no counts and
    no line numbers."""
    doc = {
        "//": ("Issue #1285. Every closed vocabulary this tree declares in more "
               "than one place, with the reason it is still declared twice. "
               "Written by `python3 tools/check_vocabulary_mirrors.py --write`, "
               "which no routine regeneration command calls; widening it is an "
               "edit a reviewer reads. Shrink-only: resolving a mirror into one "
               "definition deletes its entry."),
        "classes": [{"sites": sorted(e["sites"]),
                     "tokens": sorted(e["tokens"]),
                     "note": e.get("note", "")}
                    for e in sorted(entries, key=lambda e: (sorted(e["sites"])[0],
                                                            sorted(e["sites"])))],
        "//claims": ("Issue #1336. The CLAIM-ANCHORED near misses: a site whose "
                     "prose says its vocabulary is another module's, where the "
                     "closest vocabulary in that module is not quite the same "
                     "one. Same ratchet, same shrink-only rule: a near miss "
                     "that resolves has its entry DELETED."),
        "claims": [{"site": e["site"],
                    "mirrors": e["mirrors"],
                    "only_here": sorted(e["only_here"]),
                    "only_there": sorted(e["only_there"]),
                    "note": e.get("note", "")}
                   for e in sorted(claim_entries or [],
                                   key=lambda e: (e["site"], e["mirrors"]))],
    }
    return json.dumps(doc, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


# --------------------------------------------------------------- the gate

def check(sites: list[Site], entries: list[dict] | None,
          ledger_error: str | None) -> list[str]:
    """Every reason this tree fails the gate, in the order a reader wants
    them. An empty list is the only green."""
    problems: list[str] = []

    if entries is None:
        return [f"{ledger_error}\n"
                "    A ledger that cannot be read is a RED, not an empty check.\n"
                "    Restore it from git, or author it with "
                "`python3 tools/check_vocabulary_mirrors.py --write`."]

    # VACUITY. Disjoint from everything below: the ledger comparison says
    # nothing at all about a scan that found nothing, because an empty scan
    # matches an empty ledger.
    if not sites:
        problems.append(
            "VACUOUS SCAN: no vocabulary site at all under "
            f"{', '.join(SCAN_ROOTS)}. The walk is broken, or the roots moved. "
            "A scan that finds nothing is not a clean tree.")
        return problems
    found = classes(sites)
    if not found:
        problems.append(
            f"VACUOUS SCAN: {len(sites)} vocabulary sites and not one mirror "
            "class. This tree has always had some; the comparison is broken.")
        return problems

    grown: set[frozenset] = set()
    by_key = {tuple(c["sites"]): c for c in found}
    recorded = {tuple(sorted(e["sites"])): e for e in entries}
    live = {s.sid: s for s in sites}

    for key, entry in sorted(recorded.items()):
        if not (entry.get("note") or "").strip():
            problems.append(
                f"NO RECORDED REASON for {' + '.join(key)}.\n"
                "    Issue #1285 asks for one definition with the others as "
                "consumers, or a recorded reason. Write the reason.")
        if key in by_key:
            got = by_key[key]
            if sorted(got["tokens"]) != sorted(entry["tokens"]):
                added = sorted(set(got["tokens"]) - set(entry["tokens"]))
                gone = sorted(set(entry["tokens"]) - set(got["tokens"]))
                problems.append(
                    f"VOCABULARY CHANGED for {' + '.join(key)}.\n"
                    f"    every copy moved together, which is not a drift, but "
                    f"the ledger still records the old vocabulary.\n"
                    f"    added: {added or '-'}\n"
                    f"    gone:  {gone or '-'}\n"
                    "    Re-record it with `--write` and keep the reason.")
            continue

        # The entry is stale. Say WHY, because the fix differs.
        missing = [sid for sid in key if sid not in live]
        if not missing and len({live[sid].tokens for sid in key}) == 1:
            # The recorded copies still agree; the class grew a new copy. That
            # is a NEW MIRROR joining a recorded one, reported once, below.
            grown.add(frozenset(key))
            continue
        if missing:
            problems.append(
                f"NAMED MIRROR NO LONGER OBSERVED: {' + '.join(key)}\n"
                f"    gone from the tree: {', '.join(missing)}\n"
                "    If the duplication was resolved into one definition with "
                "the others as consumers, DELETE this entry: the ledger is "
                "shrink-only and a stale entry is a lie about the tree.")
            continue
        vocabs = {sid: sorted(live[sid].tokens) for sid in key}
        shared = set.intersection(*[set(v) for v in vocabs.values()])
        detail = []
        for sid, toks in vocabs.items():
            only = sorted(set(toks) - shared)
            detail.append(f"      {sid}: {len(toks)} tokens"
                          + (f", only here: {only}" if only else ""))
        problems.append(
            f"UNNAMED DIVERGENCE: {' + '.join(key)}\n"
            "    these were recorded as one vocabulary and no longer spell the "
            "same one.\n" + "\n".join(detail) + "\n"
            "    This is the issue-#1285 drift: one copy moved and the others "
            "did not. Put the vocabularies back in step, or resolve the "
            "duplication into one definition and delete this entry.")

    for key, got in sorted(by_key.items()):
        if key in recorded:
            continue
        joined = next((g for g in grown if g < set(key)), None)
        if joined is not None:
            problems.append(
                f"A RECORDED MIRROR GREW A COPY: {' + '.join(sorted(joined))}\n"
                f"    now also declared by: "
                f"{', '.join(sorted(set(key) - joined))}\n"
                f"    vocabulary ({len(got['tokens'])}): {got['tokens']}\n"
                "    The recorded copies still agree, so nothing has drifted "
                "YET; a third copy is a third thing to keep in step.\n"
                "    Import one of the existing ones, or re-record the class "
                "with `--write` and say in the `note` why a third is needed.")
            continue
        problems.append(
            f"NEW MIRROR: {' + '.join(key)}\n"
            f"    vocabulary ({len(got['tokens'])}): {got['tokens']}\n"
            "    A closed vocabulary declared in more than one place with "
            "nothing holding the copies together.\n"
            "    Either make one of them the definition and the others "
            "consumers (import it), or record the reason in\n"
            f"    {LEDGER.relative_to(REPO_ROOT).as_posix()} with "
            "`python3 tools/check_vocabulary_mirrors.py --write` and write the "
            "`note`.")
    return problems


def check_claims(claims: list[Claim], observed: list[dict],
                 entries: list[dict] | None,
                 ledger_error: str | None) -> list[str]:
    """Every reason this tree fails the CLAIM rule. An empty list is green.

    Three ways to fail, the same three shapes the class rule has: an UNRECORDED
    near miss, a RECORDED one whose difference moved, and a RECORDED one that
    is no longer observed (which is the shrink-only clause -- resolving a near
    miss DELETES its entry)."""
    problems: list[str] = []

    if entries is None:
        return [f"{ledger_error}\n"
                "    A ledger that cannot be read is a RED, not an empty check.\n"
                "    Restore it from git, or author it with "
                "`python3 tools/check_vocabulary_mirrors.py --write`."]

    if not claims:
        return ["VACUOUS CLAIM SCAN: not one vocabulary site in this tree says "
                "it mirrors another module.\n"
                "    This tree has always had some; the prose walk is broken. "
                "A scan that finds nothing is not a clean tree."]

    by_key = {(e["site"], e["mirrors"]): e for e in observed}
    recorded = {(e["site"], e["mirrors"]): e for e in entries}
    claiming = {c.site for c in claims}

    for key, entry in sorted(recorded.items()):
        site, mirrors = key
        if not (entry.get("note") or "").strip():
            problems.append(
                f"NO RECORDED REASON for the near miss {site} -> {mirrors}.\n"
                "    A difference that is allowed to stand needs the sentence "
                "saying why. Write it.")
        if key not in by_key:
            why = ("the claim no longer names that module, or the site is gone"
                   if site not in claiming
                   else "the two vocabularies now agree exactly")
            problems.append(
                f"NAMED NEAR MISS NO LONGER OBSERVED: {site} -> {mirrors}\n"
                f"    {why}.\n"
                "    If the near miss was resolved -- by importing, by "
                "extending rather than restating, or by putting the "
                "vocabularies back in step -- DELETE this entry. The ledger is "
                "shrink-only and a stale entry is a lie about the tree.")
            continue
        got = by_key[key]
        if (sorted(got["only_here"]) != sorted(entry["only_here"])
                or sorted(got["only_there"]) != sorted(entry["only_there"])):
            problems.append(
                f"NEAR MISS CHANGED: {site} -> {mirrors}\n"
                f"    recorded: only here {sorted(entry['only_here']) or '-'}, "
                f"only there {sorted(entry['only_there']) or '-'}\n"
                f"    observed: only here {got['only_here'] or '-'}, "
                f"only there {got['only_there'] or '-'}\n"
                "    The recorded reason was written about a different "
                "difference. Re-record it with `--write` and rewrite the "
                "reason, or close the difference.")

    for key, got in sorted(by_key.items()):
        if key in recorded:
            continue
        problems.append(
            f"UNRECORDED NEAR MISS: {got['site']}\n"
            f"    says it mirrors the module that declares {got['mirrors']}, "
            "and does not spell the same vocabulary.\n"
            f"    only here:  {got['only_here'] or '-'}\n"
            f"    only there: {got['only_there'] or '-'}\n"
            "    This is issue #1336's shape: a written claim of agreement "
            "that does not hold, which is worse than no claim at all.\n"
            "    Make the claim true -- import the vocabulary, or extend it "
            "in one declared place -- or, if the difference is deliberate, say "
            "so in the prose AND record it in\n"
            f"    {LEDGER.relative_to(REPO_ROOT).as_posix()} with "
            "`python3 tools/check_vocabulary_mirrors.py --write`.")
    return problems


# --------------------------------------------------------------- the report

def report(sites: list[Site], entries: list[dict] | None,
           claims: list[Claim] | None = None,
           claim_entries: list[dict] | None = None) -> str:
    found = classes(sites)
    notes = {}
    if entries:
        notes = {tuple(sorted(e["sites"])): e.get("note", "") for e in entries}
    lines = [f"{len(sites)} vocabulary sites under {', '.join(SCAN_ROOTS)}",
             f"{len(found)} mirror classes "
             f"({sum(len(c['sites']) for c in found)} sites)", ""]
    for cls in found:
        lines.append(f"[{len(cls['sites'])} sites] {cls['tokens']}")
        for sid in cls["sites"]:
            lines.append(f"    {sid}")
        note = notes.get(tuple(cls["sites"]))
        lines.append(f"    note: {note}" if note else "    note: (NOT RECORDED)")
        lines.append("")

    if claims is None:
        return "\n".join(lines)
    observed = near_misses(sites, claims)
    satisfied = len(claims) - len(observed) - _unanchored(sites, claims)
    claim_notes = {(e["site"], e["mirrors"]): e.get("note", "")
                   for e in (claim_entries or [])}
    lines += [f"{len(claims)} claims (a site whose prose says its vocabulary is "
              "another module's)",
              f"    {satisfied} satisfied, {len(observed)} near miss(es), "
              f"{_unanchored(sites, claims)} unanchored", ""]
    for entry in observed:
        lines.append(f"[near miss] {entry['site']} -> {entry['mirrors']}")
        lines.append(f"    only here:  {entry['only_here'] or '-'}")
        lines.append(f"    only there: {entry['only_there'] or '-'}")
        note = claim_notes.get((entry["site"], entry["mirrors"]))
        lines.append(f"    note: {note}" if note else "    note: (NOT RECORDED)")
        lines.append("")
    return "\n".join(lines)


def _unanchored(sites: list[Site], claims: list[Claim]) -> int:
    """Claims whose cited modules hold nothing within slack. Counted for the
    inventory only: an unanchored claim is prose about something that is not a
    vocabulary, and firing on it is the blob this rule exists to avoid."""
    by_module: dict = {}
    for site in sites:
        by_module.setdefault(site.module, []).append(site)
    count = 0
    for claim in claims:
        pool = [t for m in claim.cited for t in by_module.get(m, ())
                if t.sid != claim.site]
        if not pool:
            count += 1
            continue
        closest = min(len(claim.tokens ^ t.tokens) for t in pool)
        if closest > CLAIM_SLACK:
            count += 1
    return count


# --------------------------------------------------------------- self-test

_CLEAN_A = '''
ORIGINS = frozenset({"web", "net", "fs", "model", "input"})
'''

_CLEAN_B = '''
from .a import ORIGINS

def use(x):
    return x["components"], x["manifest"], x["source"], x["file"]
'''

_CLEAN_C = '''
def normalize(ir):
    out = dict(ir)
    for comp in out.get("components") or []:
        for field in ("source", "file"):
            comp[field] = comp.get(field)
    for comp in (out.get("manifest") or {}).get("components") or []:
        comp["file"] = comp.get("file")
    return out
'''


# The claim cases (issue #1336). `d.py` is the definition; the others make a
# written claim about it that either holds, is off by one token, or is prose
# about something that is not a vocabulary at all.
_CLAIM_DEF = '''
FIELDS = ("component", "evidence", "reason", "verdict")
'''

_CLAIM_HOLDS = '''
# The same field names as `src/revl/d.py`, kept in step by hand.
ANSWER = {"component": 1, "evidence": 2, "reason": 3, "verdict": 4}
'''

_CLAIM_OFF_BY_ONE = '''
# The same field names as `src/revl/d.py`, kept in step by hand.
ANSWER = {"component": 1, "evidence": 2, "reason": 3, "verdict": 4, "code": 5}
'''

_CLAIM_NO_CITATION = '''
# A hand-kept mirror of the answer shape the reducer downstream reads.
ANSWER = {"alpha": 1, "beta": 2, "gamma": 3, "delta": 4, "epsilon": 5}
'''

_CLAIM_UNRELATED = '''
# Mirrors the ordering `src/revl/d.py` walks; not its field names.
ORDER = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh")
'''


def _claims_from(tree: dict[str, str], sites: list[Site]) -> list[Claim]:
    modules = {s.module for s in sites}
    by_stem: dict = {}
    for module in modules:
        by_stem.setdefault(pathlib.PurePosixPath(module).stem, []).append(module)
    by_stem = {k: tuple(sorted(v)) for k, v in by_stem.items()}
    out: list[Claim] = []
    for name, text in sorted(tree.items()):
        module = f"src/revl/{name}"
        mine = [s for s in sites if s.module == module]
        out.extend(claims_in_source(text, module, mine, modules, by_stem))
    out.sort(key=lambda c: c.site)
    return out


def _claim_ledger_for(sites: list[Site], claims: list[Claim],
                      note: str = "self-test") -> list[dict]:
    return [dict(e, note=note) for e in near_misses(sites, claims)]


def _ledger_for(sites: list[Site], note: str = "self-test") -> list[dict]:
    return [dict(c, note=note) for c in classes(sites)]


def _sites_from(tree: dict[str, str]) -> list[Site]:
    out: list[Site] = []
    for name, text in sorted(tree.items()):
        out.extend(sites_in_source(text, f"src/revl/{name}"))
    out.sort(key=lambda s: (s.module, s.name))
    return out


def self_test() -> int:
    base = {"a.py": _CLEAN_A, "b.py": _CLEAN_B, "c.py": _CLEAN_C}
    base_sites = _sites_from(base)
    base_ledger = _ledger_for(base_sites)

    cases: list[tuple[str, list[Site], list[dict] | None, str | None, bool]] = []

    cases.append(("the baseline tree is green against its own ledger",
                  base_sites, base_ledger, None, True))

    # issue #1285 clause 3: a FOURTH instance arriving.
    new_mirror = dict(base)
    new_mirror["d.py"] = 'ADMIT = ("web", "net", "fs", "model", "input")\n'
    cases.append(("a NEW mirror of the taint-origin vocabulary reds",
                  _sites_from(new_mirror), base_ledger, None, False))

    # A recorded class gaining a THIRD copy, still in step. Nothing has
    # drifted yet, which is exactly when it is cheap to stop.
    grew = dict(base)
    grew["d.py"] = ('def normalize2(ir):\n'
                    '    return (ir["components"], ir["manifest"],\n'
                    '            ir["source"], ir["file"])\n')
    cases.append(("a recorded mirror GROWING a third copy reds",
                  _sites_from(grew), base_ledger, None, False))

    # issue #1195: one side gains a token, the other does not.
    drift = dict(base)
    drift["a.py"] = _CLEAN_A.replace('"input"}', '"input", "clipboard"}')
    drift["d.py"] = 'ADMIT = frozenset({"web", "net", "fs", "model", "input"})\n'
    drift_ledger = _ledger_for(_sites_from(
        {**base, "d.py": drift["d.py"]}))
    cases.append(("a recorded mirror DRIFTING on one side reds",
                  _sites_from(drift), drift_ledger, None, False))

    # issue #1276: the duplication was resolved; the entry must go.
    resolved = dict(base)
    resolved["c.py"] = "from .b import use\n\ndef normalize(ir):\n    return use(ir)\n"
    resolved_ledger = _ledger_for(base_sites)
    cases.append(("a RESOLVED mirror left in the ledger reds (shrink-only)",
                  _sites_from(resolved), resolved_ledger, None, False))

    cases.append(("an entry with no recorded reason reds",
                  base_sites, _ledger_for(base_sites, note="  "), None, False))

    cases.append(("a MISSING ledger reds rather than reading as nothing to check",
                  base_sites, None, "the ledger is missing: <self-test>", False))

    cases.append(("a VACUOUS scan reds rather than reading as a clean tree",
                  [], base_ledger, None, False))

    # The control: an edit that touches no vocabulary is green on both trees.
    control = dict(base)
    control["b.py"] = _CLEAN_B + "\n\ndef unrelated(n):\n    return n + 1\n"
    cases.append(("the control (an edit that touches no vocabulary) is green",
                  _sites_from(control), base_ledger, None, True))

    # ---- issue #1336: the CLAIM rule, in the near-miss band exact equality
    # cannot see. Each case declares whether exact equality sees a class at
    # all, because "the class rule is SILENT here" is the whole claim this
    # second rule makes for itself.
    claim_base = {"d.py": _CLAIM_DEF, "e.py": _CLAIM_HOLDS}
    claim_sites = _sites_from(claim_base)
    held = _claims_from(claim_base, claim_sites)

    claim_cases: list[tuple[str, list[Site], list[Claim], list[dict] | None,
                            str | None, bool, bool]] = []

    claim_cases.append(("a claim that HOLDS is green with an empty ledger",
                        claim_sites, held, [], None, True, True))

    # issue #1336 itself: the prose says the field names are the other
    # module's, and this side has grown a fifth.
    off = {"d.py": _CLAIM_DEF, "e.py": _CLAIM_OFF_BY_ONE}
    off_sites = _sites_from(off)
    off_claims = _claims_from(off, off_sites)
    off_ledger = _claim_ledger_for(off_sites, off_claims)
    claim_cases.append(("a claim OFF BY ONE TOKEN reds (issue #1336)",
                        off_sites, off_claims, [], None, False, False))
    claim_cases.append(("the same near miss, RECORDED with a reason, is green",
                        off_sites, off_claims, off_ledger, None, True, False))
    claim_cases.append(("a recorded near miss with NO REASON reds",
                        off_sites, off_claims,
                        _claim_ledger_for(off_sites, off_claims, note="  "),
                        None, False, False))
    claim_cases.append(("a RESOLVED near miss left in the ledger reds "
                        "(shrink-only)",
                        claim_sites, held, off_ledger, None, False, True))

    # The difference moved inside the slack. The reason on file was written
    # about the old difference, so it is no longer a reason.
    moved = {"d.py": _CLAIM_DEF,
             "e.py": _CLAIM_OFF_BY_ONE.replace('"code": 5', '"blocker": 5')}
    moved_sites = _sites_from(moved)
    claim_cases.append(("a recorded near miss whose DIFFERENCE MOVED reds",
                        moved_sites, _claims_from(moved, moved_sites),
                        off_ledger, None, False, False))

    # The two controls that keep this rule narrow. Firing on either rebuilds
    # the 27-site blob PR #1295 measured, by another route.
    no_cite = dict(claim_base, f=_CLAIM_NO_CITATION)
    no_cite = {"d.py": _CLAIM_DEF, "e.py": _CLAIM_HOLDS,
               "f.py": _CLAIM_NO_CITATION}
    no_cite_sites = _sites_from(no_cite)
    claim_cases.append(("a cue with NO RESOLVABLE CITATION is not a finding",
                        no_cite_sites, _claims_from(no_cite, no_cite_sites),
                        [], None, True, True))
    unrelated = {"d.py": _CLAIM_DEF, "e.py": _CLAIM_UNRELATED}
    unrelated_sites = _sites_from(unrelated)
    claim_cases.append(("a claim citing a module with NO comparable vocabulary "
                        "is not a finding",
                        unrelated_sites, _claims_from(unrelated, unrelated_sites),
                        [], None, True, False))

    claim_cases.append(("a VACUOUS claim scan reds rather than reading as a "
                        "clean tree", claim_sites, [], [], None, False, True))
    claim_cases.append(("a MISSING ledger reds rather than reading as nothing "
                        "to check", claim_sites, held, None,
                        "the ledger is missing: <self-test>", False, True))

    failures = 0
    for name, sites, entries, err, want_green in cases:
        problems = check(sites, entries, err)
        green = not problems
        ok = green == want_green
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}"
              f"  ({'green' if green else f'{len(problems)} problem(s)'})")
        if not ok:
            failures += 1
            for line in problems:
                print("        " + line.replace("\n", "\n        "))

    for name, sites, claims, entries, err, want_green, want_class in claim_cases:
        problems = check_claims(claims, near_misses(sites, claims), entries, err)
        seen_by_equality = bool(classes(sites))
        green = not problems
        ok = green == want_green and seen_by_equality == want_class
        print(f"  {'ok  ' if ok else 'FAIL'}  [claim] {name}"
              f"  ({'green' if green else f'{len(problems)} problem(s)'}, "
              f"exact equality "
              f"{'groups them' if seen_by_equality else 'sees nothing'})")
        if not ok:
            failures += 1
            for line in problems:
                print("        " + line.replace("\n", "\n        "))

    if failures:
        print(f"\nself-test: {failures} case(s) did not behave as declared")
        return 1
    print(f"\nself-test: {len(cases)} class cases and {len(claim_cases)} claim "
          "cases, the gate fires on each defect and stays green on the "
          "baseline and the controls")
    return 0


# --------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="fail on a new, drifted or stale mirror")
    parser.add_argument("--write", action="store_true",
                        help="author the ledger (NOT a routine regeneration)")
    parser.add_argument("--self-test", action="store_true",
                        help="run the gate against the issue-#1285 instances")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    sites = scan()
    claims = scan_claims(sites)

    if args.write:
        entries, _ = load_ledger(LEDGER)
        notes = {tuple(sorted(e["sites"])): e.get("note", "")
                 for e in (entries or [])}
        fresh = [dict(c, note=notes.get(tuple(c["sites"]), ""))
                 for c in classes(sites)]
        claim_entries, _ = load_claim_ledger(LEDGER)
        claim_notes = {(e["site"], e["mirrors"]): e.get("note", "")
                       for e in (claim_entries or [])}
        fresh_claims = [dict(e, note=claim_notes.get((e["site"], e["mirrors"]), ""))
                        for e in near_misses(sites, claims)]
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(ledger_text(fresh, fresh_claims), encoding="utf-8")
        missing = sum(1 for e in fresh + fresh_claims
                      if not (e["note"] or "").strip())
        print(f"wrote {LEDGER.relative_to(REPO_ROOT).as_posix()}: "
              f"{len(fresh)} classes, {len(fresh_claims)} claim near misses, "
              f"{missing} still needing a reason")
        return 0

    entries, err = load_ledger(LEDGER)
    claim_entries, claim_err = load_claim_ledger(LEDGER)

    if args.check:
        problems = check(sites, entries, err)
        problems += check_claims(claims, near_misses(sites, claims),
                                 claim_entries, claim_err)
        if problems:
            print(f"{len(problems)} problem(s) -- issue #1285, closed "
                  "vocabularies declared in more than one place, and issue "
                  "#1336, written claims of agreement that do not hold:\n")
            for problem in problems:
                print("  " + problem.replace("\n", "\n  "))
                print()
            return 1
        print(f"{len(sites)} vocabulary sites, "
              f"{len(classes(sites))} mirror classes, all recorded and in "
              f"step; {len(claims)} claims, "
              f"{len(near_misses(sites, claims))} recorded near miss(es)")
        return 0

    print(report(sites, entries, claims, claim_entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())

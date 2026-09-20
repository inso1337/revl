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
baseline, and asserts the verdict on each. It runs in the `lint` job beside the
gate. Never add `|| true`.
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

def load_ledger(path: pathlib.Path) -> tuple[list[dict] | None, str | None]:
    """The ledger's entries, or `(None, why)`. A ledger that cannot be read is
    a failure, never an empty check."""
    if not path.exists():
        return None, f"the ledger is missing: {path}"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"the ledger is unreadable: {path}: {exc}"
    entries = doc.get("classes")
    if not isinstance(entries, list):
        return None, f"the ledger has no `classes` list: {path}"
    for entry in entries:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("sites"), list)
                or not isinstance(entry.get("tokens"), list)):
            return None, f"a ledger entry is malformed: {entry!r}"
    return entries, None


def ledger_text(entries: list[dict]) -> str:
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


# --------------------------------------------------------------- the report

def report(sites: list[Site], entries: list[dict] | None) -> str:
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
    return "\n".join(lines)


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
    if failures:
        print(f"\nself-test: {failures} case(s) did not behave as declared")
        return 1
    print(f"\nself-test: {len(cases)} cases, the gate fires on each defect and "
          "stays green on the baseline and the control")
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

    if args.write:
        entries, _ = load_ledger(LEDGER)
        notes = {tuple(sorted(e["sites"])): e.get("note", "")
                 for e in (entries or [])}
        fresh = [dict(c, note=notes.get(tuple(c["sites"]), ""))
                 for c in classes(sites)]
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(ledger_text(fresh), encoding="utf-8")
        missing = sum(1 for e in fresh if not (e["note"] or "").strip())
        print(f"wrote {LEDGER.relative_to(REPO_ROOT).as_posix()}: "
              f"{len(fresh)} classes, {missing} still needing a reason")
        return 0

    entries, err = load_ledger(LEDGER)

    if args.check:
        problems = check(sites, entries, err)
        if problems:
            print(f"{len(problems)} problem(s) -- issue #1285, closed "
                  "vocabularies declared in more than one place:\n")
            for problem in problems:
                print("  " + problem.replace("\n", "\n  "))
                print()
            return 1
        print(f"{len(sites)} vocabulary sites, "
              f"{len(classes(sites))} mirror classes, all recorded and in step")
        return 0

    print(report(sites, entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())

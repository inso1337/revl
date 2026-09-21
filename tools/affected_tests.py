#!/usr/bin/env python3
"""Affected-test selector for the revl pre-merge gate.

WHY THIS EXISTS. `make pre-merge` (tools/pre_merge.sh, roadmap item 327) runs the
full ~8-minute `tests/` suite plus every per-backend emit/golden suite on every
change. An agent that touched one emitter or one stdlib module should not pay for
all of it on the inner loop. This selector maps the changed files to the MINIMAL
set of pre-merge targets to run, and `sh tools/pre_merge.sh --affected` runs only
those.

SOUNDNESS INVARIANT (load-bearing). The selection is a CONSERVATIVE SUPERSET: it
must NEVER skip a test that could be affected. Every ambiguous case fails SAFE to
the FULL gate, never fails open. Concretely:

  * A change to any file on the compile-reachable import graph of `compile_source`
    (the frontend pipeline: parser, typecheck, lower, compiler, lexer, and the
    modules they transitively load, lazy imports included) -> FULL. That graph is
    computed here from the real tree (see `compile_reachable`) so a newly added
    core module cannot silently fall through to a narrow selection.
  * A changed file matching NO mapping rule -> FULL.
  * Structural changes (Makefile, tools/pre_merge.sh, CI config, tests/conftest.py,
    shared test helpers/fixtures, the reference IR, or a test file DELETED) -> FULL.
  * A test file ADDED is read rather than assumed (issue #162, see
    `_test_add_delete_override`): it is always run, and it escalates to FULL only
    when it cannot be mapped to something the same diff touched.

WHY DELETE STILL ESCALATES BUT ADD NO LONGER DOES. A deleted test file can break
a SURVIVING test: tests in this repo do import one another (test_gate_crate_admit,
test_gate_wasm_vector and test_inprocess_gate_rust all `import test_selfhost_lower
as oracle`, and test_274_navigable_slice2 imports test_evidence_policy), so the
removal is not self-contained and the selector cannot see the blast radius without
resolving the whole cross-import graph. An ADDED file cannot be imported by an
existing test — nothing could name a file that did not exist — so the only thing
its arrival changes is that it must itself run. Escalating for it bought nothing
that running it does not buy, and it fired on nearly every PR (issue #162).

`--affected` is the INNER-LOOP gate only. The full `make pre-merge` stays the
pre-release / CI gate; this never replaces it.

MACHINE OUTPUT (one key per line, for tools/pre_merge.sh to consume):
    FULL 0|1
    REASON <one-line reason>
    PYTEST <space-separated pytest node-ids/paths, or empty>
    BACKENDS <space-separated tiers with a dedicated pre-merge step>
    GATES <space-separated of: conformance site-wheel ruff formal>
Lines beginning with '# ' are the human summary and are ignored by the parser.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

# Every backend tier that has a checked-in emitter / golden tree.
BACKEND_TIERS = ("python", "go", "rust", "wasm", "java", "typescript")
# Tiers with a dedicated per-backend step in tools/pre_merge.sh. typescript is
# deliberately absent there (its vitest/tsc suite is heavy, CI-only); a ts change
# is still covered by the folded goldens in tests/test_goldens.py + the frontend
# ts-referencing tests, which is exactly what the FULL gate does for ts too.
BACKEND_STEP_TIERS = ("python", "go", "rust", "wasm", "java")
GATES_ALL = ("conformance", "site-wheel", "ruff", "formal", "docs",
             "vocabulary")

# The documented hard core (the top-level import closure of compile_source): a
# change to any of these is unambiguously a full-gate trigger. `compile_reachable`
# computes the wider lazy-inclusive graph that also fails safe to FULL; this tuple
# is the human-facing name for "core file" in the reason string.
DOCUMENTED_CORE = (
    "parser", "typecheck", "lower", "compiler", "lexer", "errors", "_paths",
    "holes", "admit_profile", "admission", "emission_analysis", "why", "fmt",
)

# Test modules that read a committed `bench/` artifact or pin a bench-side
# constant. A change under `bench/` selects exactly these instead of the FULL
# gate. Kept honest by tests/test_affected_tests.py, which recomputes the set
# from the tree and fails if this tuple has drifted.
BENCH_DEPENDENT_TESTS = (
    # Censuses the whole tree's `.rvl` files to cost issue #1265's
    # refuse-bare-`emission` arm, and most of that census is recorded model
    # output under `bench/results/`, so a bench change must re-run it.
    "tests/test_1265_undeclared_emission_boundary.py",
    # The guard below is itself bench-dependent: it validates this very
    # mapping, so a bench change must re-run it.
    "tests/test_affected_tests.py",
    "tests/test_admission_latency.py",
    "tests/test_demand_ranking.py",
    "tests/test_inprocess_gate.py",
    "tests/test_inprocess_gate_rust.py",
    "tests/test_mcp_edit.py",
    "tests/test_mcp_ship.py",
    "tests/test_rescore_no_self_score.py",
    # Does not READ bench. Its synthetic tree mirrors the real suffix
    # collision between `backends/typescript/runtime.ts` and
    # `bench/codegen/typescript/runtime.ts`, which is the ambiguity the
    # roadmap's abbreviated `typescript/runtime.ts` citations depend on
    # resolving. Declared because the guard below is mention-based, and
    # over-selecting is the safe direction for this gate.
    "tests/test_roadmap_claims_gate.py",
    # The self-host capstone oracle pins four `bench/results/…` candidate
    # documents as members of the emit_java corpus (roadmap item 146 gap 2's
    # located-gap ratchet), so a bench change must re-run it.
    "tests/test_selfhost_compile.py",
    # Drives the codegen-perf harness (bench/codegen/python/run.py) to gate the
    # roadmap-436 / issue-71 python-emitter findings, so a bench change re-runs it.
    "tests/test_71_codegen_perf_findings.py",
    # Reads the rust codegen-perf audit (bench/codegen/rust/) that gates the
    # roadmap-116 rust-emitter findings, so a bench change must re-run it.
    "tests/test_116_rust_perf_accepted.py",
    # These self-host emit oracles read real bench corpus programs
    # (bench/results/... and bench/codegen/...) as additional emit witnesses,
    # so a bench change must re-run them.
    "tests/test_selfhost_emit_java.py",
    "tests/test_selfhost_emit_py.py",
    "tests/test_selfhost_emit_ts.py",
    "tests/test_tokens_to_green.py",
    # Scans EVERY `.rvl` in the repository, `bench/` included — 485 of the 1,035
    # files it reads live there — so a bench change must re-run it. It also
    # names `bench/results/...` explicitly: three model outputs under it do not
    # lex, and they are the only exercise the gate's unlexable-file fallback
    # gets.
    "tests/test_no_embedded_frontend_document_1120.py",
)

# Self-host oracle tests, keyed by the selfhost/<stem>.rvl file they check.
# selfhost/*.rvl is revl SOURCE compiled by the reference compiler INSIDE these
# oracle tests; nothing under src/revl imports it, so — exactly like bench/ and
# formal/ — a change to one self-host file can only break its own oracle set,
# never the frontend pipeline. Before this mapping every self-host edit matched
# no rule and fell to the fail-safe FULL gate (~8 min), which then aborted on the
# pre-existing >120s descent test tests/test_selfhost_lower.py under the hook's
# --timeout, so the commit could not complete at all and the whole class of edits
# had to be landed with --no-verify (issue #431). Mapping each file to its narrow
# oracle set (seconds) gives the inner-loop hook a real local signal again.
#
# DELIBERATELY EXCLUDES tests/test_selfhost_lower.py from the lower.rvl entry: it
# is the slow descent-bound test that made the FULL fallback time out. lower.rvl's
# IR is covered narrowly by tests/test_selfhost_lower_ir.py instead.
#
# tests/test_affected_tests.py recomputes the key set from selfhost/*.rvl on disk
# and checks every mapped test exists, so a new self-host file (or a renamed
# oracle) cannot silently fall back to the unmapped FULL gate this rule replaced.
SELFHOST_ORACLE_TESTS = {
    "checker": ("tests/test_selfhost_checker.py",),
    "compile": ("tests/test_selfhost_compile.py",),
    "emit_go": ("tests/test_selfhost_emit_go.py",),
    "emit_java": ("tests/test_selfhost_emit_java.py",),
    "emit_py": ("tests/test_selfhost_emit_py.py",),
    "emit_rust": ("tests/test_selfhost_emit_rust.py",),
    "emit_ts": ("tests/test_selfhost_emit_ts.py",),
    "emit_wasm": ("tests/test_selfhost_emit_wasm.py",),
    "lexer": ("tests/test_selfhost_lexer.py",),
    # tests/test_oracle_construct_reach.py rides with lower.rvl because the
    # `gate_census` row of tools/oracle_construct_reach.py RUNS `admit_src`
    # over the census corpus and reports the guarantee families no document
    # draws (issue #1215). A refusal this file stops issuing is a construct
    # that becomes unreached, which is the ratchet's RED — and the dependents
    # walk below carries the same selection to lexer/parser/types.rvl, the
    # rest of `admit_src`'s `use` closure.
    "lower": ("tests/test_selfhost_lower_ir.py",
              "tests/test_oracle_construct_reach.py"),
    "parser": ("tests/test_selfhost_parser.py",),
    "types": ("tests/test_selfhost_types.py",),
}

# The item-429 line-coverage gate re-runs on ANY self-host source change (it
# asserts every self-host line stays exercised), so it is added on top of the
# per-file oracle for every selfhost/*.rvl file — including checker.rvl and
# parser.rvl, which issue #431 calls out as needing their oracle + this gate.
#
# The repo-wide embedded-document gate is here for the same reason: it scans
# every `.rvl` in the tree, selfhost/*.rvl included, and those twelve files are
# the largest string-building programs in the repository. Without this line a
# self-host edit selects its oracle and the gate covering it does not run, which
# is a gate that cannot fire for the change most likely to trip it. It costs
# under two seconds.
SELFHOST_ALWAYS = (
    "tests/test_selfhost_line_coverage.py",
    "tests/test_no_embedded_frontend_document_1120.py",
)

# Which self-host emitter port mirrors a backend tier's REFERENCE emitter:
# `backends/<package>/emit.py` is what `selfhost/emit_<stem>.rvl` is held
# byte-identical to by the oracle tests. Keyed by the tier directory name, so
# the mapping is the same one tools/selfhost_line_coverage.py's TIERS table
# states in the other direction.
REFERENCE_EMITTER_ORACLE = {
    "python": "emit_py",
    "typescript": "emit_ts",
    "go": "emit_go",
    "java": "emit_java",
    "rust": "emit_rust",
    "wasm": "emit_wasm",
}

# The self-host oracles that load EVERY tier's reference emitter and compare it
# with its port, so any tier's `backends/*/emit.py` can break them. They name the
# reference only in prose (tools/selfhost_differential_survey.py builds
# `backends/<tier>/emit.py` paths programmatically and the test module never
# spells out a tier name), so the text heuristic in `_tier_tests` cannot see
# them. This is the other half of the #850 hole: that PR changed
# `backends/python/emit.py` alone and the matrix was skipped, but even the
# inner-loop selector would not have selected
# tests/test_selfhost_differential_survey.py.
REFERENCE_EMITTER_ALWAYS = ("tests/test_selfhost_differential_survey.py",)

# Files `tools/evolution_progress.py` reads a counter out of WITHOUT importing
# them, so no import graph reaches them (issue #1224). Each one is a repository
# artifact whose shape the progress counters depend on.
PROGRESS_COUNTER_SOURCES = {
    "tests/test_selfhost_compile.py",   # the LOWER_GAP_DOCS residual table
    "tools/selfhost_coverage.py",       # reference_constructs / TIERS
    "tools/gate_reference_census.py",   # CORPUS_DIRS / _SKIP_DIRS
}

# The held-out scorer's fence (roadmap item 537). Every file it names is read
# by `tests/test_heldout_scoring.py`, which asserts that every repo path those
# files name is classified fence, subject or unreached. That test is what turns
# a new dependency into a red, so it has to be SELECTED when one of them moves.
#
# DERIVED from the tool, by AST rather than by import: a copy here is exactly
# the drift the selection exists to catch. Issue #1307 landed because
# `tools/gate_reference_census.py` began naming `tools/corpus_provenance.py`
# and the tests selected for that file did not include the one that holds the
# classification, so main went red on a path nobody ran.
_FENCE_CACHE: dict[Path, frozenset] = {}


def held_out_fence(root: Path) -> frozenset[str]:
    """`HELD_OUT_FENCE` as written in `tools/heldout_scoring.py`.

    An empty result is returned rather than raised: the caller treats it as "no
    fence file changed", and the tool's own suite holds the parse. A scorer
    file that is renamed away is a rename the generic rules still cover.

    Cached per root, same shape and same reason as `_READ_CACHE` below: this
    runs once per changed file, and `select()` is called a few hundred times in
    a single run of `tests/test_affected_tests.py`.
    """
    if root in _FENCE_CACHE:
        return _FENCE_CACHE[root]
    _FENCE_CACHE[root] = _held_out_fence(root)
    return _FENCE_CACHE[root]


def _held_out_fence(root: Path) -> frozenset[str]:
    source = root / "tools" / "heldout_scoring.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return frozenset()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "HELD_OUT_FENCE" not in names:
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            return frozenset()
        return frozenset(
            e.value for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return frozenset()


# The directories `tools/gate_reference_census.py` walks for `.rvl` documents
# (roadmap item 542, issue #1331). Every document under one of them is a case in
# a scoring corpus, so it needs a line in `tests/fixtures/corpus_provenance.json`
# and reds `tests/test_corpus_provenance.py` without one. Nothing in the import
# graph reaches from a `.rvl` to that test, so the manifest is selected only by
# a rule that names it.
#
# A NEW document reached the manifest from every corpus directory but one, and
# always through the FULL fail-safe (`tests/fixtures/**` and `examples/**` by
# name, `stdlib/`, `selfhost/`, `demo/`, `tck/` and `dogfood/` as unmapped or
# unreferenced paths). `backends/**` is the one: it has its own narrow rule, so
# `backends/go/scenarios/<new>.rvl` selected 243 nodes, none of them the
# manifest, and an undeclared document there reached main green. A REMOVED
# document, which leaves a stale entry the same gate refuses, was narrower
# still: `stdlib/json.rvl` selected 16. Measured on this tree before this rule.
#
# What this does not close, stated so the next reader does not overtrust it:
# issue #1331's own document is not this shape. `tests/fixtures/**` was already
# FULL, and PR #1271's head carried no `tests/test_corpus_provenance.py` at all
# because the manifest landed on main four hours after that branch last took
# main. No selection can run a gate the branch does not have; only a
# merge-queue-style re-run on the merged tree can.
#
# DERIVED from the census by AST rather than restated, for the same reason
# `held_out_fence` is: the census owns which directories it walks, and a copy
# here would be free to go stale against it.
_CORPUS_DIRS_CACHE: dict[Path, tuple] = {}


def census_corpus_dirs(root: Path) -> tuple[str, ...]:
    """`CORPUS_DIRS` as written in `tools/gate_reference_census.py`.

    An EMPTY result means "could not read it", and the caller reads that as
    "every `.rvl` is a corpus document". That is the opposite default from
    `held_out_fence`'s, deliberately: a fence file that cannot be read is still
    covered by the generic rules, while an unreadable corpus list would make a
    scoring document select nothing at all, which is the fail-open direction
    this rule exists to close. Over-selecting costs one 0.3s test module.
    """
    if root in _CORPUS_DIRS_CACHE:
        return _CORPUS_DIRS_CACHE[root]
    _CORPUS_DIRS_CACHE[root] = _census_corpus_dirs(root)
    return _CORPUS_DIRS_CACHE[root]


def _census_corpus_dirs(root: Path) -> tuple[str, ...]:
    source = root / "tools" / "gate_reference_census.py"
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "CORPUS_DIRS" not in names:
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List)):
            return ()
        return tuple(
            e.value for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return ()


def _is_scoring_corpus_document(f: str, root: Path) -> bool:
    """True when this path is an `.rvl` the census walks.

    `EXTRA_DIRS` (`bench/`, `site/`, `docs/`, ...) is NOT read: the census walks
    it only under `--everything`, and `corpus_provenance.enumerate_corpora`
    calls `load_corpus` without it, so a document there is in no scoring corpus
    and needs no manifest line.
    """
    if not f.endswith(".rvl"):
        return False
    dirs = census_corpus_dirs(root)
    if not dirs:
        return True
    return any(f == d or f.startswith(d + "/") for d in dirs)


# Shared test scaffolding whose change can affect the whole suite -> FULL.
_SHARED_TEST_FILES = {
    "tests/conftest.py",
    "tests/_backend_import.py",
    "tests/_net_gate_client.ts",
    "tests/_net_gate_provider.py",
}


def _norm(f: str) -> str:
    return f.strip().replace("\\", "/")


# --------------------------------------------------------------------------- #
# Test-corpus index (pure, deterministic scan of tests/).                      #
# --------------------------------------------------------------------------- #
def _test_files(root: Path):
    d = root / "tests"
    if not d.is_dir():
        return []
    return sorted(d.glob("test_*.py"))


_READ_CACHE: dict[Path, str] = {}


def _read(p: Path) -> str:
    if p not in _READ_CACHE:
        try:
            _READ_CACHE[p] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            _READ_CACHE[p] = ""
    return _READ_CACHE[p]


def _node(p: Path) -> str:
    return f"tests/{p.name}"


# --------------------------------------------------------------------------- #
# Companion files: the repository files a test names outside tests/.           #
# --------------------------------------------------------------------------- #
# Issue #1342. Every text heuristic in this file reads `tests/**` and nothing
# else, so a test whose SUBSTANCE lives in another file is invisible to all of
# them. `tests/test_flagship_demo_525.py` is a thin wrapper that shells out to
# `demo/legacy_enterprise/run_demo.py`; the demo is what runs `revl audit`,
# `revl compile` and the computer-use verb set, and the wrapper names none of
# it. A narrow change to `src/revl/audit.py` therefore selected 133 tests and
# NOT the one test that runs `revl audit` end to end, which is how `main` came
# to ship a demo that exits 1 with every gate green.
#
# That is the same shape as BENCH_DEPENDENT_TESTS, PROGRESS_COUNTER_SOURCES,
# the census/provenance pair and `held_out_fence`: four hand-written tables of
# "this test reads that file", each added after the gap it closes had already
# reached `main`. The rule below is the general form, derived from the tests'
# own source rather than restated here, so the fifth one does not have to be
# noticed first. It is used in BOTH directions:
#
#   forward   a companion's text joins the test's own for the word/tier/stdlib
#             heuristics, so a wrapper inherits the vocabulary of what it runs;
#   reverse   a change to a named file selects every test that names it, which
#             is what the four tables above each do for one path set.
#
# Paths are taken from the test's AST, not from a regex over its prose: a
# `"a/b.py"` literal, and the trailing constant run of a `ROOT / "a" / "b.py"`
# chain, which is how these files spell a repo path. A candidate counts only if
# it exists in the tree and carries a separator; a bare `"src"` or `"demo"` is
# too coarse to mean anything and is dropped. Comments and docstrings are NOT
# a source of paths here: mentioning a file in prose is not reading it.
_COMPANION_SUFFIXES = frozenset({
    ".py", ".rvl", ".sh", ".md", ".json", ".toml", ".yml", ".yaml",
    ".ts", ".mjs", ".go", ".rs", ".java", ".wat", ".ir",
})
# One companion file is read whole. The largest thing a test names today is
# well under this; the cap is here so a future generated artifact cannot make
# the selector quadratic in tree size.
_COMPANION_MAX_BYTES = 512 * 1024


def _path_suffix_segments(node: ast.AST) -> list[str]:
    """The trailing run of string constants in a `x / "a" / "b"` chain.

    `ROOT / "demo" / "legacy_enterprise" / "run_demo.py"` yields the three
    segments; the `ROOT` name at the head is unresolvable and is where the walk
    stops. A plain `"a/b"` constant yields itself.
    """
    out: list[str] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = node.right
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return list(reversed(out))
        out.append(right.value)
        node = node.left
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    return list(reversed(out))


def _named_paths(root: Path, source: str) -> frozenset[str]:
    """Repo-relative paths a python source names and that exist in the tree."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    candidates: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            segs = _path_suffix_segments(node)
            # Every suffix of the chain, because the head may be a `parents[1]`
            # expression OR an already-nested directory constant.
            for i in range(len(segs)):
                candidates.add("/".join(segs[i:]))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "/" in node.value:
                candidates.add(node.value)
    out: set[str] = set()
    for cand in candidates:
        rel = cand.strip("/")
        # A separator is what makes a candidate specific enough to be evidence.
        # `tests/**` is excluded because the sibling-import rule already owns
        # test-to-test edges, and a URL is not a repo path.
        if "/" not in rel or rel.startswith(("http:", "https:", "tests/")):
            continue
        if ".." in rel.split("/"):
            continue
        if (root / rel).exists():
            out.add(rel)
    return frozenset(out)


_COMPANION_CACHE: dict[Path, dict[str, frozenset[str]]] = {}


def companion_paths(root: Path) -> dict[str, frozenset[str]]:
    """test node -> the repo paths outside `tests/` that it names.

    Cached per root, same shape and reason as `_READ_CACHE`: `select()` is
    called a few hundred times in one run of `tests/test_affected_tests.py`.
    """
    cached = _COMPANION_CACHE.get(root)
    if cached is None:
        cached = {}
        for p in _test_files(root):
            named = _named_paths(root, _read(p))
            if named:
                cached[_node(p)] = named
        _COMPANION_CACHE[root] = cached
    return cached


# The one test the reverse rule must not add. `tests/test_selfhost_lower.py` is
# the >120s descent test issue #431 excluded from the `selfhost/lower.rvl`
# selection ON PURPOSE: the FULL fallback aborted on it under the pre-commit
# hook's --timeout, which is what made a whole class of edits unlandable. It
# names `selfhost/lower.rvl`, so the derived rule would put it straight back.
# The exclusion is a COST decision that predates this rule, and the coverage it
# gives up is the coverage #431 already decided to give up; a general rule is
# not a reason to reopen it silently. `tests/test_selfhost_lower_ir.py` holds
# that file's IR narrowly and is selected instead.
REVERSE_RULE_EXCLUDED = ("tests/test_selfhost_lower.py",)


def tests_naming(root: Path, changed: str) -> set[str]:
    """Tests that name `changed`, or a directory containing it."""
    parts = changed.split("/")
    prefixes = {"/".join(parts[:i]) for i in range(2, len(parts) + 1)}
    return {
        node for node, named in companion_paths(root).items()
        if named & prefixes and node not in REVERSE_RULE_EXCLUDED
    }


_COMPANION_TEXT_CACHE: dict[tuple[Path, str], str] = {}


def _companion_text(root: Path, p: Path) -> str:
    """A test's own source, plus the source of every repo file it names.

    This is what the word / tier / stdlib heuristics read, so a test that
    delegates to a script or a fixture program is matched on that file's
    vocabulary as well as its own.
    """
    node = _node(p)
    key = (root, node)
    if key in _COMPANION_TEXT_CACHE:
        return _COMPANION_TEXT_CACHE[key]
    parts: list[str] = [_read(p)]
    for rel in sorted(companion_paths(root).get(node, ())):
        q = root / rel
        if not q.is_file() or q.suffix not in _COMPANION_SUFFIXES:
            continue
        try:
            if q.stat().st_size > _COMPANION_MAX_BYTES:
                continue
        except OSError:
            continue
        parts.append(_read(q))
    text = "\n".join(parts) if len(parts) > 1 else parts[0]
    _COMPANION_TEXT_CACHE[key] = text
    return text


def _tier_tests(root: Path, tier: str) -> set[str]:
    """Frontend tests that reference a backend tier, by filename or content.

    Word-boundary matched so `go` does not match `golden`; over-matching would
    only be safe anyway (a superset), but this keeps the fast set actually fast.
    Content matching (not just filename) catches cross-tier tests such as
    test_conformance_validate.py that hand a tier's emitted code to its compiler.
    """
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(tier)}(?![A-Za-z0-9_])")
    path = re.compile(rf"backends/{re.escape(tier)}\b")
    out: set[str] = set()
    for p in _test_files(root):
        text = _companion_text(root, p)
        if word.search(p.name) or word.search(text) or path.search(text):
            out.add(_node(p))
    return out


def _stdlib_symbols(root: Path, mod: str) -> set[str]:
    """Public symbol names exported by a stdlib module (`pub ... fn/type/... NAME`).

    A test is affected by a change to `stdlib/<mod>.rvl` only if its embedded revl
    source references the module's public API, so these symbol names are the sound
    key to grep for — far tighter than the bare word `json`, which collides with
    the host test's own Python `import json` scaffolding.
    """
    p = root / "stdlib" / f"{mod}.rvl"
    if not p.is_file():
        return set()
    text = p.read_text(encoding="utf-8", errors="replace")
    decl = re.compile(
        r"\bpub\b[^\n]*?\b(?:fn|type|const|let|service|effect|trait)\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)"
    )
    return set(decl.findall(text))


def _stdlib_tests(root: Path, mod: str) -> set[str]:
    """Frontend tests affected by a change to stdlib/<mod>.rvl: those naming its
    public symbols, referencing `stdlib/<mod>`, or the module's own test file."""
    syms = _stdlib_symbols(root, mod)
    sym_re = re.compile(
        r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(s) for s in syms) + r")(?![A-Za-z0-9_])"
    ) if syms else None
    path_re = re.compile(rf"stdlib/{re.escape(mod)}\b")
    out: set[str] = set()
    for p in _test_files(root):
        # The test's OWN text, deliberately not `_companion_text`: this match is
        # on bare stdlib symbol names, which collide freely with words in any
        # `.rvl` a test names, and the sound edge from a self-host file to a
        # stdlib module is the `use` graph the caller already walks. Measured:
        # feeding companions in here made a `stdlib/render.rvl` edit select
        # `tests/test_selfhost_emit_go.py`, whose companion `selfhost/emit_go.rvl`
        # merely spells a render symbol and does not `use` the module.
        text = _read(p)
        if p.name == f"test_{mod}.py" or p.name.startswith(f"test_{mod}_"):
            out.add(_node(p))
        elif path_re.search(text) or (sym_re and sym_re.search(text)):
            out.add(_node(p))
    return out


def _word_tests(root: Path, token: str) -> set[str]:
    """Frontend tests mentioning `token` as a bare word anywhere (name or body).

    Used for stdlib modules and non-core src leaf modules. A deliberate superset:
    any test naming the module or feature is included, which covers both
    `import revl.<mod>` call-sites and CLI-subcommand / feature-name references.
    """
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])")
    out: set[str] = set()
    for p in _test_files(root):
        if word.search(p.name) or word.search(_companion_text(root, p)):
            out.add(_node(p))
    return out


def _test_importers(root: Path, target_stem: str) -> set[str]:
    """Frontend test files that import the test module `target_stem` as a sibling.

    Tests in this repo import ONE ANOTHER: five gate tests
    `import test_selfhost_lower as oracle`, test_274_navigable_slice2 imports
    test_evidence_policy, and eight modules `import test_command`. Editing a
    shared test module can therefore red an importer that never itself changed,
    so a modified test file must also select the tests that import it. Matched
    against the same-directory sibling-import forms these files actually use
    (`import X`, `import X as y`, `from X import ...`), anchored so a prefix
    sibling (`test_command_helpers`) is not a false hit.
    """
    stem = re.escape(target_stem)
    pat = re.compile(
        rf"^[ \t]*(?:from[ \t]+{stem}[ \t]+import[ \t]"
        rf"|import[ \t]+{stem}(?:[ \t]+as[ \t]+\w+)?[ \t]*(?:#.*)?$)",
        re.MULTILINE,
    )
    out: set[str] = set()
    for p in _test_files(root):
        if p.stem == target_stem:
            continue
        if pat.search(_read(p)):
            out.add(_node(p))
    return out


# --------------------------------------------------------------------------- #
# Self-host `use "..."` graph (selfhost/*.rvl compose by textual import).      #
# --------------------------------------------------------------------------- #
_USE_TARGET_RE = re.compile(r'\buse\s+"([^"]+\.rvl)"')


def _selfhost_use_graph(root: Path):
    """Parse the `use "..."` edges of every selfhost/*.rvl file.

    Returns (sh_deps, std_deps):
      sh_deps[stem]  = set of OTHER selfhost stems `stem` directly `use`s
      std_deps[stem] = set of stdlib module stems `stem` directly `use`s

    The self-host compiler is revl source that composes purely by textual
    `use "./x.rvl"` / `use "../stdlib/x.rvl"` imports — there is no package
    resolver — so a lexical scan IS the whole dependency graph.
    """
    d = root / "selfhost"
    sh_deps: dict[str, set[str]] = {}
    std_deps: dict[str, set[str]] = {}
    if not d.is_dir():
        return sh_deps, std_deps
    for p in sorted(d.glob("*.rvl")):
        stem = p.stem
        sh_deps.setdefault(stem, set())
        std_deps.setdefault(stem, set())
        for target in _USE_TARGET_RE.findall(_read(p)):
            norm = target.replace("\\", "/")
            name = Path(norm).stem
            if "/stdlib/" in norm or norm.startswith("stdlib/"):
                std_deps[stem].add(name)
            else:
                sh_deps[stem].add(name)
    return sh_deps, std_deps


def _selfhost_dependents(sh_deps: dict[str, set[str]], stems) -> set[str]:
    """`stems` plus every selfhost file that transitively `use`s one of them.

    A change to lexer.rvl changes what parser.rvl / lower.rvl / checker.rvl
    compile to, and a change to lower.rvl / an emitter changes what compile.rvl
    produces, so those files' oracles are affected too. This is reverse-
    reachability over the `use` graph: the full set of selfhost files whose
    oracle could move when one of `stems` changes.
    """
    rev: dict[str, set[str]] = {}
    for a, ds in sh_deps.items():
        for dep in ds:
            rev.setdefault(dep, set()).add(a)
    seen = set(stems)
    stack = list(stems)
    while stack:
        x = stack.pop()
        for u in rev.get(x, ()):
            if u not in seen:
                seen.add(u)
                stack.append(u)
    return seen


def _selfhost_oracles(root: Path, stems) -> set[str]:
    """Oracle test node-ids for a set of selfhost stems, plus the always-on
    line-coverage gate when any oracle is selected. An unmapped stem contributes
    nothing here; the caller decides whether that escalates to FULL."""
    out: set[str] = set()
    for stem in stems:
        out.update(SELFHOST_ORACLE_TESTS.get(stem, ()))
    if out:
        out.update(SELFHOST_ALWAYS)
    return out


# --------------------------------------------------------------------------- #
# Compile-reachability of src/revl (fail-safe core detection).                 #
# --------------------------------------------------------------------------- #
# One AST walk of `src/revl/**` per tree, not per `select()` call. The selector
# is a pure function of (changed, tree) and every process that uses it is
# short-lived, so re-parsing ~175 modules for each call bought nothing; callers
# that ask the same question many times (tests/test_affected_tests.py,
# tests/test_root_suite_coverage_is_unconditional.py) paid it every time. Same
# shape as `_READ_CACHE` above.
_REACH_CACHE: dict[Path, object] = {}


def compile_reachable(root: Path):
    """Top-level module names reachable from the package entry (`revl/__init__`)
    through ALL imports, lazy/nested included. A change to any of these can run
    during compilation, so it fails safe to the FULL gate. Returns None if the
    tree cannot be analyzed (also -> FULL at the call site)."""
    key = Path(root).resolve()
    if key in _REACH_CACHE:
        cached = _REACH_CACHE[key]
        return None if cached is None else set(cached)
    result = _compile_reachable_uncached(root)
    _REACH_CACHE[key] = None if result is None else frozenset(result)
    return result


def _compile_reachable_uncached(root: Path):
    pkg = root / "src" / "revl"
    if not pkg.is_dir():
        return None
    try:
        mods: dict[str, Path] = {}
        for p in pkg.rglob("*.py"):
            name = ".".join(p.relative_to(pkg).with_suffix("").parts)
            mods[name] = p

        def deps(path: Path) -> set[str]:
            out: set[str] = set()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom):
                    if n.level >= 1 and n.module:
                        out.add(n.module.split(".")[0])
                    elif n.level >= 1:
                        for a in n.names:
                            out.add(a.name.split(".")[0])
                    elif n.module and n.module.startswith("revl."):
                        out.add(n.module.split(".")[1])
            return out

        seen: set[str] = set()
        stack = ["__init__"]
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            p = mods.get(m) or mods.get(m + ".__init__")
            if not p:
                continue
            for d in deps(p):
                if d in seen:
                    continue
                if d in mods or (d + ".__init__") in mods:
                    stack.append(d)
        return {m.split(".")[0] for m in seen}
    except (OSError, SyntaxError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# The selector.                                                                #
# --------------------------------------------------------------------------- #
def _full(reason: str) -> dict:
    return {
        "full": True,
        "reason": reason,
        "pytest": ["tests/"],
        "backends": list(BACKEND_STEP_TIERS),
        "gates": list(GATES_ALL),
    }


def select(changed, root) -> dict:
    """Map a list of changed repo-relative paths to the minimal pre-merge target
    set. Pure and deterministic given (changed, tree). See module docstring for
    the soundness contract."""
    root = Path(root)
    changed = [_norm(f) for f in changed if _norm(f)]
    if not changed:
        return _full("no changed files detected -> full (fail safe)")

    reach = compile_reachable(root)

    pytest_nodes: set[str] = set()
    backends: set[str] = set()
    # `ruff` because lint is cheap. `vocabulary`
    # (`tools/check_vocabulary_mirrors.py`) because it has NO path set to
    # select on: it walks every `.py` under `src/revl` and `tools`, and a
    # mirror is a relation BETWEEN two files, so the file that creates one is
    # routinely neither of the two the ledger will name. Issue #1332 is the
    # measurement: four new mirror classes reached `main` and reddened the
    # required `lint` check, and none of the four introducing commits selected
    # this gate -- including the one that selected the FULL gate, because
    # `tools/pre_merge.sh` did not run the tool in any mode. The honest
    # selector for a whole-tree read is "always", and the cost of always is
    # 1.2s: 1.19s for `--check` over 1585 sites and 0.03s for `--self-test`,
    # measured on this tree against a 15-110s affected run.
    gates: set[str] = {"ruff", "vocabulary"}
    reasons: list[str] = []

    for f in changed:
        # --- the self-evolution progress counters (issue #1224) ------------ #
        # `tools/evolution_progress.py` reads its counters out of artifacts
        # other files own, by AST and by JSON key, and NOT by importing them.
        # So no import graph reaches the dependency: renaming `LOWER_GAP_DOCS`,
        # or changing `reference_constructs`, reds
        # tests/test_evolution_progress.py from a file that never mentions it.
        # First in the loop because every file named here also has its own rule
        # further down that ends in `continue`. The census baseline and the
        # reach ledger need no entry: a non-python `tools/` change and any
        # `tests/fixtures/**` change are already FULL.
        if f in PROGRESS_COUNTER_SOURCES:
            pytest_nodes.add("tests/test_evolution_progress.py")
            reasons.append(f"{f} (self-evolution progress counter input)")

        # --- the held-out scoring fence (issue #1307) ---------------------- #
        # Here for the same reason as the block above: every file named by the
        # fence also has its own rule further down that ends in `continue`.
        if f in held_out_fence(root):
            pytest_nodes.add("tests/test_heldout_scoring.py")
            reasons.append(f"{f} (held-out scoring fence)")

        # --- the corpus provenance manifest (issue #1331) ------------------ #
        # Here for the same reason as the two blocks above: `backends/**` and
        # `stdlib/**` are census corpus directories AND have their own rules
        # further down that end in `continue`. A document that arrives in a
        # scoring corpus must name its generation, and the manifest is the only
        # thing that reads it. Added and deleted are the same rule: a removed
        # document leaves a STALE entry, which the same gate refuses in the
        # other direction.
        if _is_scoring_corpus_document(f, root):
            pytest_nodes.add("tests/test_corpus_provenance.py")
            reasons.append(f"{f} (scoring corpus document -> provenance)")

        # --- a test NAMES this file (issue #1342) -------------------------- #
        # The general form of the three blocks above and of
        # BENCH_DEPENDENT_TESTS: a test that reads or runs a repository file is
        # affected when that file moves, whether or not anyone has written the
        # pair down. Derived from
        # the tests' own source by `companion_paths`, and here rather than in a
        # rule of its own because every path it can name also has a rule further
        # down that ends in `continue`.
        named_by = tests_naming(root, f)
        if named_by:
            pytest_nodes |= named_by
            reasons.append(f"{f} (named by {len(named_by)} test(s))")

        # --- structural: always FULL --------------------------------------- #
        if f == "Makefile":
            return _full("Makefile changed -> full")
        if f == "tools/pre_merge.sh":
            return _full("tools/pre_merge.sh changed -> full")
        if f.startswith(".github/"):
            return _full(f"CI config {f} changed -> full")
        if f in _SHARED_TEST_FILES:
            return _full(f"shared test scaffolding {f} changed -> full")
        if f.startswith("tests/fixtures/"):
            return _full("tests/fixtures/** changed -> full")
        if f.startswith("examples/"):
            return _full(f"reference IR / example {f} changed -> full")

        # --- backends/<tier>/** -------------------------------------------- #
        if f.startswith("backends/"):
            parts = f.split("/")
            tier = parts[1] if len(parts) > 1 else ""
            if tier not in BACKEND_TIERS:
                return _full(f"unknown backend path {f} -> full")
            # The committed playground/site wheel vendors the py tier's
            # TOP-LEVEL modules as `revl/backends/python/<name>.py`
            # (playground/build_wheel.py's SOURCE_TREES), so a change to one of
            # them stales the committed wheel exactly as a src/revl change does.
            # Nothing here selected the gate for it: PR #1092 changed
            # backends/python/revl_fs_workspace.py, passed every check, and left
            # `site wheel drift` red on main across four merges. Deliberately
            # matched to the builder's real glob — top level only, not a
            # recursive walk — so subdirectories the wheel never ships
            # (golden/, tests/) do not drag the gate in. Held to the builder by
            # tests/test_affected_tests.py, which reads build_wheel's own
            # input list rather than restating it.
            vendored = tier == "python" and f.endswith(".py") and len(parts) == 3
            if vendored:
                gates.add("site-wheel")
            pytest_nodes |= _tier_tests(root, tier)
            # `_tier_tests` matches the tier NAME, by filename or by content, in
            # the tests it scans. The oracles that hold a tier's REFERENCE
            # emitter byte-identical to its self-host port do not satisfy that:
            # they reach `backends/<tier>/emit.py` through a path assembled at
            # runtime, or mention it only in prose. Select them by the tier they
            # guard instead of by a word they may not contain (issue #854: PR
            # #850 changed backends/python/emit.py, the port was never made, and
            # tests/test_selfhost_differential_survey.py was not selected).
            oracle = REFERENCE_EMITTER_ORACLE.get(tier)
            if oracle:
                pytest_nodes |= set(SELFHOST_ORACLE_TESTS.get(oracle, ()))
                pytest_nodes |= set(SELFHOST_ALWAYS)
            pytest_nodes |= set(REFERENCE_EMITTER_ALWAYS)
            pytest_nodes.add("tests/test_goldens.py")
            gates.add("conformance")
            if tier in BACKEND_STEP_TIERS:
                backends.add(tier)
            reasons.append(
                f"backends/{tier}/**" + (" (+ site wheel)" if vendored else "")
            )
            continue

        # --- stdlib/<mod>.rvl ---------------------------------------------- #
        if f.startswith("stdlib/") and f.endswith(".rvl"):
            mod = Path(f).stem
            hits = _stdlib_tests(root, mod)
            # A stdlib module is ALSO `use`d by the self-host compiler sources
            # (selfhost/*.rvl are compiled inside the self-host oracle tests);
            # nothing under tests/ names those `use` imports, so scan the graph
            # and pull in the oracle of every selfhost file that reaches this
            # module — directly, or transitively through another selfhost file
            # (e.g. stdlib/value.rvl -> emit_py.rvl -> compile.rvl's oracle).
            # Before this the oracles were missed and a stdlib edit that broke
            # only the self-host emitters shipped green.
            sh_deps, std_deps = _selfhost_use_graph(root)
            users = {s for s, mods in std_deps.items() if mod in mods}
            oracle_hits = _selfhost_oracles(
                root, _selfhost_dependents(sh_deps, users)
            )
            if not hits and not oracle_hits:
                return _full(f"stdlib/{mod}.rvl has no referencing test -> full")
            pytest_nodes |= hits
            pytest_nodes |= oracle_hits
            reasons.append(
                f"stdlib/{mod}"
                + (" (+ self-host oracles)" if oracle_hits else "")
            )
            continue
        if f.startswith("stdlib/"):
            return _full(f"non-module stdlib change {f} -> full")

        # --- selfhost/<stem>.rvl (the self-host compiler, revl source) ----- #
        # Narrow like bench/ and formal/: a self-host file only feeds its own
        # oracle tests, so select those (+ the item-429 line-coverage gate)
        # rather than the FULL gate the hook was timing out on (issue #431).
        if f.startswith("selfhost/") and f.endswith(".rvl"):
            stem = Path(f).stem
            if SELFHOST_ORACLE_TESTS.get(stem) is None:
                return _full(f"selfhost/{stem}.rvl has no oracle mapping -> full")
            # A self-host file is `use`d by other self-host files (checker/parser/
            # lower `use` lexer; compile `use`s lower + the emitters), so a change
            # to it also moves those DEPENDENTS' compiled output — select their
            # oracles too, not just this file's. Reverse-reachability over the
            # `use` graph; before this a lexer.rvl edit ran only the lexer oracle
            # while silently changing parser/lower/checker output.
            sh_deps, _ = _selfhost_use_graph(root)
            dependents = _selfhost_dependents(sh_deps, {stem})
            pytest_nodes |= _selfhost_oracles(root, dependents)
            extra = sorted(dependents - {stem})
            reasons.append(
                f"selfhost/{stem} (self-host oracle"
                + (f" + dependents {' '.join(extra)}" if extra else "")
                + ")"
            )
            continue
        if f.startswith("selfhost/"):
            return _full(f"non-source selfhost change {f} -> full")

        # --- src/revl/** ---------------------------------------------------- #
        if f.startswith("src/revl/") and f.endswith(".py"):
            gates.add("site-wheel")
            if reach is None:
                return _full("cannot analyze src/revl imports -> full")
            top = f[len("src/revl/"):].split("/")[0]
            top = top[:-3] if top.endswith(".py") else top
            if top in reach:
                where = "core" if top in DOCUMENTED_CORE else "compile-reachable"
                return _full(f"src/revl/{top} is {where} -> full")
            hits = _word_tests(root, top)
            if not hits:
                return _full(f"src/revl/{top} (leaf) has no referencing test -> full")
            pytest_nodes |= hits
            reasons.append(f"src/revl/{top} (leaf)")
            continue
        if f.startswith("src/"):
            return _full(f"unmapped source path {f} -> full")

        # --- tools/*.py ----------------------------------------------------- #
        if f == "tools/affected_tests.py":
            pytest_nodes.add("tests/test_affected_tests.py")
            reasons.append("tools/affected_tests.py (selector self-test)")
            continue
        if f in ("tools/conformance.py", "tools/conformance_cert.py"):
            gates.add("conformance")
            pytest_nodes |= {
                _node(p) for p in _test_files(root) if p.name.startswith("test_conformance")
            }
            reasons.append(f"{f}")
            continue
        if f in ("tools/tier_guarantees.py", "tools/check_roadmap_markers.py"):
            # Both feed the GUARANTEE-TIER-MATRIX block in docs/conformance.md:
            # `tier_guarantees.py` generates it, and `check_roadmap_markers.py`
            # supplies the parity records that decide its divergence cells. The
            # generic tools/ rule below matches `test_<stem>.py`, which neither
            # of these has, so without this rule the selector fell all the way
            # through to a FULL run for a file whose covering tests are three
            # named modules. Naming them keeps the gate that checks the block
            # (`conformance --check-readme`) in the selection too.
            gates.add("conformance")
            pytest_nodes.add("tests/test_tier_guarantee_matrix.py")
            pytest_nodes.add("tests/test_roadmap_gate_bites.py")
            pytest_nodes |= {
                _node(p) for p in _test_files(root)
                if p.name.startswith("test_conformance")
            }
            reasons.append(f"{f} (guarantee x tier matrix + roadmap gate)")
            continue
        if f in ("tools/gate_reference_census.py", "tools/corpus_provenance.py"):
            # The two are coupled in both directions (roadmap item 542): the
            # census prints the provenance table, and `corpus_provenance.py`
            # enumerates its scoring corpus with the census's own
            # `load_corpus`, so its case ids are the census's. The generic
            # tools/ rule below matches only `test_<stem>.py`, which would run
            # one side of that coupling and not the other -- and the coupling
            # is where a drift would land, not in either file alone.
            pytest_nodes.add("tests/test_gate_reference_census.py")
            pytest_nodes.add("tests/test_corpus_provenance.py")
            # item 560: the published artifact reads BOTH of these, and its
            # tests hold the committed report against what they now say. A
            # change here that moves the allowance or the NEVER_BASELINED
            # mechanism has to red the artifact's suite, or the published
            # table goes stale silently, which is the whole defect it exists
            # to prevent.
            pytest_nodes.add("tests/test_census_artifact.py")
            reasons.append(f"{f} (census/provenance coupling)")
            if f == "tools/gate_reference_census.py":
                # issue #1215: the `gate_census` row of
                # tools/oracle_construct_reach.py imports this file for its
                # corpus walk and its fast engine, so a change to the census
                # moves what that row measures and what its ledger records.
                # This clause used to be a second `if f == ...` rule further
                # down, which the `continue` above made unreachable -- the
                # census kept selecting the coupling pair and never the
                # construct-reach ledger the rule was added to cover.
                pytest_nodes.add("tests/test_oracle_construct_reach.py")
                reasons.append(
                    "tools/gate_reference_census.py (construct-reach ledger)"
                )
            continue
        if f == "tools/check_site_wheel.py":
            gates.add("site-wheel")
            reasons.append("tools/check_site_wheel.py")
            continue
        if f == "tools/docgen.py":
            gates.add("docs")
            pytest_nodes.add("tests/test_check_vision_claims.py")
            reasons.append("tools/docgen.py")
            continue
        # issue #1204: the vision gate rides in the `docs` gate step, and its
        # `vision-tiers` block lives in docgen, so each file re-runs the other's
        # covering test. Without the gate here the generic tools/*.py rule below
        # would select the pytest module and skip the gate that actually runs
        # against the committed document.
        if f == "tools/check_vision_claims.py":
            gates.add("docs")
            pytest_nodes.add("tests/test_check_vision_claims.py")
            pytest_nodes.add("tests/test_docgen_doc_status_shape.py")
            reasons.append("tools/check_vision_claims.py")
            continue
        if f.startswith("tools/") and f.endswith(".py"):
            stem = Path(f).stem
            hits = {
                _node(p) for p in _test_files(root)
                if p.name == f"test_{stem}.py" or p.name.startswith(f"test_{stem}_")
            }
            if not hits:
                return _full(f"tools/{stem}.py has no covering test -> full")
            pytest_nodes |= hits
            reasons.append(f"tools/{stem}.py")
            continue
        if f.startswith("tools/"):
            return _full(f"non-python tools change {f} -> full")

        # --- formal/harness/** (the differential oracle's own half) --------- #
        # Narrower than the block below, and deliberately: `diff_corpus.py`
        # decides the REFERENCE side of every verdict the Lean oracle is
        # diffed against, and `Oracle.lean` states each judgment a second
        # time. `tests/test_formal_config_data_row.py` holds the two spellings
        # of the config-is-data allowlist to each other and the exporter's
        # type walk to the shipped checker, and needs no toolchain — so a
        # change in here is collected on a machine where the gate skips. It
        # falls through to the formal block, which adds the gate itself.
        if f.startswith("formal/harness/"):
            pytest_nodes.add("tests/test_formal_config_data_row.py")

        # --- formal/** (the Lean backbone) and its own corpus --------------- #
        # A formal/ change affects the formal gate and the pytest modules
        # below, and nothing else (plus lint, which is always run). Deliberately
        # narrower than the fail-safe default so proof-engineering iterations
        # stay fast on the inner loop. Those modules are the python half of
        # `formal/harness/diff_corpus.py` — the rules that decide its `P` and
        # `W` rows, the checker-alignment fidelity of its `U`/`F` rows and
        # checker door (#1169), the namespace the L2 derived layer states its
        # theorems in, and its A9/A2 row modules: halves `make formal` can only
        # collect with a Lean toolchain installed, and which are therefore what
        # can move unnoticed on a machine without one.
        if f.startswith("formal/") or f.startswith("tests/formal_corpus/"):
            gates.add("formal")
            pytest_nodes.add("tests/test_formal_attenuation_namespace.py")
            pytest_nodes.add("tests/test_formal_derived_namespace.py")
            pytest_nodes.add("tests/test_formal_a9_row.py")
            pytest_nodes.add("tests/test_formal_a2_row.py")
            pytest_nodes.add("tests/test_formal_alignment.py")
            # `formal/STATUS.md` is not only prose: `revl.cert` PARSES it for
            # the census the component certificate reports, and the alignment
            # census is generated into it by the harness. Rewriting that
            # section without this node reds `test_826_component_certificate`
            # in CI while the selector says the change was covered (measured
            # on issue #1169, where the rewrite dropped the agree/mismatch
            # clause `cert.oracle_census` reads).
            pytest_nodes.add("tests/test_826_component_certificate.py")
            reasons.append(f"{f} (formal gate)")
            continue

        # --- tests/test_*.py (modified — add/delete handled by caller) ------ #
        if f.startswith("tests/") and Path(f).name.startswith("test_") and f.endswith(".py"):
            pytest_nodes.add(f)
            # Tests here import one another (module docstring), so a modified
            # test file must also run the tests that import it — otherwise an
            # importer that never changed can red on the edit and the narrow
            # selection misses it.
            importers = _test_importers(root, Path(f).stem)
            pytest_nodes |= importers
            # docs/guide-humans.md states this module's test count, generated
            # from its AST, so editing it can stale a doc block (issue #255).
            if f == "tests/test_mcp.py":
                gates.add("docs")
            reasons.append(
                f"{f} (self"
                + (f" + importers {' '.join(sorted(importers))}" if importers else "")
                + ")"
            )
            continue

        # --- generated docs / matrix --------------------------------------- #
        if f.startswith("docs/") or f.endswith(".md"):
            # THREE pre-merge steps a doc can break. First, the compiled
            # snippets: tests/test_doc_examples.py sweeps README.md + docs/**.md
            # and compiles every ```revl block, so any .md edit must re-run it.
            # Before this rule a .md change matched no pytest node and selected
            # ZERO tests, so a rotted doc example landed green. Second, the
            # generated conformance matrix (conformance --check-readme). Third,
            # the source-derived doc blocks (docgen --check, issue #255);
            # DOC-STATUS's inventory is a function of every docs/*.md, so ANY
            # doc edit can stale it.
            gates.add("conformance")
            gates.add("docs")
            pytest_nodes.add("tests/test_doc_examples.py")
            # issue #1204: and the vision gate, which resolves docs/vision.md's
            # commands and re-checks its generated tier block. Its module holds
            # the REAL document against the REAL tree, so a doc edit that moves
            # a cited path has to re-run it.
            pytest_nodes.add("tests/test_check_vision_claims.py")
            # issue #1300: and the self-host residual, for the same reason. The
            # residual figure lived in prose in three documents and disagreed
            # with `LOWER_GAP_DOCS` in all three; it is generated now, and the
            # module below byte-compares the generated blocks and reads every
            # remaining prose figure. `docgen --check` does that too, in the
            # `frontend` job, which a documentation-only diff SKIPS -- so
            # without this line the gate would miss precisely the pull request
            # that moves one of these documents.
            pytest_nodes.add("tests/test_selfhost_residual_is_generated.py")
            reasons.append(f"{f} (doc examples + generated-matrix + docgen check)")
            continue

        # --- bench/ measurement harnesses ---------------------------------- #
        if f.startswith("bench/"):
            # A benchmark harness is measurement, not shipped compiler code:
            # nothing under src/revl imports it, and no CI job runs it. The only
            # things a bench change can break are the tests that read a
            # committed bench artifact or pin a bench constant, which is
            # BENCH_DEPENDENT_TESTS. Before this rule every perf-audit branch
            # fell through to the fail-safe below and ran the FULL gate for a
            # file the compiler cannot even see, which is why perf work kept
            # stalling on whole-suite runs.
            #
            # tests/test_affected_tests.py asserts BENCH_DEPENDENT_TESTS still
            # equals the set of test modules mentioning `bench/`, so a new
            # dependant cannot silently escape this selection.
            for t in BENCH_DEPENDENT_TESTS:
                pytest_nodes.add(t)
            reasons.append(f"{f} (bench harness -> bench-dependent tests)")
            continue

        # --- anything else: fail safe -------------------------------------- #
        return _full(f"unmapped file {f} -> full (fail safe)")

    reason = "; ".join(dict.fromkeys(reasons)) + " -> targeted"
    return {
        "full": False,
        "reason": reason,
        "pytest": sorted(pytest_nodes),
        "backends": sorted(backends),
        "gates": sorted(gates),
    }


# --------------------------------------------------------------------------- #
# Changed-file discovery (git).                                                #
# --------------------------------------------------------------------------- #
def _git(root: Path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=False,
    ).stdout


def _merge_base(root: Path) -> str:
    for ref in ("origin/main", "main"):
        out = _git(root, "merge-base", ref, "HEAD").strip()
        if out:
            return out
    return "HEAD"


def changed_files(root: Path, base: str | None):
    """Union of committed-since-base, staged, unstaged, and untracked changes.

    Returns (names, added, deleted, base). Additions and deletions are kept
    APART, not merged into one set: since issue #162 the two decide different
    things (a deleted test file escalates to FULL, an added one does not), so
    collapsing them would re-create the bug.
    """
    if base is None:
        base = _merge_base(root)
    names: set[str] = set()
    added: set[str] = set()
    deleted: set[str] = set()

    def _record(line: str) -> None:
        parts = line.split("\t")
        if len(parts) < 2:
            return
        status, path = parts[0], parts[-1]
        names.add(path)
        if not status:
            return
        # `R`/`C` name TWO paths; parts[-1] is the destination, which is the
        # one that now exists, so it counts as an addition.
        if status[0] in ("A", "R", "C"):
            added.add(path)
        elif status[0] == "D":
            deleted.add(path)
        if status[0] in ("R", "C") and len(parts) >= 3:
            names.add(parts[1])
            deleted.add(parts[1])

    # committed range (name-status to learn add/delete)
    for line in _git(root, "diff", "--name-status", f"{base}...HEAD").splitlines():
        _record(line)
    # working tree (staged + unstaged) vs HEAD
    for line in _git(root, "diff", "--name-status", "HEAD").splitlines():
        _record(line)
    # untracked (brand-new files)
    for path in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        if path.strip():
            names.add(path.strip())
            added.add(path.strip())

    # A path that is both (a rename's two halves, or a delete then re-add) is
    # treated as deleted: that is the escalating side, so the ambiguity resolves
    # toward running more.
    added -= deleted
    return sorted(names), added, deleted, base


def _is_frontend_test(f: str) -> bool:
    return (f.startswith("tests/") and Path(f).name.startswith("test_")
            and f.endswith(".py"))


def _added_test_imports(root: Path, f: str) -> set[str] | None:
    """Top-level `revl.<mod>` modules an added test file imports.

    Returns None when the file cannot be read or parsed — the caller escalates,
    because "I could not read the new test" is exactly the ambiguity FULL is for.
    Covers `import revl.x`, `from revl.x import ...` and `from revl import x`.
    """
    p = root / f
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                parts = a.name.split(".")
                if parts[0] == "revl" and len(parts) > 1:
                    out.add(parts[1])
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            parts = n.module.split(".")
            if parts[0] != "revl":
                continue
            if len(parts) > 1:
                out.add(parts[1])
            else:
                # `from revl import x` — x is a module only if src/revl has it
                for a in n.names:
                    if (root / "src" / "revl" / f"{a.name}.py").is_file() \
                            or (root / "src" / "revl" / a.name).is_dir():
                        out.add(a.name)
    return out


def _touched_targets(changed) -> tuple[set[str], set[str], set[str]]:
    """(src/revl top-level modules, backend tiers, stdlib modules) the diff touches."""
    mods: set[str] = set()
    tiers: set[str] = set()
    stdlib: set[str] = set()
    for f in map(_norm, changed):
        if f.startswith("src/revl/") and f.endswith(".py"):
            top = f[len("src/revl/"):].split("/")[0]
            mods.add(top[:-3] if top.endswith(".py") else top)
        elif f.startswith("backends/"):
            parts = f.split("/")
            if len(parts) > 1 and parts[1] in BACKEND_TIERS:
                tiers.add(parts[1])
        elif f.startswith("stdlib/") and f.endswith(".rvl"):
            stdlib.add(Path(f).stem)
    return mods, tiers, stdlib


def _test_add_delete_override(changed, added, deleted, root):
    """Decide the selection when the diff adds or deletes a frontend test file.

    Returns a result dict to use INSTEAD of `select(changed, root)`, or None to
    let the normal selection stand.

    DELETE -> FULL. See the module docstring: tests here import one another, so
    removing one can red a survivor and the blast radius is not visible from the
    path alone.

    ADD -> read it (issue #162). The file itself always runs. It escalates to
    FULL only when the selector cannot connect it to anything the same diff
    touched, which is the "a new test could test anything" case the blanket
    escalation was standing in for. Connected means any of:
      * the rest of the diff's own selection already names it (the module /
        tier / stdlib mapping rules scan tests/ by content, so a new test for a
        touched leaf module or tier is picked up there for free); or
      * it imports a `revl.<mod>` the diff touched; or
      * it names a backend tier or stdlib module the diff touched.
    A diff that adds ONLY test files is narrow by definition: nothing else
    changed, so nothing but the new tests can newly fail.
    """
    root = Path(root)
    changed = [_norm(f) for f in changed]
    added = {_norm(f) for f in added}
    deleted = {_norm(f) for f in deleted}

    for f in sorted(deleted):
        if _is_frontend_test(f):
            return _full(f"test file {f} deleted -> full "
                         "(surviving tests may import it)")

    new_tests = sorted(f for f in changed if f in added and _is_frontend_test(f))
    if not new_tests:
        return None

    rest = [f for f in changed if f not in set(new_tests)]
    if not rest:
        return {
            "full": False,
            "reason": ("only new test file(s) added: "
                       + " ".join(new_tests) + " -> targeted"),
            "pytest": new_tests,
            "backends": [],
            "gates": ["ruff"],
        }

    base = select(rest, root)
    if base["full"]:
        return base

    mods, tiers, stdlib_mods = _touched_targets(rest)
    subjects = mods | tiers | stdlib_mods
    for f in new_tests:
        # 1. the rest of the diff's own selection already names it. The tier /
        #    leaf-module / stdlib rules scan tests/ by content, so a new test
        #    for a touched subject is usually picked up here for free.
        if f in base["pytest"]:
            continue
        # 2. it names a touched subject — in its filename (tokenised on the
        #    underscores a test name is built from, so `test_wasm_newthing.py`
        #    yields `wasm`) or in its body (the same word-boundary match
        #    `_word_tests` and `_tier_tests` use).
        if subjects & set(re.split(r"[^A-Za-z0-9]+", Path(f).name)):
            continue
        body = _read(root / f)
        if any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(s)}(?![A-Za-z0-9_])", body)
               for s in subjects):
            continue
        # 3. it IMPORTS a touched `revl.<mod>` without naming it. Reading the
        #    file is the point of issue #162; failing to read it is the
        #    ambiguity FULL exists for.
        imports = _added_test_imports(root, f)
        if imports is None:
            return _full(f"added test file {f} could not be read -> full")
        if imports & mods:
            continue
        return _full(f"added test file {f} maps to no touched module -> full")

    return {
        "full": False,
        "reason": base["reason"] + "; + added test file(s) "
                  + " ".join(new_tests),
        "pytest": sorted(set(base["pytest"]) | set(new_tests)),
        "backends": base["backends"],
        "gates": base["gates"],
    }


# --------------------------------------------------------------------------- #
# CLI.                                                                          #
# --------------------------------------------------------------------------- #
def _emit(result: dict, base: str, fmt: str) -> str:
    lines: list[str] = []
    if fmt in ("human", "both"):
        lines.append(f"# affected-test selector (base {base})")
        lines.append(f"# reason: {result['reason']}")
        if result["full"]:
            lines.append("# selection: FULL GATE (equivalent to make pre-merge)")
        else:
            n = len([x for x in result["pytest"] if x != "tests/"])
            lines.append(f"# frontend pytest node(s): {n or 0}")
            for x in result["pytest"]:
                lines.append(f"#   - {x}")
            lines.append(f"# per-backend suites: {' '.join(result['backends']) or '(none)'}")
            lines.append(f"# gates: {' '.join(result['gates']) or '(none)'}")
        lines.append("# NOTE: --affected is the inner-loop gate; the full "
                     "`make pre-merge` remains the release/CI gate.")
    if fmt in ("machine", "both"):
        lines.append(f"FULL {1 if result['full'] else 0}")
        lines.append(f"REASON {result['reason']}")
        lines.append(f"PYTEST {' '.join(result['pytest'])}")
        lines.append(f"BACKENDS {' '.join(result['backends'])}")
        lines.append(f"GATES {' '.join(result['gates'])}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=None,
                    help="base ref (default: merge-base with origin/main)")
    ap.add_argument("--format", choices=("human", "machine", "both"), default="both")
    ap.add_argument("--root", default=None, help="repo root (default: git toplevel)")
    args = ap.parse_args(argv)

    if args.root:
        root = Path(args.root)
    else:
        top = _git(Path.cwd(), "rev-parse", "--show-toplevel").strip()
        root = Path(top) if top else Path.cwd()

    changed, added, deleted, base = changed_files(root, args.base)
    result = (_test_add_delete_override(changed, added, deleted, root)
              or select(changed, root))
    print(_emit(result, base, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())

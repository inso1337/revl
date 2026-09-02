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
GATES_ALL = ("conformance", "site-wheel", "ruff", "formal", "docs")

# The documented hard core (the top-level import closure of compile_source): a
# change to any of these is unambiguously a full-gate trigger. `compile_reachable`
# computes the wider lazy-inclusive graph that also fails safe to FULL; this tuple
# is the human-facing name for "core file" in the reason string.
DOCUMENTED_CORE = (
    "parser", "typecheck", "lower", "compiler", "lexer", "errors", "_paths",
    "holes", "admit_profile", "admission", "emission_analysis", "why", "fmt",
)

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
        if word.search(p.name) or word.search(_read(p)) or path.search(_read(p)):
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
        if word.search(p.name) or word.search(_read(p)):
            out.add(_node(p))
    return out


# --------------------------------------------------------------------------- #
# Compile-reachability of src/revl (fail-safe core detection).                 #
# --------------------------------------------------------------------------- #
def compile_reachable(root: Path):
    """Top-level module names reachable from the package entry (`revl/__init__`)
    through ALL imports, lazy/nested included. A change to any of these can run
    during compilation, so it fails safe to the FULL gate. Returns None if the
    tree cannot be analyzed (also -> FULL at the call site)."""
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
    gates: set[str] = {"ruff"}  # lint is cheap; always run it
    reasons: list[str] = []

    for f in changed:
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
            pytest_nodes |= _tier_tests(root, tier)
            pytest_nodes.add("tests/test_goldens.py")
            gates.add("conformance")
            if tier in BACKEND_STEP_TIERS:
                backends.add(tier)
            reasons.append(f"backends/{tier}/**")
            continue

        # --- stdlib/<mod>.rvl ---------------------------------------------- #
        if f.startswith("stdlib/") and f.endswith(".rvl"):
            mod = Path(f).stem
            hits = _stdlib_tests(root, mod)
            if not hits:
                return _full(f"stdlib/{mod}.rvl has no referencing test -> full")
            pytest_nodes |= hits
            reasons.append(f"stdlib/{mod}")
            continue
        if f.startswith("stdlib/"):
            return _full(f"non-module stdlib change {f} -> full")

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
        if f == "tools/check_site_wheel.py":
            gates.add("site-wheel")
            reasons.append("tools/check_site_wheel.py")
            continue
        if f == "tools/docgen.py":
            gates.add("docs")
            reasons.append("tools/docgen.py")
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

        # --- formal/** (the Lean backbone) ---------------------------------- #
        # A formal/ change affects nothing else — the formal gate is the only
        # thing it can break (plus lint, which is always run). Deliberately
        # narrower than the fail-safe default so proof-engineering iterations
        # stay fast on the inner loop.
        if f.startswith("formal/"):
            gates.add("formal")
            reasons.append(f"{f} (formal gate)")
            continue

        # --- tests/test_*.py (modified — add/delete handled by caller) ------ #
        if f.startswith("tests/") and Path(f).name.startswith("test_") and f.endswith(".py"):
            pytest_nodes.add(f)
            # docs/guide-humans.md states this module's test count, generated
            # from its AST, so editing it can stale a doc block (issue #255).
            if f == "tests/test_mcp.py":
                gates.add("docs")
            reasons.append(f"{f} (self)")
            continue

        # --- generated docs / matrix --------------------------------------- #
        if f.startswith("docs/") or f.endswith(".md"):
            # Two pre-merge steps a doc can break: the generated conformance
            # matrix (conformance --check-readme) and the source-derived doc
            # blocks (docgen --check, issue #255). DOC-STATUS's inventory is a
            # function of every docs/*.md, so ANY doc edit can stale it.
            gates.add("conformance")
            gates.add("docs")
            reasons.append(f"{f} (generated-matrix + docgen check)")
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

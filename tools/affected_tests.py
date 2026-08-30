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
    shared test helpers/fixtures, the reference IR, or a test file added/deleted)
    -> FULL.

`--affected` is the INNER-LOOP gate only. The full `make pre-merge` stays the
pre-release / CI gate; this never replaces it.

MACHINE OUTPUT (one key per line, for tools/pre_merge.sh to consume):
    FULL 0|1
    REASON <one-line reason>
    PYTEST <space-separated pytest node-ids/paths, or empty>
    BACKENDS <space-separated tiers with a dedicated pre-merge step>
    GATES <space-separated of: conformance site-wheel ruff>
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
GATES_ALL = ("conformance", "site-wheel", "ruff")

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

        # --- tests/test_*.py (modified — add/delete handled by caller) ------ #
        if f.startswith("tests/") and Path(f).name.startswith("test_") and f.endswith(".py"):
            pytest_nodes.add(f)
            reasons.append(f"{f} (self)")
            continue

        # --- generated docs / matrix --------------------------------------- #
        if f.startswith("docs/") or f.endswith(".md"):
            # The only pre-merge step a doc can break is the generated matrix
            # (conformance --check-readme). Nothing else is affected.
            gates.add("conformance")
            reasons.append(f"{f} (generated-matrix check)")
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
    """Union of committed-since-base, staged, unstaged, and untracked changes,
    plus the set added/deleted (so the caller can force FULL on test add/delete)."""
    if base is None:
        base = _merge_base(root)
    names: set[str] = set()
    added_deleted: set[str] = set()

    # committed range (name-status to learn add/delete)
    for line in _git(root, "diff", "--name-status", f"{base}...HEAD").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status, path = parts[0], parts[-1]
            names.add(path)
            if status and status[0] in ("A", "D"):
                added_deleted.add(path)
    # working tree (staged + unstaged) vs HEAD
    for line in _git(root, "diff", "--name-status", "HEAD").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status, path = parts[0], parts[-1]
            names.add(path)
            if status and status[0] in ("A", "D"):
                added_deleted.add(path)
    # untracked (brand-new files)
    for path in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        if path.strip():
            names.add(path.strip())
            added_deleted.add(path.strip())

    return sorted(names), added_deleted, base


def _apply_add_delete_full(changed, added_deleted):
    """A test file added or deleted changes what the suite collects -> FULL."""
    for f in changed:
        f = _norm(f)
        if f in added_deleted and f.startswith("tests/") \
                and Path(f).name.startswith("test_") and f.endswith(".py"):
            return _full(f"test file {f} added/deleted -> full")
    return None


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

    changed, added_deleted, base = changed_files(root, args.base)
    result = _apply_add_delete_full(changed, added_deleted) or select(changed, root)
    print(_emit(result, base, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Static gate: every fail-silent ``revl_*`` runtime seam is defined EXACTLY
ONCE, with the arity its caller expects.

WHY THIS EXISTS (issue #292). Three defects reached ``main`` in one day from
merges git reported as clean -- each green in isolation, broken only in the
union, because no branch's CI ran against the merged result. The merge queue
(the ``merge_group`` trigger in ``.github/workflows/ci.yml``) closes that class
by construction. This gate is the cheap, static half that closes the ONE case
of the three a linter could ever have caught -- and it fails in seconds at lint
time rather than minutes into a merge-queue build.

That case: two branches independently defined ``revl_note_emission_index`` with
INCOMPATIBLE signatures -- ``(component, index)`` on one, ``(index)`` on the
other. The second shadowed the first, and ``replay.py`` would have raised
``TypeError`` on every recorded emission. ``ruff`` already runs ``select=["F"]``,
so F811 catches a redefinition WITHIN a module; this collision was CROSS-module,
resolved by string name through ``getattr`` -- which no linter and no type
checker follows:

    note = getattr(mod, "revl_note_emission_index", None)   # backends/python/replay.py
    reset = getattr(self.runtime, "revl_reset_run_trace_state", None)  # src/revl/run.py
    take = getattr(self.runtime, "revl_take_model_call", None)         # src/revl/run.py

All three are fail-silent: ``getattr(..., name, None)`` then ``if x is not
None``. A rename, a signature change, or a second definition does not raise --
the feature just quietly stops happening. So the seam set is exactly the class
of thing that rots without a raise, and the class worth a declared registry.

The ``getattr``-crossing filter is what makes this near-zero-noise. The naive
"same module-level name in more than one file with a differing signature" gives
168 hits across the tree, almost all legitimate per-file test helpers. Filtering
to names actually read through a ``getattr`` string literal leaves the handful
of real runtime seams.

WHAT IT CHECKS, over ``src/`` and ``backends/``:

  1. DEFINITION. Each registered seam is defined exactly once (a module-level
     ``def``; a module-level assignment counts toward uniqueness but its arity
     cannot be read). Zero definitions, or two, is an error. This is the half
     that catches the issue-#292 collision: the second ``def`` reds the gate.

  2. ARITY. Each seam's single ``def`` accepts the argument count its caller
     uses, declared in REGISTRY below as ``(min, max)`` positional arguments
     (``max=None`` for ``*args``). ``(index)`` against a declared ``(2, 2)``
     is an error -- the incompatible-signature half of the same collision, for
     the case where both defs land in different files so uniqueness still holds
     but the arity does not.

  3. COVERAGE, both directions. The set of ``revl_*`` names read through a
     ``getattr`` string literal must EQUAL the registered set. A new
     fail-silent seam added without registering it is an error (it would rot
     unwatched, which is the whole failure mode); a registered seam that
     nothing reads any more is an error (a stale registry entry, usually a
     rename the read site made and the registry did not).

WHAT IT DOES NOT DO. It does not follow ``getattr`` whose name is a variable
rather than a literal -- there is nothing static to check there. It scopes the
scan to ``src/`` + ``backends/``, as the issue does; a stub a test defines under
those roots is a real second definition and SHOULD red, which is exactly
condition 1.

USAGE

    python3 tools/check_runtime_seams.py
    python3 tools/check_runtime_seams.py --self-test

``--self-test`` runs the gate against the issue-#292 collision itself (a second
definition, and a changed arity) plus an undeclared seam and the clean
baseline, and asserts the gate reds on each. It runs in the ``lint`` job beside
the gate: a checker whose own teeth are never exercised is the same shape of gap
as the one it was written to close. Never add ``|| true``.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
from dataclasses import dataclass, field

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The roots the issue scopes the seam set to. Definitions and reads are counted
# across both.
SCAN_ROOTS = ("src", "backends")


@dataclass(frozen=True)
class Seam:
    """A declared fail-silent runtime seam.

    ``min_args`` / ``max_args`` are the POSITIONAL argument counts the caller
    relies on; ``max_args=None`` means ``*args`` (unbounded). ``read_at`` and
    ``defined_at`` are informational, quoted in messages so a failure points
    straight at the two sites without a grep.
    """

    name: str
    min_args: int
    max_args: int | None
    read_at: str
    defined_at: str


# ---------------------------------------------------------------------------
# THE REGISTRY. One line per fail-silent seam. Adding a seam here is the price
# of adding a `getattr(x, "revl_...", None)` read; the coverage check (both
# directions) is what makes that price mandatory rather than optional.
# ---------------------------------------------------------------------------
REGISTRY: tuple[Seam, ...] = (
    Seam(
        name="revl_note_emission_index",
        min_args=2,
        max_args=2,
        read_at="backends/python/replay.py",
        defined_at="backends/python/runtime.py",
    ),
    Seam(
        name="revl_reset_run_trace_state",
        min_args=0,
        max_args=0,
        read_at="src/revl/run.py",
        defined_at="backends/python/runtime.py",
    ),
    Seam(
        name="revl_take_model_call",
        # crossing has a default, so a bare call and a one-arg call are both
        # valid: (0, 1).
        min_args=0,
        max_args=1,
        read_at="src/revl/run.py",
        defined_at="backends/python/runtime.py",
    ),
)

REGISTERED = {seam.name: seam for seam in REGISTRY}


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        self.errors.append(message)


# ---------------------------------------------------------------------------
# AST scanning
# ---------------------------------------------------------------------------


@dataclass
class Definition:
    name: str
    path: str
    lineno: int
    # (min, max) positional arity, or None when the binding is not a `def` and
    # its arity cannot be read.
    arity: tuple[int, int | None] | None


@dataclass
class Read:
    name: str
    path: str
    lineno: int


def _def_arity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int | None]:
    """(min, max) POSITIONAL arguments a `def` accepts.

    ``min`` is the count without a default; ``max`` is the total positional
    slots, or None when ``*args`` makes it unbounded. Keyword-only arguments
    are not counted: the seams are called positionally, and this is the arity
    the collision was about.
    """
    args = node.args
    positional = args.posonlyargs + args.args
    total = len(positional)
    required = total - len(args.defaults)
    maximum = None if args.vararg is not None else total
    return (required, maximum)


class _SeamVisitor(ast.NodeVisitor):
    """Collect module-level definitions of registered seams and every
    ``getattr(x, "revl_*")`` literal read.

    Definitions inside a ``def``/``async def``/``class`` are LOCAL and not the
    runtime seam, so the walk does not descend into them. Module-level
    conditional definitions (``if TYPE_CHECKING:`` and the like) are still
    reached, because those block statements are descended into.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.definitions: list[Definition] = []
        self.reads: list[Read] = []
        # How many FunctionDef/ClassDef bodies deep the walk currently is. A
        # definition counts as the runtime seam only at depth 0 (module level,
        # including a module-level `if`/`try`/`with`); a method or a local
        # named like a seam is not the seam. Reads are collected at every depth,
        # because a getattr can live inside a function -- and all three real
        # ones do.
        self._scope_depth = 0

    # -- reads: found anywhere, including inside functions --------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value.startswith("revl_")
        ):
            self.reads.append(Read(node.args[1].value, self.path, node.lineno))
        self.generic_visit(node)

    # -- definitions: module-level only --------------------------------------
    def _record_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self._scope_depth == 0 and node.name in REGISTERED:
            self.definitions.append(
                Definition(node.name, self.path, node.lineno, _def_arity(node))
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_def(node)
        self._scope_depth += 1
        self.generic_visit(node)  # descend for reads; nested defs are local
        self._scope_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_def(node)
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # A method named like a seam is not the seam. Still descend for reads.
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1

    def _record_assign_targets(self, targets: list[ast.expr], lineno: int) -> None:
        if self._scope_depth != 0:
            return
        for target in targets:
            if isinstance(target, ast.Name) and target.id in REGISTERED:
                # A binding that is not a `def`: counts toward uniqueness, arity
                # unknown.
                self.definitions.append(Definition(target.id, self.path, lineno, None))

    def visit_Assign(self, node: ast.Assign) -> None:
        self._record_assign_targets(node.targets, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._record_assign_targets([node.target], node.lineno)
        self.generic_visit(node)


def scan_file(path: pathlib.Path) -> tuple[list[Definition], list[Read]]:
    # A definition is the runtime seam only when it is NOT nested in a function
    # or class -- a method or local named like a seam is not the seam. The
    # visitor records a `def` when it reaches it but records definitions from
    # inside a FunctionDef/ClassDef body as none, because it does not re-walk
    # those bodies for definitions (only for reads). So a plain visit yields
    # exactly the module-level definitions, including those in a module-level
    # `if TYPE_CHECKING:` block.
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    rel = path.relative_to(REPO_ROOT).as_posix()
    visitor = _SeamVisitor(rel)
    visitor.visit(tree)
    return visitor.definitions, visitor.reads


def scan(roots: tuple[str, ...]) -> tuple[list[Definition], list[Read]]:
    all_defs: list[Definition] = []
    all_reads: list[Read] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            defs, reads = scan_file(path)
            all_defs.extend(defs)
            all_reads.extend(reads)
    return all_defs, all_reads


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _arity_str(min_args: int, max_args: int | None) -> str:
    if max_args is None:
        return f"{min_args}+"
    if min_args == max_args:
        return str(min_args)
    return f"{min_args}..{max_args}"


def check(defs: list[Definition], reads: list[Read]) -> Report:
    report = Report()

    defs_by_name: dict[str, list[Definition]] = {}
    for d in defs:
        defs_by_name.setdefault(d.name, []).append(d)

    read_names = {r.name for r in reads}

    # 1 + 2: definition count and arity, per registered seam.
    for seam in REGISTRY:
        found = defs_by_name.get(seam.name, [])
        if not found:
            report.error(
                f"seam `{seam.name}` is registered (read at {seam.read_at}) but "
                f"defined NOWHERE under {'/, '.join(SCAN_ROOTS)}/ -- a fail-silent "
                f"read of a name nothing defines never raises; it just stops "
                f"happening. Expected a definition in {seam.defined_at}."
            )
            continue
        if len(found) > 1:
            sites = ", ".join(f"{d.path}:{d.lineno}" for d in found)
            report.error(
                f"seam `{seam.name}` is defined {len(found)} times ({sites}) -- "
                f"exactly the issue-#292 collision. It is read fail-silent via "
                f"getattr at {seam.read_at}, so a second definition shadows the "
                f"first with no error until a call hits the wrong arity."
            )
            # Arity is ambiguous with multiple defs; the count error is the point.
            continue
        only = found[0]
        if only.arity is None:
            report.error(
                f"seam `{seam.name}` is bound by assignment at "
                f"{only.path}:{only.lineno}, not a `def`, so its arity cannot be "
                f"read. Registered seams must be plain `def`s so this gate can "
                f"check the {_arity_str(seam.min_args, seam.max_args)}-argument "
                f"contract its caller at {seam.read_at} relies on."
            )
            continue
        got_min, got_max = only.arity
        # The caller uses between seam.min_args and seam.max_args positional
        # args. The def must accept every count in that range: it may not
        # REQUIRE more than the caller's minimum, and may not ACCEPT fewer than
        # the caller's maximum.
        too_demanding = got_min > seam.min_args
        too_narrow = seam.max_args is not None and (got_max is not None and got_max < seam.max_args)
        if too_demanding or too_narrow:
            report.error(
                f"seam `{seam.name}` at {only.path}:{only.lineno} accepts "
                f"{_arity_str(got_min, got_max)} positional argument(s), but its "
                f"caller at {seam.read_at} uses "
                f"{_arity_str(seam.min_args, seam.max_args)}. A getattr read does "
                f"not check arity; the mismatch surfaces as a TypeError at call "
                f"time, in production, not here. Update the registry only if the "
                f"caller genuinely changed."
            )

    # 3: coverage, both directions.
    for name in sorted(read_names - set(REGISTERED)):
        sites = ", ".join(f"{r.path}:{r.lineno}" for r in reads if r.name == name)
        report.error(
            f"`{name}` is read through a getattr string literal ({sites}) but is "
            f"not in the seam registry. A fail-silent `revl_*` seam that nothing "
            f"asserts is defined-exactly-once is how issue #292 happened. Add a "
            f"Seam(...) line for it in tools/check_runtime_seams.py."
        )
    for name in sorted(set(REGISTERED) - read_names):
        report.error(
            f"seam `{name}` is registered but read NOWHERE via a getattr literal "
            f"under {'/, '.join(SCAN_ROOTS)}/. Either the read site was renamed "
            f"(update the registry) or the entry is stale (remove it). A registry "
            f"that names seams nothing reads teaches the next reader to trust it "
            f"less."
        )

    return report


# ---------------------------------------------------------------------------
# Self-test: the gate, run against the issue-#292 collision itself.
# ---------------------------------------------------------------------------

# One clean definition of every registered seam, plus the three real reads.
# Every self-test case is this baseline PLUS one mutation, so a case proves the
# mutation is what reddens the gate and nothing else.
_CLEAN_RUNTIME = """
def revl_reset_run_trace_state():
    pass

def revl_note_emission_index(component, index):
    pass

def revl_take_model_call(crossing=None):
    pass
"""

_CLEAN_READS = """
def a(mod):
    return getattr(mod, "revl_note_emission_index", None)

def b(runtime):
    return getattr(runtime, "revl_reset_run_trace_state", None)

def c(runtime):
    return getattr(runtime, "revl_take_model_call", None)
"""


def _run_on_sources(sources: dict[str, str]) -> Report:
    defs: list[Definition] = []
    reads: list[Read] = []
    for name, text in sources.items():
        tree = ast.parse(text, filename=name)
        visitor = _SeamVisitor(name)
        visitor.visit(tree)
        defs.extend(visitor.definitions)
        reads.extend(visitor.reads)
    return check(defs, reads)


def self_test() -> int:
    cases: list[tuple[str, dict[str, str], int]] = [
        (
            "the clean baseline is green",
            {"runtime.py": _CLEAN_RUNTIME, "run.py": _CLEAN_READS},
            0,
        ),
        (
            "issue #292: a SECOND definition of a seam, in another file, reds",
            {
                "runtime.py": _CLEAN_RUNTIME,
                "run.py": _CLEAN_READS,
                "other.py": "def revl_note_emission_index(index):\n    pass\n",
            },
            1,
        ),
        (
            "issue #292: the incompatible ARITY `(index)` for a `(2,2)` seam reds",
            {
                "runtime.py": _CLEAN_RUNTIME.replace(
                    "def revl_note_emission_index(component, index):",
                    "def revl_note_emission_index(index):",
                ),
                "run.py": _CLEAN_READS,
            },
            1,
        ),
        (
            "a seam defined nowhere reds",
            {
                "runtime.py": _CLEAN_RUNTIME.replace(
                    "def revl_reset_run_trace_state():\n    pass\n", ""
                ),
                "run.py": _CLEAN_READS,
            },
            1,
        ),
        (
            "an undeclared `revl_*` getattr seam reds (coverage, forward)",
            {
                "runtime.py": _CLEAN_RUNTIME,
                "run.py": _CLEAN_READS
                + '\ndef d(m):\n    return getattr(m, "revl_brand_new_seam", None)\n',
            },
            1,
        ),
        (
            "a registered seam nothing reads reds (coverage, reverse)",
            {
                "runtime.py": _CLEAN_RUNTIME,
                "run.py": _CLEAN_READS.replace(
                    'return getattr(runtime, "revl_take_model_call", None)',
                    "return None",
                ),
            },
            1,
        ),
        (
            "a bare-call site (0 args) against the (0,1) seam stays green",
            {
                "runtime.py": _CLEAN_RUNTIME,
                "run.py": _CLEAN_READS,
            },
            0,
        ),
        (
            "a seam bound by assignment, not a def, reds (arity unreadable)",
            {
                "runtime.py": _CLEAN_RUNTIME.replace(
                    "def revl_take_model_call(crossing=None):\n    pass\n",
                    "revl_take_model_call = _some_alias\n",
                ),
                "run.py": _CLEAN_READS,
            },
            1,
        ),
    ]

    failures = 0
    for title, sources, expected in cases:
        report = _run_on_sources(sources)
        got = len(report.errors)
        ok = got == expected
        print(f"  {'PASS' if ok else 'FAIL'}  {title}")
        if not ok:
            failures += 1
            print(f"        expected {expected} error(s), got {got}")
            for message in report.errors:
                print(f"        got: {message}")
    print()
    if failures:
        print(f"self-test FAILED: {failures} case(s)")
        return 1
    print(f"self-test passed: {len(cases)} cases")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run this gate against the issue-#292 collision it was written for",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    defs, reads = scan(SCAN_ROOTS)
    report = check(defs, reads)

    for message in report.errors:
        print(f"::error::{message}")
        print(f"  {message}")
    if report.errors:
        print()
        print(f"runtime-seam gate FAILED: {len(report.errors)} finding(s).")
        print(
            "Each fail-silent `revl_*` seam must be defined exactly once, with "
            "the arity its getattr caller uses (issue #292)."
        )
        return 1
    print(
        f"runtime-seam gate passed: {len(REGISTRY)} seam(s), each defined once "
        f"with the arity its caller uses."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

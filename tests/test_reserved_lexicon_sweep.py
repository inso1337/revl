"""Issue #320 — A3's reserved lexicon must be a single frontend-owned union, so
every legal revl identifier is safe verbatim on every tier.

Two guarantees are pinned here:

1. The invariant that keeps the fix from double-escaping (the bug PR #374 hit):
   the frontend-owned `_HOST_PREDECLARED` remainder set is DISJOINT from every
   backend's own keyword-rename set. A name a backend already renames must not
   also be pre-escaped in the frontend, or the two passes compose
   (`func` -> `func_` -> `func__`). This test also checks `_ALL_HOST_KEYWORDS`
   (the frontend's mirror of those sets) still matches the backends, so the
   disjointness reasoning stays valid as the backends evolve.

2. The conformance sweep itself: one-name-per-program over the union, in the
   binding positions a name can occupy (parameter, value local), run through the
   real per-tier toolchains via `tools/reserved_lexicon_sweep`. Every union
   member must be safe verbatim in those positions on every AVAILABLE tier.

The function-NAME position for a subset of names (constructor spellings, emitter
helper names, go predeclared functions) is a KNOWN REMAINDER: making a fn *name*
safe verbatim needs the second half of item 320 — the backends dropping their
own keyword rename so the frontend can own naming end to end. Those cells are
listed in `KNOWN_FNNAME_REMAINDER` and asserted to be the ONLY failures, so the
test documents the remainder precisely and fails the day a NEW gap appears.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The cross-tier half drives real toolchains (cargo/javac/wasmtime/go), which is
# minutes, not seconds. It is gated so the default run stays fast; set
# REVL_SWEEP_ALL_TIERS=1 (CI's conformance job) to exercise it. The python half
# below is in-process and always runs.
_CROSS_TIER = pytest.mark.skipif(
    not os.environ.get("REVL_SWEEP_ALL_TIERS"),
    reason="set REVL_SWEEP_ALL_TIERS=1 to sweep the real per-tier toolchains")

import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from revl.lower import (  # noqa: E402
    _ALL_HOST_KEYWORDS,
    _HOST_PREDECLARED,
    _host_keyword,
    _predeclared_mangle,
)
from tools import reserved_lexicon_sweep as S  # noqa: E402


def _emit_module(tier: str):
    spec = importlib.util.spec_from_file_location(
        f"_kw_{tier}_emit", ROOT / "backends" / tier / "emit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The attribute on each backend that holds its keyword-rename set.
_BACKEND_KEYWORD_ATTR = {
    "go": "_GO_KEYWORDS",
    "java": "_JAVA_RESERVED",
    "rust": "_RUST_RESERVED",
    "typescript": "JS_RESERVED",
}


def _backend_keywords() -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    for tier, attr in _BACKEND_KEYWORD_ATTR.items():
        out[tier] = frozenset(getattr(_emit_module(tier), attr))
    return out


def test_predeclared_is_disjoint_from_every_backend_keyword_set():
    """The remainder set must never overlap a backend's keyword rename, or the
    two escaping passes double-escape (issue #320 / PR #374)."""
    for tier, kws in _backend_keywords().items():
        overlap = _HOST_PREDECLARED & kws
        assert not overlap, (
            f"{tier} renames {sorted(overlap)} as keywords AND the frontend "
            f"pre-escapes them — that double-escapes. Remove them from "
            f"_PREDECLARED_CANDIDATES or from the backend keyword set.")


def test_predeclared_is_disjoint_from_python_keywords():
    import keyword as kwmod

    for name in _HOST_PREDECLARED:
        assert not (kwmod.iskeyword(name) or kwmod.issoftkeyword(name)), name


def test_all_host_keywords_mirror_matches_the_backends():
    """`_ALL_HOST_KEYWORDS` is the frontend's copy of the backend keyword sets;
    every backend keyword must be in it so the candidate filter is correct."""
    union = set()
    for kws in _backend_keywords().values():
        union |= kws
    missing = union - set(_ALL_HOST_KEYWORDS)
    assert not missing, (
        f"_ALL_HOST_KEYWORDS is missing {sorted(missing)} — update the mirror "
        f"in src/revl/lower.py so candidate filtering stays correct.")


def test_predeclared_mangle_is_injective_and_pure():
    # ladder-shift: a predeclared name and its `_`-suffixed sibling stay distinct
    for name in list(_HOST_PREDECLARED)[:20]:
        assert _predeclared_mangle(name) == name + "_"
        assert _predeclared_mangle(name + "_") == name + "__"
    # identity on an ordinary name
    assert _predeclared_mangle("customerName") == "customerName"


def test_host_keyword_predicate_covers_python_and_backends():
    assert _host_keyword("class")   # ts/java/rust keyword
    assert _host_keyword("lambda")  # python keyword
    assert not _host_keyword("len")


# ---------------------------------------------------------------- the sweep ---

# fn-NAME position cells that need item 320's second half. The frontend renames
# a colliding user name only in BINDING positions (`_predeclared_mangle` runs on
# params/locals/lets/captures, never on a fn's own name); making a fn *name* safe
# verbatim needs each backend to drop its own keyword/scaffolding rename so the
# frontend can own naming end to end. Until then a fn whose NAME spells a host
# construct the emitter also emits at module scope collides. Everything else
# (every binding position, every other name) must be safe verbatim.
#
# The families here, all module-scope collisions the emitter injects next to the
# user's top-level `function`/`func`/`(func ...)`:
#   * constructor spellings (`Some`/`None`) the emitter constructs directly;
#   * go predeclared/import/builtin names — `int64`/`strconv`/`float64` (already
#     listed) plus `panic` (a Go builtin the runtime helpers call), `testing`
#     (the imported test package, `testing.T`), and `init` (Go's reserved
#     package-init function, which the go backend already emits as `func init()`
#     for WAL setup — two `func init` with a signature is a compile error);
#   * TS globals/imports/helpers the emitted test harness names at module scope —
#     `eval`/`arguments` (illegal as a `function` name in strict-mode modules),
#     the vitest imports `expect`/`it` (`function expect`/`it` redeclares the
#     import), and `revlEq` (a `fn revlEq` whose in-file `test` asserts equality
#     forces the emitter's own `function revlEq` helper — duplicate declaration).
#     `expect`/`it`/`revlEq` are safe in BINDING position (renamed by the
#     frontend, or never referenced from a body); only the fn-NAME spelling is
#     deferred;
#   * wasm scaffolding the module always defines — `alloc`/`memory`/`f64_to_str`
#     — which a same-named exported `(func ...)` redefines.
KNOWN_FNNAME_REMAINDER = {
    ("python", "fnname", "Job"), ("python", "fnname", "Map"),
    ("python", "fnname", "Pool"), ("python", "fnname", "Stream"),
    ("python", "fnname", "Some"), ("python", "fnname", "None"),
    ("go", "fnname", "None"), ("go", "fnname", "Some"),
    ("go", "fnname", "int64"), ("go", "fnname", "strconv"),
    ("go", "fnname", "float64"),
    ("go", "fnname", "init"), ("go", "fnname", "panic"),
    ("go", "fnname", "testing"),
    ("typescript", "fnname", "eval"), ("typescript", "fnname", "expect"),
    ("typescript", "fnname", "it"), ("typescript", "fnname", "revlEq"),
    ("typescript", "fnname", "arguments"),
    ("rust", "fnname", "None"), ("rust", "fnname", "Some"),
    ("wasm", "fnname", "Some"),
    ("wasm", "fnname", "alloc"), ("wasm", "fnname", "f64_to_str"),
    ("wasm", "fnname", "memory"),
}


def test_python_param_and_local_positions_are_safe_verbatim():
    """The reference tier, in-process (fast): every union member is safe as a
    parameter and as a value local. This is the half of #320 this change lands."""
    res = S.sweep(names=S.UNION, positions=("param", "local"))
    fails = {
        (t, pos, name): detail
        for (name, pos, t), (out, detail) in res.items()
        if t == "python" and out == "fail"
    }
    assert not fails, f"python binding-position regressions: {fails}"


@_CROSS_TIER
def test_cross_tier_param_and_local_positions_are_safe_verbatim():
    """Every AVAILABLE tier's real toolchain accepts every union member in a
    binding position. Tiers whose toolchain is absent are skipped, never
    counted as a pass."""
    res = S.sweep(names=S.UNION, positions=("param", "local"))
    fails = {
        (t, pos, name): detail
        for (name, pos, t), (out, detail) in res.items()
        if out == "fail"
    }
    assert not fails, f"binding-position regressions across tiers: {fails}"


@_CROSS_TIER
def test_only_the_known_fnname_cells_remain():
    """The function-NAME position: assert the set of remaining failures is
    EXACTLY the documented remainder, so a new gap (or a fixed one) is caught."""
    res = S.sweep(names=S.UNION, positions=("fnname",))
    fails = {
        (t, pos, name)
        for (name, pos, t), (out, _detail) in res.items()
        if out == "fail"
    }
    # Only compare tiers we could actually check here.
    checkable = {
        t for (name, pos, t), (out, _d) in res.items()
        if out != "unavailable"
    }
    expected = {c for c in KNOWN_FNNAME_REMAINDER if c[0] in checkable}
    new_gaps = fails - expected
    assert not new_gaps, f"NEW fn-name gaps not in the documented remainder: {new_gaps}"

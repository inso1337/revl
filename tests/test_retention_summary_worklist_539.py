"""`ownership.retention_summary` is a monotone least fixpoint (issue #539).

The round-based driver recomputed EVERY body every round and rebuilt the whole
visible summary each time — O(F^2) per round, 50-55% of check+lower on large
inputs (measured 1992ms on `selfhost/lower.rvl`, 553 functions x ~20 rounds).
The worklist driver recomputes a body only when a callee's retention actually
changed and reads a shadow-view of just that body's callee summaries.

A least fixpoint over a finite lattice is independent of evaluation order, so
the worklist answer must be BYTE-IDENTICAL to the round-based one. This test
pins that: a verbatim copy of the original round-based algorithm is the oracle,
and the shipping `retention_summary` must agree with it on every function of a
real corpus (the emit_py fixtures plus the self-host frontend, `lower.rvl`
included — the exact document the regression was measured on). A timing sanity
check confirms the new driver is not slower than the oracle on that document.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.ownership import (  # noqa: E402
    _FRESH,
    _bound_names,
    _summary_walk,
    retention_summary,
)


def _reference_retention_summary(functions):
    """The pre-#539 round-based driver, verbatim, as the equality oracle."""
    bodies: dict = {}
    for fn in functions or []:
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        params = [p.get("name") for p in fn.get("params") or []]
        if isinstance(name, str) and name not in bodies and all(
                isinstance(p, str) for p in params):
            bodies[name] = (fn.get("body"), params)
    summary = {name: (False,) * len(params) for name, (_, params) in bodies.items()}
    shadows = {name: _bound_names(body, {p for p in params})
               for name, (body, params) in bodies.items()}
    for _ in range(sum(len(p) for _, p in bodies.values()) + 2):
        changed = False
        visible = {
            fname: {k: v for k, v in summary.items() if k not in shadowed}
            for fname, shadowed in shadows.items()
        }
        for name, (body, params) in bodies.items():
            state = {param: _FRESH for param in params}
            _summary_walk(body, state, visible[name])
            keeps = tuple(
                held or param not in state
                for held, param in zip(summary[name], params))
            if keeps != summary[name]:
                summary[name] = keeps
                changed = True
        if not changed:
            return summary
    return {name: (True,) * len(params)
            for name, (_, params) in bodies.items()}


CORPUS_DIR = ROOT / "tests" / "fixtures" / "emit_py_corpus"
_FIXTURE_DOCS = sorted(p.name for p in CORPUS_DIR.glob("*.rvl"))

# The self-host frontend: real, large programs — `lower.rvl` is the 553-function
# document the O(F^2) regression was measured on.
_SELFHOST_DOCS = [
    f"selfhost/{name}.rvl"
    for name in ("lower", "checker", "parser", "emit_py", "emit_ts")
    if (ROOT / "selfhost" / f"{name}.rvl").exists()
]


def _functions(doc: str):
    if doc.startswith("selfhost/"):
        return compile_files([str(ROOT / doc)]).get("functions") or []
    return compile_source((CORPUS_DIR / doc).read_text()).get("functions") or []


@pytest.mark.parametrize("doc", _FIXTURE_DOCS + _SELFHOST_DOCS)
def test_worklist_summary_matches_reference(doc):
    """The worklist summary is byte-identical to the round-based oracle."""
    functions = _functions(doc)
    assert retention_summary(functions) == _reference_retention_summary(functions)


def test_lower_rvl_is_the_stressed_document():
    """Guard the corpus: `lower.rvl` is present and is the big one (>500 fns)."""
    assert "selfhost/lower.rvl" in _SELFHOST_DOCS
    assert len(_functions("selfhost/lower.rvl")) > 500


def test_worklist_not_slower_than_reference_on_lower_rvl():
    """The whole point of #539: the worklist is not slower than the round-based
    driver on the document it regressed on. A loose bound (<= oracle) keeps this
    from flaking on a loaded CI box while still failing an accidental revert to
    the quadratic driver, which was ~10x slower."""
    functions = _functions("selfhost/lower.rvl")
    # warm caches / imports so neither run pays a one-off cost
    retention_summary(functions)
    _reference_retention_summary(functions)

    t = time.perf_counter()
    new = retention_summary(functions)
    new_s = time.perf_counter() - t

    t = time.perf_counter()
    ref = _reference_retention_summary(functions)
    ref_s = time.perf_counter() - t

    assert new == ref
    assert new_s <= ref_s

"""Verified state hand-off on hot-swap — the static half (roadmap item 53).

The `code_change` gap: `revl swap` (item 23) drains in-flight calls but tears
the old provider's *state* down LIFO, so a stateful successor starts cold. Item
53 closes it revl-style — an optional `handoff` names the shape of the state a
provider **exports** when replaced and **accepts** when replacing, and the
admission gate proves the two sides are §5-compatible before any swap threads
the value across (docs/state-handoff.md).

This file is the frontend contract — grammar, lowering, and the admission
gate — all checkable without a runtime. The warm-start threading itself runs
on cordis-py in `test_state_handoff_exec.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


def _cache(state_type: str = "Map[Str, Str]", *, version: int = 1,
           name: str = "Cache") -> str:
    return f"""
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
  fn version() -> Int
}}
component {name} provides cache: Store {{
  handoff cache: {state_type}
  let m = effect Map.new() undo m.drop()
  provide cache {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
    fn version() = {version}
  }}
}}
"""


# -- grammar + lowering -------------------------------------------------------

def test_handoff_parses_and_lowers_additively():
    ir = compile_source(_cache(), "c.rvl")
    comp = next(c for c in ir["components"] if c["name"] == "Cache")
    assert comp["handoff"] == {"key": "cache", "type": "Map[Str, Str]"}


def test_handoff_is_absent_when_undeclared():
    src = _cache().replace("  handoff cache: Map[Str, Str]\n", "")
    ir = compile_source(src, "c.rvl")
    comp = next(c for c in ir["components"] if c["name"] == "Cache")
    assert "handoff" not in comp  # additive: stateless IR is byte-identical


def test_handoff_must_target_a_provided_key():
    src = _cache().replace("handoff cache:", "handoff nope:")
    with pytest.raises(RevlError, match="not a declared provision"):
        compile_source(src, "c.rvl")


def test_handoff_must_precede_effects():
    src = _cache().replace(
        "  handoff cache: Map[Str, Str]\n  let m = effect Map.new() undo m.drop()",
        "  let m = effect Map.new() undo m.drop()\n  handoff cache: Map[Str, Str]")
    with pytest.raises(RevlError, match="must precede"):
        compile_source(src, "c.rvl")


def test_at_most_one_handoff_per_component():
    src = _cache().replace(
        "  handoff cache: Map[Str, Str]\n",
        "  handoff cache: Map[Str, Str]\n  handoff cache: Map[Str, Int]\n")
    with pytest.raises(RevlError, match="more than one `handoff`"):
        compile_source(src, "c.rvl")


# -- the admission gate (the §5 relation, pointed at state) -------------------

def test_compatible_successor_is_admitted():
    old = compile_source(_cache(version=1), "c.rvl")
    # same declared shape, changed code — admitted
    compile_source(_cache(version=2), "c.rvl", manifest=old, replacing=("Cache",))


def test_widened_acceptor_is_admitted():
    # predecessor exports a bare `Str`; a successor that accepts the wider
    # `Opt[Str]` still admits everything the predecessor produces (the T ->
    # Opt[T] injection the §5 relation already models)
    old = compile_source(_cache("Str"), "c.rvl")
    compile_source(_cache("Opt[Str]"), "c.rvl", manifest=old, replacing=("Cache",))


def test_narrowed_acceptor_is_refused():
    # the reverse — predecessor exports `Opt[Str]`, successor accepts only
    # `Str` — drops the `None` case, so it is refused
    old = compile_source(_cache("Opt[Str]"), "c.rvl")
    with pytest.raises(RevlError, match="state hand-off on `cache` differs"):
        compile_source(_cache("Str"), "c.rvl", manifest=old, replacing=("Cache",))


def test_incompatible_successor_is_refused_at_admission():
    old = compile_source(_cache("Map[Str, Str]"), "c.rvl")
    with pytest.raises(RevlError, match="state hand-off on `cache` differs"):
        compile_source(_cache("Map[Str, Int]"), "c.rvl",
                       manifest=old, replacing=("Cache",))


def test_refusal_names_both_shapes_and_carries_a_why_trace():
    old = compile_source(_cache("Map[Str, Str]"), "c.rvl")
    try:
        compile_source(_cache("Map[Str, Int]"), "c.rvl",
                       manifest=old, replacing=("Cache",))
        assert False, "expected refusal"
    except RevlError as exc:
        assert "Map[Str, Int]" in str(exc) and "Map[Str, Str]" in str(exc)
        assert exc.why is not None and exc.why.kind == "state-handoff-drift"
        roles = {step.kind for step in exc.why.steps}
        assert {"accept", "export"} <= roles


def test_renamed_successor_correlates_by_provided_key():
    # the successor is a differently-named component that re-provides `cache`;
    # the gate correlates by the *key*, not the component name
    old = compile_source(_cache("Map[Str, Str]", name="Cache"), "c.rvl")
    with pytest.raises(RevlError, match="state hand-off on `cache` differs"):
        compile_source(_cache("Map[Str, Int]", name="CacheV2"), "c.rvl",
                       manifest=old, replacing=("Cache",))


def test_successor_without_handoff_opts_out_no_refusal():
    # a stateless successor of a stateful provider is a valid (lossy) choice —
    # it declares no `handoff`, so the gate does not force the old state on it
    old = compile_source(_cache("Map[Str, Str]"), "c.rvl")
    src = _cache(version=2).replace("  handoff cache: Map[Str, Str]\n", "")
    compile_source(src, "c.rvl", manifest=old, replacing=("Cache",))


def test_cold_key_no_running_export_is_admitted():
    # the running provider declared no hand-off; a successor that *accepts* one
    # simply starts cold — nothing to be incompatible with
    src_old = _cache().replace("  handoff cache: Map[Str, Str]\n", "")
    old = compile_source(src_old, "c.rvl")
    compile_source(_cache("Map[Str, Str]", version=2), "c.rvl",
                   manifest=old, replacing=("Cache",))

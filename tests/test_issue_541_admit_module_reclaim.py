"""#541: the per-generation `sys.modules` reclaim + the reused admit class map.

`_Driver._emit_module` registers a `revl_run_gen{N}` module in `sys.modules` for
every generation it emits (an emitted record dataclass needs the entry at
class-creation time). Nothing ever removed it, so a long session pinned one whole
emitted module per generation for the life of the PROCESS — the leak #541 (P-5)
reports (201 modules / tens of MB after 200 admits). `_Driver._evict_dead_modules`
now reclaims a generation's entry once every component it defined is disposed:
immediately when a swap/reload predecessor is superseded, and at teardown for a
whole composition (admit turns included).

The P-4 half — `_wire_turn` reusing the `ClassMap` it already built for the
pre-plug gates as the live class map instead of deriving a second one — is proven
here by the admit verdicts and the post-admit calls being unchanged across many
additive turns (the reused map must classify the widened surface identically).

The sweep itself is exercised without a runtime (`test_evict_*`); the end-to-end
admit reclaim needs a live cordis composition and skips without it.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the admit reclaim is proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh`",
)


def _gen_modules() -> list[str]:
    return [m for m in sys.modules if m.startswith("revl_run_gen")]


# --------------------------------------------------------------------------- #
# The sweep in isolation — no runtime needed. A stand-in with just the three
# attributes `_evict_dead_modules` reads drives it exactly as `_Driver` does.
# --------------------------------------------------------------------------- #

class _Stub:
    """Minimal stand-in exposing the attributes `_evict_dead_modules` touches."""

    def __init__(self):
        self.fibers: dict = {}
        self._route_disposers: dict = {}
        self._gen_modules: dict = {}


def _evict(stub):
    from revl.run import _Driver
    # a plain method; call it with the stub as `self`.
    _Driver._evict_dead_modules(stub)


def test_evict_reclaims_a_fully_disposed_generation():
    from revl import run  # noqa: PLC0415
    stub = _Stub()
    name = "revl_run_gen_test_541_a"
    sys.modules[name] = run  # any module object; identity is all that matters
    stub._gen_modules[name] = frozenset({"A", "B"})
    try:
        # both components still live -> the module stays.
        stub.fibers = {"A": object(), "B": object()}
        _evict(stub)
        assert name in sys.modules
        assert name in stub._gen_modules

        # one component disposed, one still live -> the module still stays.
        stub.fibers = {"B": object()}
        _evict(stub)
        assert name in sys.modules

        # last component disposed -> the entry is reclaimed.
        stub.fibers = {}
        _evict(stub)
        assert name not in sys.modules
        assert name not in stub._gen_modules
    finally:
        sys.modules.pop(name, None)


def test_evict_counts_a_router_provision_as_live():
    from revl import run  # noqa: PLC0415
    stub = _Stub()
    name = "revl_run_gen_test_541_b"
    sys.modules[name] = run
    stub._gen_modules[name] = frozenset({"R"})
    try:
        # a component realized as a router lives in `_route_disposers`, not
        # `fibers`; the sweep must not evict a module still backing one.
        stub._route_disposers = {"R": [lambda: None]}
        _evict(stub)
        assert name in sys.modules

        stub._route_disposers = {}
        _evict(stub)
        assert name not in sys.modules
    finally:
        sys.modules.pop(name, None)


def test_evict_leaves_a_still_live_sibling_generation_alone():
    from revl import run  # noqa: PLC0415
    stub = _Stub()
    dead, live = "revl_run_gen_test_541_dead", "revl_run_gen_test_541_live"
    sys.modules[dead] = run
    sys.modules[live] = run
    stub._gen_modules = {dead: frozenset({"X"}), live: frozenset({"Y"})}
    try:
        stub.fibers = {"Y": object()}  # X gone, Y still live
        _evict(stub)
        assert dead not in sys.modules      # reclaimed
        assert live in sys.modules          # untouched
        assert set(stub._gen_modules) == {live}
    finally:
        sys.modules.pop(dead, None)
        sys.modules.pop(live, None)


# --------------------------------------------------------------------------- #
# End-to-end: the admit loop bounds `sys.modules` and every verdict is unchanged.
# --------------------------------------------------------------------------- #

_BASE = (
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn shout(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn shout(sink, msg) { emit announce(sink, msg) }\n"
    "  }\n"
    "}\n"
)


def _turn(i: int) -> str:
    return (
        f"service Turn{i} {{ emission fn run(sink: Str) }}\n"
        f"component TurnComp{i} requires ops: Ops provides turn{i}: Turn{i} {{\n"
        f"  provide turn{i} {{\n"
        f'    fn run(sink) {{ emit ops.shout(sink, "from-{i}") }}\n'
        f"  }}\n"
        f"}}\n"
    )


def _base_ir():
    from revl import compile_files
    from revl._paths import stdlib_root
    admit_path = str(stdlib_root() / "admit.rvl")
    base_abs = os.path.abspath("base_541.rvl")
    return compile_files([base_abs, admit_path], sources={base_abs: _BASE})


@needs_cordis
def test_additive_admits_keep_identical_verdicts_and_reclaim_on_unload(tmp_path):
    from revl.mcp.session import Session

    n = 8
    # measured as a delta: other tests in the same process may have left
    # generation modules registered, so the claim is "this session adds none
    # that survive its teardown", not an absolute count.
    baseline = set(_gen_modules())

    session = Session()
    session.load(copy.deepcopy(_base_ir()))

    verdicts = []
    for i in range(n):
        sink = str(tmp_path / f"sink{i}.log")
        verdict = session.admit(_turn(i), granted=["Ops"])
        assert verdict.admitted, verdict.message
        # P-4: the reused class map classifies the widened surface identically,
        # so the turn's own key is provided and callable every time.
        assert f"turn{i}" in verdict.keys
        verdict.handle.call(f"turn{i}", "run", [sink])
        assert Path(sink).read_text().splitlines() == [f"announce:from-{i}"]
        verdicts.append((verdict.admitted, tuple(verdict.keys)))

    # every admit reached the same verdict shape (admitted + its own key).
    assert verdicts == [(True, (f"turn{i}",)) for i in range(n)]
    # the turns are additive, so their generations are all still LIVE here: the
    # base plus one per turn, over and above whatever the process already held.
    assert len(_gen_modules()) >= len(baseline) + 1 + n

    # unload disposes the whole composition; #541: every generation module this
    # session registered is reclaimed rather than pinned in `sys.modules` for the
    # process lifetime. Without the fix, `after` grows past `baseline` by 1 + n.
    session.unload()
    after = set(_gen_modules())
    assert after <= baseline, (
        "generation modules leaked past teardown: "
        + ", ".join(sorted(after - baseline)))

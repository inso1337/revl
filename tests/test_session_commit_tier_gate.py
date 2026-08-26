"""Decision 2 tier gate, wired through the emitters — roadmap item 245, Slice 2.

Slice 1 wrote and tested the guard (`refuse_deferred_on_ownerless_tier`) and its
single canonical diagnostic; those unit tests live in
test_session_commit_frontend.py. This file proves Slice 2's wiring: a CALL to a
`deferred` emission is REFUSED at emit time on each of the five ownerless tiers
(rust, go, java, wasm, typescript), surfaced through that tier's own EmitError,
while a declared-but-never-called deferred extern still emits cleanly (the gate
is call-site keyed). The py tier is the session owner and emits the call fine.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

OWNERLESS_TIERS = ("rust", "go", "java", "wasm", "typescript")

# A `deferred` emission carrying a portable body for every tier, so the ONLY
# thing that can refuse it on a non-py tier is the gate (not the unrelated
# "no @<tier> body" portability refusal).
_SEND = ("extern emission deferred fn send(to: Str) "
         "= @py { return } = @ts { } = @rs { } = @go { } = @java { }\n")

_CALLED = _SEND + (
    "service Ops { emission fn q(to: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn q(to) { emit send(to) } }\n"
    "}\n"
)

_UNCALLED = _SEND + (
    "service Counter { fn bump(n: Int) -> Int }\n"
    "component C provides counter: Counter {\n"
    "  provide counter { fn bump(n) { let step = 1 let next = n + step return next } }\n"
    "}\n"
)


def _emitter(tier: str):
    """Load a backend emitter by path under a unique module name (a bare
    `import emit` collides across backends)."""
    path = ROOT / "backends" / tier / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_gate_{tier}_emit", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tier", OWNERLESS_TIERS)
def test_a_deferred_call_is_refused_with_the_canonical_diagnostic(tier):
    ir = compile_source(_CALLED, "t.rvl")
    module = _emitter(tier)
    with pytest.raises(module.EmitError) as exc:
        module.emit(ir)
    msg = str(exc.value)
    assert "needs a session owner runtime" in msg
    assert f"the {tier} tier" in msg
    assert "python tier only" in msg
    # keyed to the reached component + extern name
    assert "Agent" in msg and "`send`" in msg


@pytest.mark.parametrize("tier", OWNERLESS_TIERS)
def test_a_declared_but_uncalled_deferred_extern_emits_cleanly(tier):
    ir = compile_source(_UNCALLED, "t.rvl")
    # no raise: the gate keys off the call site, not the declaration
    _emitter(tier).emit(ir)


def test_the_py_owner_emits_the_deferred_call():
    # py has the session-owner runtime (the `revl run` / MCP driver), so the same
    # program that the five ownerless tiers refuse emits on py.
    ir = compile_source(_CALLED, "t.rvl")
    _emitter("python").emit(ir)

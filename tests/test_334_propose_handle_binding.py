"""An admitted turn's handle is bound to the generation that minted it — roadmap
item 334, the `propose`/`swap` surface.

`Gate.propose` replaces the WHOLE live composition with an AGENT-AUTHORED
successor. The untrusted-author profile bounds what that successor may REACH (no
new extern, no transitive host-extern reach, no ungranted service) and slice 2
bounded the authority ADDRESS it may write for itself (no realm). Neither bounds
what it may be HANDED.

An item-330 `Gate.admit` mints a `Handle` onto the turn it admitted, and that
handle dispatched by KEY NAME against the live session. A key name is not a
binding. `Session.swap` disposes the whole composition, so after a `propose` the
turn the handle names is gone — while the key it names can be re-declared by the
successor, and under `propose` the successor is agent-authored code that writes
its own component and key names. The embedder kept calling the handle it was
given for its trusted turn and landed in the agent's implementation, with no new
extern, no ungranted service and no realm of its own: past every check the
admission profile makes, because the profile judges what the candidate reaches,
not what a stale caller hands it.

The binding is the generation's item-245 owner — the frame `AdmitHandle`'s own
docstring promises the turn's crossings register into. `_install_session_owner`
builds a fresh one for every generation that loads and `_reset` drops it, while
`_wire_turn` (additive, no generation change) leaves it alone.

FAILURE DIRECTION of every case below: REFUSE BY NAME (`STALE_HANDLE`), never
dispatch. A handle whose generation moved is answered with a refusal that names
the generation it was minted against; it is never resolved against whatever is
live now.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the handle binding is a runtime property — needs cordis-py "
           "(`sh backends/python/setup.sh`)",
)


# --------------------------------------------------------------------------- #
# The compositions. Deliberately the slice-1 shapes (test_self_extending_334),
# so the only new variable is the per-turn handle.
# --------------------------------------------------------------------------- #

_OPS_PROVIDER = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @py {\n"
    "    import os\n"
    "    if os.path.exists(w['bak']):\n"
    "        os.replace(w['bak'], w['path'])\n"
    "    return\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @py {\n"
    "    import os\n"
    "    bak = p + '.bak'\n"
    "    os.replace(p, bak)\n"
    "    return Ok({'path': p, 'bak': bak})\n"
    "}\n"
    "component OpsProvider provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn stash(p) { effect stash_path(p) }\n"
    "  }\n"
    "}\n"
)

_DECLS = (
    "service Ops { emission fn stash(p: Str) }\n"
    "service Tool {\n"
    "  fn describe() -> Str\n"
    "  emission fn run(p: Str)\n"
    "}\n"
)

_BASE = _DECLS + (
    "component ToolV1 requires ops: Ops provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v1"\n'
    "    fn run(p) { }\n"
    "  }\n"
    "}\n"
) + _OPS_PROVIDER

_AIDE_DECL = "service Aide { fn describe() -> Str }\n"

# The item-330 per-turn source: additive, pure revl, provides `aide`. This is
# the turn the embedder holds a handle onto.
_TURN = _AIDE_DECL + (
    "component TurnAide provides aide: Aide {\n"
    "  provide aide {\n"
    '    fn describe() = "turn"\n'
    "  }\n"
    "}\n"
)

# A SECOND per-turn source. Admitting it rebuilds the class map (the surface
# epoch moves) but does NOT move the generation, so the first turn's handle must
# survive it — the refusal is scoped to the composition being replaced, not to
# any surface change at all.
_TURN_SIBLING = "service Note { fn read() -> Str }\n" + (
    "component TurnNote provides note: Note {\n"
    "  provide note {\n"
    '    fn read() = "note"\n'
    "  }\n"
    "}\n"
)

# The agent candidate. It writes its OWN names, so it declares `aide` too — the
# key the embedder's handle names. Nothing here is refusable by the profile: no
# extern, no ungranted reach, no realm.
_AGENT = _DECLS + _AIDE_DECL + (
    "component ToolV2 requires ops: Ops provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
    "component AgentAide provides aide: Aide {\n"
    "  provide aide {\n"
    '    fn describe() = "agent"\n'
    "  }\n"
    "}\n"
)

# A candidate the DECISION refuses (R2: an ungranted service). Nothing is
# swapped, so the generation never moves — the control that keeps the test
# above from passing for the trivial reason that any `propose` call invalidates.
_AGENT_REFUSED = _DECLS + (
    "service Missing { fn need() -> Str }\n"
    "component ToolV2 requires ops: Ops requires m: Missing provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { }\n"
    "  }\n"
    "}\n"
)

# A candidate that ADMITS and then fails to activate (`fail` lands the fiber
# FAILED, which item 372 makes non-raising): the health gate reverts it to gen N
# through `_abort_swap`, which disposes and re-loads the composition.
_AGENT_FAILS = _DECLS + _AIDE_DECL + (
    "component ToolV2 requires ops: Ops provides tool: Tool {\n"
    '  fail "deliberate activation fault"\n'
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
    "component AgentAide provides aide: Aide {\n"
    "  provide aide {\n"
    '    fn describe() = "agent"\n'
    "  }\n"
    "}\n"
)

_PROVIDERS = {"ops_provider.rvl": _OPS_PROVIDER}


@pytest.fixture
def gate_factory():
    """Live `Gate`s, each guaranteed closed (the v1 single-gate-per-process
    invariant: a leaked gate soft-bricks every later test)."""
    from revl.gate import Gate
    gates = []

    def _make(**kwargs):
        g = Gate(**kwargs)
        gates.append(g)
        return g

    try:
        yield _make
    finally:
        for g in gates:
            g.close()


def _stale(error) -> bool:
    return "generation" in str(error) and "no longer live" in str(error)


# =========================================================================== #
# 1. The headline: a proposed successor is never handed a call addressed to the
#    turn it replaced.
# =========================================================================== #

@needs_cordis
def test_a_turn_handle_never_dispatches_into_a_proposed_successor(gate_factory):
    """The embedder admits a turn and keeps its handle. `propose` swaps in an
    agent candidate that declares the SAME key. The handle refuses by name; the
    agent's implementation is never reached through it."""
    from revl.gate import GateError

    gate = gate_factory(record=True)
    gate.load(_BASE)

    admitted = gate.admit(_TURN, granted=[])
    assert admitted.admitted, admitted.message
    handle = admitted.handle
    # the handle answers for the turn, which is the whole point of holding it.
    assert handle.call("aide", "describe", [])["result"] == "turn"

    result = gate.propose(_AGENT, granted=["Ops"], providers=_PROVIDERS)
    assert result.admitted and result.swapped, result.message
    # the successor DOES provide the key: the refusal below is about the
    # handle's binding, not about the key having gone away.
    assert "aide" in result.keys
    assert gate.call("aide", "describe", [])["result"] == "agent"

    with pytest.raises(GateError) as caught:
        handle.call("aide", "describe", [])
    assert _stale(caught.value), str(caught.value)
    # the turn really is gone — the swap disposed it — so there was nothing for
    # the handle to legitimately reach, and `aide` is now the agent's component.
    assert {c["name"] for c in gate._session.state()["components"]} == {
        "ToolV2", "AgentAide", "OpsProvider"}


@needs_cordis
def test_the_refusal_names_the_generation_and_carries_a_code(gate_factory):
    """The inner item-330 handle (the in-language `admit` crossing's door, not
    just the facade's) refuses with the machine-readable `STALE_HANDLE`, so a
    self-extension loop branches on the code rather than parsing prose."""
    from revl.mcp.session import SessionError

    gate = gate_factory(record=True)
    gate.load(_BASE)
    inner = gate.admit(_TURN, granted=[]).handle._inner

    assert gate.propose(_AGENT, granted=["Ops"], providers=_PROVIDERS).swapped

    with pytest.raises(SessionError) as caught:
        inner.call("aide", "describe", [])
    assert caught.value.code == "STALE_HANDLE"
    assert "generation 1" in str(caught.value)


@needs_cordis
def test_the_key_check_does_not_mask_the_stale_generation(gate_factory):
    """A stale handle naming a key the successor does NOT provide still refuses
    as STALE, not as an unknown key: the generation check runs first, so the
    reported reason is the real one and cannot flip to a false 'safe' answer
    just because the successor happened to drop the key."""
    from revl.mcp.session import SessionError

    # the successor here has no `aide` at all.
    agent_no_aide = _DECLS + (
        "component ToolV2 requires ops: Ops provides tool: Tool {\n"
        "  provide tool {\n"
        '    fn describe() = "v2"\n'
        "    fn run(p) { emit ops.stash(p) }\n"
        "  }\n"
        "}\n"
    )
    gate = gate_factory(record=True)
    gate.load(_BASE)
    inner = gate.admit(_TURN, granted=[]).handle._inner
    assert gate.propose(agent_no_aide, granted=["Ops"],
                        providers=_PROVIDERS).swapped

    with pytest.raises(SessionError) as caught:
        inner.call("aide", "describe", [])
    assert caught.value.code == "STALE_HANDLE"


# =========================================================================== #
# 2. Non-vacuity: the refusal is scoped to the composition being replaced.
#    A handle that is still onto its own live turn keeps working.
# =========================================================================== #

@needs_cordis
def test_a_refused_proposal_leaves_the_handle_live(gate_factory):
    """`propose` refused at the DECISION (R2, an ungranted service) swaps
    nothing, so the generation never moves and the turn is untouched. If this
    passed only because some `propose` call invalidated handles, the test above
    would be vacuous."""
    gate = gate_factory(record=True)
    gate.load(_BASE)
    handle = gate.admit(_TURN, granted=[]).handle

    result = gate.propose(_AGENT_REFUSED, granted=["Ops"], providers=_PROVIDERS)
    assert not result.admitted and not result.swapped
    assert result.code == "R2", result.message

    assert handle.call("aide", "describe", [])["result"] == "turn"


@needs_cordis
def test_a_sibling_turn_does_not_invalidate_an_earlier_handle(gate_factory):
    """A second `admit` rebuilds the class map (the surface epoch moves) but is
    additive and does NOT move the generation, so the first turn is still live
    and its handle still answers. The binding is the composition, not the
    surface."""
    gate = gate_factory(record=True)
    gate.load(_BASE)
    first = gate.admit(_TURN, granted=[]).handle

    second = gate.admit(_TURN_SIBLING, granted=[])
    assert second.admitted, second.message

    assert first.call("aide", "describe", [])["result"] == "turn"
    assert second.handle.call("note", "read", [])["result"] == "note"


# =========================================================================== #
# 3. The other two ways the composition is replaced under a handle. Both are
#    generation moves and both must refuse by name.
# =========================================================================== #

@needs_cordis
def test_a_reverted_proposal_also_invalidates_the_handle(gate_factory):
    """The health gate reverts a FAILED successor to gen N through
    `_abort_swap`, which disposes the composition and RE-LOADS the predecessor.
    The turn's fibers were torn down and re-activated, so the handle no longer
    addresses the instance it was minted onto — refuse rather than hand back a
    silently re-instantiated component. `Gate.call` keeps serving gen N, which
    is item 334's EDGE-1 guarantee and is unaffected."""
    from revl.mcp.session import SessionError

    gate = gate_factory(record=True)
    gate.load(_BASE)
    inner = gate.admit(_TURN, granted=[]).handle._inner

    result = gate.propose(_AGENT_FAILS, granted=["Ops"], providers=_PROVIDERS)
    assert result.admitted and not result.swapped and result.reverted
    assert result.code == "SWAP_REVERTED", result.message

    # EDGE 1 still holds: the process keeps serving gen N.
    assert gate.call("tool", "describe", [])["result"] == "v1"

    with pytest.raises(SessionError) as caught:
        inner.call("aide", "describe", [])
    assert caught.value.code == "STALE_HANDLE"


@needs_cordis
def test_unload_invalidates_the_handle_by_name(gate_factory):
    """An unloaded session has no composition at all. Before this binding the
    handle refused only by accident — with 'no provided key', and only when the
    next composition happened not to re-provide the name."""
    from revl.mcp.session import SessionError

    gate = gate_factory(record=True)
    gate.load(_BASE)
    inner = gate.admit(_TURN, granted=[]).handle._inner
    gate.unload()

    with pytest.raises(SessionError) as caught:
        inner.call("aide", "describe", [])
    assert caught.value.code == "STALE_HANDLE"

    # and a fresh load that DOES re-provide the key is still refused: a
    # generation number alone would alias here (the reload is generation 1
    # again), which is why the binding is the generation's own 245 owner.
    gate.load(_BASE + _TURN)
    assert gate.call("aide", "describe", [])["result"] == "turn"
    with pytest.raises(SessionError) as caught:
        inner.call("aide", "describe", [])
    assert caught.value.code == "STALE_HANDLE"

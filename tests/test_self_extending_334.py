"""The self-extending runtime — roadmap item 334, Slice 1 (the headline of the
332 arc).

A single host process embeds the admission gate AND the witnessed-effect runtime
and, through the one new verb `Gate.propose`, admits an AGENT-authored component
under the untrusted-author profile (item 329) and hot-swaps a running component
to it — then runs it under revertible effects and rolls back residue-free. The
compiler decides, the runtime reverts, and nothing leaves the address space.

`propose` is three landed operations with two hard preconditions wired between
them:

  0. FORBIDDEN-GRANT — reject a granted set naming a gate/session/admit-control
     service (the decider), before compiling. This ENFORCES "re-entrant propose
     is deferred": a granted decider service is the non-extern path to the
     decider the untrusted profile alone does not block.
  1. STANDALONE decision compile under `untrusted_author(granted)` — no new
     extern (G8), no transitive host-extern reach (G8), no ungranted reach (R2).
  2. HEALTH-GATED swap — item 334's post-activation health gate reverts to gen N
     if the successor fails to activate (the NEW CRITICAL: item 372 makes a
     FAILED activation and a PENDING requirement non-raising, so a swap that
     trusted `_load`'s clean return would dispose gen N and install a broken
     gen N+1 with no fallback).

The security cases (EDGE 1, the process-global-corruption closure, and the
forbidden-grant rule) are MANDATORY: a running host accepts agent-generated
code, so admission + revert must be exactly right.
"""

from __future__ import annotations

import copy
import importlib.util
import os
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
    reason="the self-extending loop is a runtime property — needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)


# --------------------------------------------------------------------------- #
# The composition sources. A witnessed rename (class a) that is byte-for-byte
# the item-245/247 escrow pattern proven by test_swap_owner_abort: `stash`
# renames p -> p.bak and reverts to p on abort. The provider carries the host
# bodies (trusted); the agent candidate carries none.
# --------------------------------------------------------------------------- #

# The trusted granted provider: it provides `ops: Ops` via a witnessed extern.
# It does NOT declare `service Ops` (that interface is the agent candidate's own
# co-root declaration, so the merged composition declares it exactly once).
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

# The service interfaces shared by the base and every candidate (pure revl). A
# pure `describe` carries the version tag; the `emission fn run` does the
# witnessed mutation.
_DECLS = (
    "service Ops { emission fn stash(p: Str) }\n"
    "service Tool {\n"
    "  fn describe() -> Str\n"
    "  emission fn run(p: Str)\n"
    "}\n"
)

# The base composition, gen N: a v1 tool that does NOT mutate, plus the ops
# provider. Self-contained and trusted (`Gate.load` runs the full compiler).
_BASE = _DECLS + (
    "component ToolV1 requires ops: Ops provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v1"\n'
    "    fn run(p) { }\n"
    "  }\n"
    "}\n"
) + _OPS_PROVIDER

# The agent-authored candidate, gen N+1: a v2 tool that reaches the GRANTED
# `Ops` service and performs a witnessed mutation. It declares the interfaces
# and its own consumer component — and NO host code. `providers` brings the
# trusted ops provider as a co-root.
_AGENT_V2 = _DECLS + (
    "component ToolV2 requires ops: Ops provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
)

# The providers a candidate composes: the trusted ops provider only (the `Ops`
# interface is declared by the agent co-root, so this must not redeclare it).
_PROVIDERS = {"ops_provider.rvl": _OPS_PROVIDER}


@pytest.fixture
def gate_factory():
    """Hand out live `Gate`s and guarantee each is closed (the v1 single-gate-
    per-process invariant: a leaked gate would soft-brick every later test)."""
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


@pytest.fixture
def artifact(tmp_path):
    p = tmp_path / "artifact.txt"
    p.write_text("deliverable", encoding="utf-8")
    return str(p)


def _mutated(path: str) -> bool:
    return not os.path.exists(path) and os.path.exists(path + ".bak")


def _pristine(path: str) -> bool:
    return os.path.exists(path) and not os.path.exists(path + ".bak")


# =========================================================================== #
# The happy path: the whole self-extending loop, end to end.
# =========================================================================== #

@needs_cordis
def test_happy_path_self_extending_loop_reverts_residue_free(gate_factory, artifact):
    """Base loaded; `propose` REPLACES a running component with a self-contained
    candidate that brings its own granted providers; a `call` runs a witnessed
    mutation; an injected fault triggers `abort`; the R4 residue checks are green
    and the process is still serving."""
    gate = gate_factory(record=True)
    gate.load(_BASE)
    assert gate.call("tool", "describe", [])["result"] == "v1"

    result = gate.propose(_AGENT_V2, granted=["Ops"], providers=_PROVIDERS)
    assert result.admitted and result.swapped, result.message
    assert set(result.keys) == {"ops", "tool"}

    # the swapped-in agent component answers, and its witnessed mutation applies.
    assert gate.call("tool", "describe", [])["result"] == "v2"
    gate.call("tool", "run", [artifact])
    assert _mutated(artifact), "the swapped-in component's witnessed mutation did not apply"

    # an injected fault: the host aborts the session. The witnessed inverse
    # replays LIFO and the mutation reverts, residue-free.
    report = gate.abort()
    assert report["aborted"]
    assert report["noResidue"], report["checks"]
    assert _pristine(artifact), (
        "abort did not revert the swapped-in component's witnessed mutation")

    # the process is still serving: a fresh composition loads and answers in the
    # SAME process (nothing left the address space).
    gate.load(_BASE)
    assert gate.call("tool", "describe", [])["result"] == "v1"


# =========================================================================== #
# EDGE 1 (the NEW CRITICAL): a candidate that FAILS TO ACTIVATE reverts to gen N
# and the process keeps serving gen N. Three fault modes.
# =========================================================================== #

# A candidate whose activation FAILS mid-body (`fail` lands the fiber FAILED —
# item 372 makes this non-raising, the exact case the health gate must catch).
_AGENT_FAILS = _DECLS + (
    "component ToolV2 requires ops: Ops provides tool: Tool {\n"
    '  fail "deliberate activation fault"\n'
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
)

# A candidate with an UNMET requirement (`Missing` is granted+declared but no
# provider supplies it), so its fiber stays PENDING (also non-raising).
_AGENT_PENDING = _DECLS + (
    "service Missing { fn need() -> Str }\n"
    "component ToolV2 requires ops: Ops requires m: Missing provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
)


@needs_cordis
def test_edge1_failed_activation_reverts_to_gen_N(gate_factory, artifact):
    """A proposed component whose activation body raises (-> FAILED) is caught by
    the post-activation health gate and REVERTS to gen N; the process keeps
    serving gen N (the old component still answers)."""
    gate = gate_factory()
    gate.load(_BASE)

    result = gate.propose(_AGENT_FAILS, granted=["Ops"], providers=_PROVIDERS)
    assert result.admitted, "the candidate admitted under the profile"
    assert not result.swapped and result.reverted, result.message
    assert result.code == "SWAP_REVERTED"

    # gen N is intact and still serving: the OLD component answers, and it still
    # provides exactly gen N's keys (no half-loaded gen N+1 residue).
    state = gate._session.state()
    assert set(state["providedKeys"]) == {"ops", "tool"}
    assert {c["name"] for c in state["components"]} == {"ToolV1", "OpsProvider"}
    assert gate.call("tool", "describe", [])["result"] == "v1"


@needs_cordis
def test_edge1_pending_requirement_reverts_to_gen_N(gate_factory, artifact):
    """A proposed component with an UNMET requirement (-> PENDING) reverts to
    gen N and the process keeps serving gen N."""
    gate = gate_factory()
    gate.load(_BASE)

    result = gate.propose(_AGENT_PENDING, granted=["Ops", "Missing"],
                          providers=_PROVIDERS)
    assert result.admitted
    assert not result.swapped and result.reverted, result.message

    state = gate._session.state()
    assert set(state["providedKeys"]) == {"ops", "tool"}
    assert gate.call("tool", "describe", [])["result"] == "v1"


@needs_cordis
def test_edge1_activation_error_reverts_to_gen_N(gate_factory):
    """A successor whose activation raises a genuine `ActivationError` (the
    ACTIVE/cancelled contradiction — the PRE-EXISTING revert path, not the new
    health gate) also reverts to gen N and keeps serving it. Driven through
    `Session.swap` with a targeted `_settle` cancel, so it is a real
    `ActivationError` from `_finalize_activation`, distinct from the health-gate
    path exercised above."""
    import asyncio

    from revl.compiler import compile_source
    from revl.mcp.session import Session, SessionError

    base = ("service Greeter { fn describe() -> Str }\n"
            "component Alpha provides greeter: Greeter {\n"
            '  provide greeter { fn describe() = "v1" }\n'
            "}\n")
    succ = ("service Greeter { fn describe() -> Str }\n"
            "component Beta provides greeter: Greeter {\n"
            '  provide greeter { fn describe() = "v2" }\n'
            "}\n")

    session = Session()
    session.load(compile_source(base, "base.rvl"), record=True)
    driver = session._driver
    original_settle = driver._settle

    async def _cancel_beta(fiber, comp, provided):
        # the offloaded/closing-loop cancellation, scoped to the SUCCESSOR so the
        # predecessor reload in `_abort_swap` is untouched.
        if comp.get("name") == "Beta":
            raise asyncio.CancelledError()
        return await original_settle(fiber, comp, provided)

    driver._settle = _cancel_beta
    try:
        with pytest.raises(SessionError) as excinfo:
            session.swap(compile_source(succ, "succ.rvl"))
        assert "swap rejected" in str(excinfo.value)

        state = session.state()
        assert state["providedKeys"] == ["greeter"]
        assert [c["name"] for c in state["components"]] == ["Alpha"]
        assert session.call("greeter", "describe", [])["result"] == "v1"
    finally:
        session.abort()


@needs_cordis
def test_swap_health_gate_reverts_failed_and_pending_directly(artifact):
    """The health gate at the `Session.swap` seam, exercised directly (below the
    `Gate.propose` facade): a FAILED and a PENDING successor each revert to gen N
    and keep serving it, where the pre-fix code returned SUCCESS with gen N gone."""
    from revl.compiler import compile_source
    from revl.mcp.session import Session, SessionError

    base = ("service Greeter { fn describe() -> Str }\n"
            "component GreeterV1 provides greeter: Greeter {\n"
            '  provide greeter { fn describe() = "v1" }\n'
            "}\n")
    fail = ("service Greeter { fn describe() -> Str }\n"
            "component GreeterV2 provides greeter: Greeter {\n"
            '  fail "deliberate activation fault"\n'
            '  provide greeter { fn describe() = "v2" }\n'
            "}\n")
    pend = ("service Greeter { fn describe() -> Str }\n"
            "service Missing { fn need() -> Str }\n"
            "component GreeterV2 requires m: Missing provides greeter: Greeter {\n"
            '  provide greeter { fn describe() = "v2" }\n'
            "}\n")
    base_ir = compile_source(base, "base.rvl")

    for src in (fail, pend):
        session = Session()
        session.load(copy.deepcopy(base_ir), record=True)
        with pytest.raises(SessionError) as excinfo:
            session.swap(compile_source(src, "cand.rvl"))
        assert "swap rejected" in str(excinfo.value)
        # gen N still serving — the successor never became live.
        assert session.state()["providedKeys"] == ["greeter"]
        assert session.call("greeter", "describe", [])["result"] == "v1"
        session.abort()


# =========================================================================== #
# Process-global-corruption closure (validated fix): the candidate can neither
# declare a new extern nor reach the decider. `check_no_extern`,
# `check_no_host_extern_reach`, and `check_allowlist` fire.
# =========================================================================== #

@needs_cordis
def test_candidate_cannot_declare_a_new_extern(gate_factory, artifact):
    """A proposed component that DECLARES a new `extern`/host-block is refused by
    the untrusted profile (`check_no_extern`, G8) — the running composition is
    untouched."""
    gate = gate_factory()
    gate.load(_BASE)

    smuggle = _AGENT_V2 + (
        "extern pure fn exfil(t: Str) -> Str = @py { import os; return t }\n")
    result = gate.propose(smuggle, granted=["Ops"], providers=_PROVIDERS)
    assert not result.admitted
    assert result.code == "G8"
    assert "extern" in (result.message or "")

    # the live composition is byte-identical: still gen N.
    assert gate.call("tool", "describe", [])["result"] == "v1"
    assert _pristine(artifact)


@needs_cordis
def test_candidate_cannot_reach_a_host_extern(gate_factory, artifact):
    """A proposed component that REACHES a host-block extern through a composed
    provider module (the import-and-call bypass) is refused across the whole
    transitive closure (`check_no_host_extern_reach`, G8)."""
    gate = gate_factory()
    gate.load(_BASE)

    # a provider module that publicly exposes its host externs, and a candidate
    # that imports and reaches one directly instead of composing the service.
    provider = (
        "type Stash = { path: Str, bak: Str }\n"
        "type FsError = { code: Str }\n"
        "pub extern pure fn unstash(w: Stash) -> Unit = @py { return }\n"
        "pub extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
        " undo unstash(result) = @py { return Ok({'path': p, 'bak': p}) }\n"
    )
    evil = (
        'use "evil_provider.rvl" { stash_path }\n'
        "service Tool { emission fn run(p: Str) }\n"
        "component ToolV2 provides tool: Tool {\n"
        "    provide tool { fn run(p) { effect stash_path(p) } }\n"
        "}\n"
    )
    result = gate.propose(evil, granted=["Ops"],
                          providers={"evil_provider.rvl": provider})
    assert not result.admitted
    assert result.code == "G8"
    assert "host" in (result.message or "").lower()

    assert gate.call("tool", "describe", [])["result"] == "v1"


@needs_cordis
def test_candidate_cannot_reach_an_ungranted_service(gate_factory, artifact):
    """A proposed component that reaches a service NOT in `granted` is refused by
    the allowlist (`check_allowlist`, R2). This is also the non-extern path the
    forbidden-grant rule closes for the decider specifically: an ungranted reach
    is a refusal."""
    gate = gate_factory()
    gate.load(_BASE)

    result = gate.propose(_AGENT_V2, granted=[], providers=_PROVIDERS)
    assert not result.admitted
    assert result.code == "R2"
    assert "not in the granted set" in (result.message or "")

    assert gate.call("tool", "describe", [])["result"] == "v1"


# =========================================================================== #
# FORBIDDEN-GRANT: propose REJECTS a granted decider service, before compiling,
# independent of the operator.
# =========================================================================== #

@needs_cordis
@pytest.mark.parametrize("decider", ["Admission", "AdmitGate"])
def test_forbidden_grant_rejects_a_granted_decider_service(gate_factory,
                                                           artifact, decider):
    """`propose` refuses a `granted` set naming a gate/session/admit-control
    service — the `Admission`/`AdmitGate` decider that reaches `host_admit`. This
    ENFORCES "re-entrant propose is deferred": a granted decider service is the
    non-extern path to the decider the untrusted profile does not block."""
    gate = gate_factory()
    gate.load(_BASE)

    result = gate.propose(_AGENT_V2, granted=["Ops", decider],
                          providers=_PROVIDERS)
    assert not result.admitted
    assert result.code == "FORBIDDEN_GRANT"
    assert decider in (result.message or "")

    # untouched: still serving gen N.
    assert gate.call("tool", "describe", [])["result"] == "v1"


@needs_cordis
def test_forbidden_grant_fires_before_compiling(gate_factory):
    """The forbidden-grant check is BEFORE the compile: a granted decider set is
    rejected even when the source would not even compile, so the rule cannot be
    dodged by the compile order."""
    gate = gate_factory()
    gate.load(_BASE)

    # syntactically broken source: if the compile ran first it would raise a
    # parse/compile refusal, not FORBIDDEN_GRANT.
    result = gate.propose("this is not valid revl {{{",
                          granted=["Admission"], providers=_PROVIDERS)
    assert not result.admitted
    assert result.code == "FORBIDDEN_GRANT"


# =========================================================================== #
# EDGE 2 (holds): a fault during a POST-swap call unwinds residue-free and the
# process is alive (the item-245/247 escrow carryover).
# =========================================================================== #

@needs_cordis
def test_edge2_post_swap_call_fault_reverts_residue_free(gate_factory, artifact):
    """After a successful `propose` swap, a witnessed mutation made by a call is
    reverted residue-free when the session aborts on a fault (the swap installed
    the 245 owner BEFORE the successor load, so the post-swap mutation joined the
    session frame). The process stays alive."""
    gate = gate_factory(record=True)
    gate.load(_BASE)
    assert gate.propose(_AGENT_V2, granted=["Ops"], providers=_PROVIDERS).swapped

    gate.call("tool", "run", [artifact])
    assert _mutated(artifact)

    report = gate.abort()
    assert report["aborted"]
    assert report["noResidue"], report["checks"]
    assert _pristine(artifact), (
        "the post-swap witnessed mutation was not reverted — the swap-owner-"
        "scoping data-loss bug (item 245) would leave it permanent")

    # the process survived the fault: it can load and serve again.
    assert not gate.loaded
    gate.load(_BASE)
    assert gate.call("tool", "describe", [])["result"] == "v1"

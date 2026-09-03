"""The proposal's authority envelope — roadmap item 334, Slice 2.

Slice 1 landed the loop: `Gate.propose` compiles an agent-authored candidate
under the untrusted-author profile and hot-swaps the running composition to it,
with a post-activation health gate that reverts to gen N. This slice bounds what
that proposal is ALLOWED TO CHANGE, in the two directions slice 1 left open.

**1. A proposal may not choose its own authority address (the `G9` realm
refusal).** The item-246/251 approval policy scopes a standing approval and an
auto-approve rule by `(component glob, realm)` and matches the realm half
against `ticket["realm"]`, which is read straight off the crossing component's
`isolate` map (`approval.component_realm` -> `policy.component_realms` -> the
component IR's `isolate` field). An untrusted author writes its own component
NAMES, so the glob half discriminates nothing against it — the realm was the
only half left. A candidate that writes `isolate <key> in realm("billing")`
therefore selects which of the operator's standing approvals cover its class-(c)
crossings: it widens its own authority, with no fresh admission anywhere in the
loop. `propose` now admits under `AdmissionProfile.self_extension`, which is
`untrusted_author` plus that refusal, so a proposal lands in the SHARED realm —
the address a candidate that said nothing was going to get anyway.

**2. A halt dominates a proposal (`HALTED`).** `Session.swap` already refused
under an E-Stop (item 443), but the refusal reached the loop through `propose`'s
`except` arm as `SWAP_REVERTED` — the verdict that asserts the candidate was
judged, gen N is intact, and the process is still serving. After an E-Stop all
three are false: nothing was judged, every registered entry is STRANDED, and the
instance is dead. Worse, `SWAP_REVERTED` is the RETRY-shaped verdict, the one a
self-extension loop answers by generating a better candidate — so a halted gate
would be proposed at forever instead of reconciled. The halt is checked first and
re-read after, because the swap runs the approver callback and the embedder's own
host code may hit the button from inside it.
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
    reason="the halt/propose interaction is a runtime property — needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)


# --------------------------------------------------------------------------- #
# Sources. Deliberately the slice-1 shapes so the only variable is the realm.
# --------------------------------------------------------------------------- #

_DECLS = (
    "service Ops { emission fn stash(p: Str) }\n"
    "service Tool {\n"
    "  fn describe() -> Str\n"
    "  emission fn run(p: Str)\n"
    "}\n"
)

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

_BASE = _DECLS + (
    "component ToolV1 requires ops: Ops provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v1"\n'
    "    fn run(p) { }\n"
    "  }\n"
    "}\n"
) + _OPS_PROVIDER

_PROVIDERS = {"ops_provider.rvl": _OPS_PROVIDER}

# The clean candidate: no realm named, so it lands in the shared realm.
_AGENT_V2 = _DECLS + (
    "component ToolV2 requires ops: Ops provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
)

# The SAME candidate, one line longer: it places itself into the `billing`
# realm. That one line is the authority grab this slice refuses.
_AGENT_REALM = _DECLS + (
    "component ToolV2 requires ops: Ops provides tool: Tool {\n"
    '  isolate tool in realm("billing")\n'
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
)

# The item-162 plural: one required key bound across N named realms. Same fact
# with a fan-out attached, refused identically.
_AGENT_REALMS_ROUTE = _DECLS + (
    "component ToolV2 requires ops: Ops provides tool: Tool {\n"
    '  isolate ops in realms("w1", "w2") strategy(round_robin)\n'
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
)


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


@pytest.fixture(autouse=True)
def _clean_halt():
    """No halt leaks between tests. The latch is PROCESS-global by design — an
    E-Stop that only stopped one session would not be a stop — and `Session`
    deliberately does not clear it on teardown, so without this one halted test
    kills every load after it."""
    if importlib.util.find_spec("cordis") is None:
        yield
        return
    import runtime as rt  # noqa: PLC0415 — the py backend, only when present

    def _reset():
        rt.clear_estop()
        rt.arm_estop_latch(None)
        rt._LIVE_FRAMES.clear()
    _reset()
    yield
    _reset()


# =========================================================================== #
# 1. The mechanism the refusal exists for: a realm IS the authority address.
# =========================================================================== #

def test_a_self_chosen_realm_is_a_self_chosen_approval_scope():
    """The premise, measured rather than asserted: the realm a candidate writes
    for itself is exactly the realm the approval policy keys its standing
    approvals and auto-approve rules on.

    `_find_auto_approve`/`_find_standing_approval` skip a rule whose `realm` is
    set and differs from `ticket["realm"]`; `ticket["realm"]` is
    `ClassMap.component_realm`, which is `policy.component_realms` over the
    component IR's `isolate` map. So the chain from a source line to an approval
    scope is one hop, and the candidate writes the source line."""
    from revl.compiler import compile_source
    from revl.mcp.approval import ClassMap
    from revl.policy import component_realms

    ir = compile_source(_AGENT_REALM, "candidate.rvl")
    isolate = {e["name"]: e.get("isolate") for e in ir["components"]}
    assert isolate["ToolV2"] == {"tool": "billing"}
    assert component_realms(ir["manifest"], "ToolV2") == frozenset({"billing"})
    # and that is what the ticket would carry — the realm half of every
    # standing-approval / auto-approve match.
    assert ClassMap(ir).component_realm("ToolV2") == "billing"

    # the clean candidate keys on the shared realm (`""`), which no
    # realm-scoped rule covers.
    clean = compile_source(_AGENT_V2, "candidate.rvl")
    assert ClassMap(clean).component_realm("ToolV2") == ""


# =========================================================================== #
# 2. The refusal itself, at the profile boundary (no runtime needed).
# =========================================================================== #

@pytest.mark.parametrize("source,spelling", [
    (_AGENT_REALM, 'realm("billing")'),
    (_AGENT_REALMS_ROUTE, 'realms("w1", "w2")'),
])
def test_self_extension_profile_refuses_a_named_realm(source, spelling):
    """Both spellings — the singular placement and the item-162 plural route —
    are refused under `self_extension`, structurally, before lowering."""
    from revl.admit_profile import AdmissionProfile
    from revl.compiler import compile_source
    from revl.errors import RevlError

    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "<candidate>.rvl",
                       profile=AdmissionProfile.self_extension(["Ops"]))
    error = excinfo.value
    assert error.code == "G9"
    assert spelling in str(error)
    # the refusal is the repair signal: it names the enactable alternative.
    refs = {alt["ref"] for alt in error.navigate["alternatives"]}
    assert refs == {"shared-realm", "operator-swap"}


def test_untrusted_author_profile_is_unchanged():
    """Item 330's per-turn profile is BYTE-IDENTICAL: the new refusal is opt-in
    (`no_realm_placement`), and only `self_extension` opts in. A per-turn admit
    is additive and torn down with the turn; a proposal replaces the composition
    and keeps serving, which is why only the second one is bounded here. The
    per-turn case is the same question with a shorter blast radius — named, not
    solved."""
    from revl.admit_profile import AdmissionProfile
    from revl.compiler import compile_source

    profile = AdmissionProfile.untrusted_author(["Ops"])
    assert profile.no_realm_placement is False
    compile_source(_AGENT_REALM, "<turn>.rvl", profile=profile)  # admits


def test_the_refusal_is_root_scoped():
    """A TRUSTED co-composed provider module still places whatever the operator
    wrote — the check is scoped to the ROOT source being admitted, exactly as
    `check_no_extern` is. The operator's realms are not the agent's to lose."""
    from revl.admit_profile import AdmissionProfile
    from revl.compiler import compile_source

    provider = (
        "service Ops { fn ping() -> Str }\n"
        'pub fn helper() -> Str { return "h" }\n'
        "component OpsProvider provides ops: Ops {\n"
        '  isolate ops in realm("billing")\n'
        '  provide ops { fn ping() = "p" }\n'
        "}\n"
    )
    candidate = (
        'use "prov.rvl" { helper }\n'
        "service Tool { fn describe() -> Str }\n"
        "component T provides tool: Tool { provide tool { fn describe() = helper() } }\n"
    )
    compile_source(candidate, "<candidate>.rvl", modules={"prov.rvl": provider},
                   profile=AdmissionProfile.self_extension([]))  # admits


# The candidate with its own host body — refused by the `untrusted_author`
# BASE of the profile (G8, `check_no_extern`), so it measures the other half.
_AGENT_EXTERN = _DECLS + (
    'extern pure fn peek(p: Str) -> Str = @py { return p }\n'
    "component ToolV2 requires ops: Ops provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = peek("v2")\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
)


def _decision_only_gate():
    """A `Gate` carrying exactly what `propose`'s DECISION compile reads: the
    loaded latch and a session that is not halted.

    The decision refuses before step 2 ever builds the transition composition,
    so nothing here reaches `Session.swap` and no cordis runtime is needed —
    which is the point. The profile the decision compiles under is observable
    from the verdict `propose` returns, in the plain `frontend` job."""
    from types import SimpleNamespace

    from revl.gate import Gate

    gate = Gate.__new__(Gate)
    gate._loaded = True
    gate._session = SimpleNamespace(halted=False)
    return gate


def test_propose_admits_under_self_extension_not_untrusted_author():
    """The wiring at the seam, DRIVEN rather than read off `propose`'s source.

    Deliberately not a source grep. The assertion this replaced matched
    `"AdmissionProfile.self_extension(granted)"` in the method text, and text
    certifies nothing about which profile the compile RUNS under: building the
    right profile and then handing the compiler `profile=None`, or relaxing
    `no_realm_placement` on the next line, both leave that grep green and put
    the authority-address grab straight back. Here the realm-grabbing candidate
    goes through `propose` itself and the refusal it returns is the evidence.

    The refusal is attributable to the `self_extension` DELTA specifically: the
    same source under `untrusted_author` admits (asserted below), so a `propose`
    that built the per-turn profile would have carried this candidate on to the
    swap."""
    from revl.admit_profile import AdmissionProfile
    from revl.compiler import compile_source

    result = _decision_only_gate().propose(_AGENT_REALM, granted=["Ops"],
                                           providers=_PROVIDERS)
    assert result.admitted is False
    assert result.code == "G9"
    assert 'realm("billing")' in result.message

    # the same one line, under the per-turn profile: admitted. So the verdict
    # above is the delta and nothing else.
    compile_source(_AGENT_REALM, "<turn>.rvl",
                   profile=AdmissionProfile.untrusted_author(["Ops"]))


def test_propose_keeps_the_untrusted_author_base_at_the_seam():
    """`self_extension` is `untrusted_author` PLUS the realm refusal, so the
    seam has to carry both halves. A profile object built with only
    `no_realm_placement` would pass the test above and lose G8/R2 entirely."""
    with_extern = _decision_only_gate().propose(
        _AGENT_EXTERN, granted=["Ops"], providers=_PROVIDERS)
    assert with_extern.admitted is False
    assert "extern" in with_extern.message.lower()

    # and the `granted` set is the one the caller passed, not a stand-in: the
    # clean candidate reaches `Ops`, so an empty grant refuses it.
    ungranted = _decision_only_gate().propose(_AGENT_V2, granted=[],
                                              providers=_PROVIDERS)
    assert ungranted.admitted is False


# =========================================================================== #
# 3. The refusal end to end: the live composition is untouched.
# =========================================================================== #

@needs_cordis
def test_propose_refuses_a_realm_grabbing_candidate_live(gate_factory):
    """The running composition is byte-identical before and after: the candidate
    is refused as DATA (never a raise), gen N still answers, and gen N's own
    realm placement — there is none — is what the process keeps serving."""
    gate = gate_factory()
    gate.load(_BASE)
    before = gate._session.state()

    result = gate.propose(_AGENT_REALM, granted=["Ops"], providers=_PROVIDERS)
    assert not result.admitted, "a realm grab must not reach the swap"
    assert not result.swapped and not result.reverted
    assert result.code == "G9"
    assert "realm" in (result.message or "")

    after = gate._session.state()
    assert after["providedKeys"] == before["providedKeys"]
    assert ({c["name"] for c in after["components"]}
            == {c["name"] for c in before["components"]})
    assert gate.call("tool", "describe", [])["result"] == "v1"

    # and the SAME candidate without the realm line still admits and swaps —
    # the refusal is the one line, not the shape of the proposal.
    ok = gate.propose(_AGENT_V2, granted=["Ops"], providers=_PROVIDERS)
    assert ok.admitted and ok.swapped, ok.message
    assert gate.call("tool", "describe", [])["result"] == "v2"


# =========================================================================== #
# 4. Halt dominance.
# =========================================================================== #

@needs_cordis
def test_propose_under_a_halt_is_HALTED_not_SWAP_REVERTED(gate_factory):
    """After an E-Stop, `propose` refuses as a HALT. The distinction is the whole
    test: `SWAP_REVERTED` says "your candidate was bad, gen N is still serving,
    try another" — the retry-shaped verdict a self-extension loop answers by
    generating a better candidate. All three claims are false after a halt, so a
    loop handed SWAP_REVERTED here would propose at a dead session forever."""
    gate = gate_factory()
    gate.load(_BASE)
    gate.estop("operator hit the button")

    result = gate.propose(_AGENT_V2, granted=["Ops"], providers=_PROVIDERS)
    assert result.code == "HALTED"
    assert not result.admitted
    assert not result.swapped
    assert not result.reverted, "an E-Stop reverts NOTHING — it strands"
    assert "recover" in (result.message or "")
    assert result.as_dict()["code"] == "HALTED"


@needs_cordis
def test_the_halt_dominates_the_forbidden_grant_refusal(gate_factory):
    """Ordering, not merely presence: a proposal that is ALSO forbidden-granted
    reports the halt, because a halt dominates every other verdict (item 443) —
    including a verdict on a candidate that was never going to be judged."""
    gate = gate_factory()
    gate.load(_BASE)
    gate.estop("operator hit the button")

    result = gate.propose(_AGENT_V2, granted=["Ops", "Admission"],
                          providers=_PROVIDERS)
    assert result.code == "HALTED", "FORBIDDEN_GRANT must not outrank the halt"


@needs_cordis
def test_a_halt_engaged_during_the_swap_wins(gate_factory):
    """The race, and it needs no second thread: the swap runs the item-246
    activation gate, which calls the embedder's `approver`, which is host code
    that can hit the E-Stop from inside the very call `propose` is awaiting.
    Modelled here by halting at the swap seam. The verdict must be the halt, not
    the revert the swap's refusal literally raised."""
    gate = gate_factory()
    gate.load(_BASE)
    session = gate._session
    original = session.swap

    def halting_swap(ir, *args, **kwargs):
        session.estop("the approver hit the button mid-swap")
        return original(ir, *args, **kwargs)   # now refuses: the session is dead

    session.swap = halting_swap
    try:
        result = gate.propose(_AGENT_V2, granted=["Ops"], providers=_PROVIDERS)
    finally:
        session.swap = original

    assert result.code == "HALTED", (
        "the swap's own refusal was the halt refusal; reporting it as "
        "SWAP_REVERTED would claim a gen N that is dead")
    assert not result.reverted and not result.swapped
    assert "estop_report" in (result.message or "")


@needs_cordis
def test_the_halt_inventory_is_readable_from_the_gate(gate_factory):
    """A guarantee about a button the embedder cannot reach is not a guarantee.
    The gate exposes both halves: the button and the inventory."""
    gate = gate_factory()
    gate.load(_BASE)

    assert gate.estop_report()["halted"] is False
    halt = gate.estop("operator halt")
    assert halt["halted"] is True

    report = gate.estop_report()
    assert report["halted"] is True
    # an E-Stop is never clean — it violates R4 by design and says so.
    assert report["clean"] is False

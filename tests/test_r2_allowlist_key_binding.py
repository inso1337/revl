"""R2's internal-provision exemption is resolved by BINDING KEY, not by service
name — the decoy-provision bypass of the item-329 allowlist.

`check_allowlist` exempted a `requires` whose SERVICE the turn also provided
anywhere, but the wiring resolves a `requires` by provision KEY. So an untrusted
candidate declared one throwaway component providing the same service under an
unused key, and its real `requires <live-key>: <Service>` was then never
allowlist-checked at all: at wiring time it bound to the real, ambient,
host-backed provider that owns that key. `granted` became decorative.

Three proven exploits, one per admitted crossing, plus the escalation:

  1. `Session.admit` (item 330) — the per-turn crossing, reaching an ambient
     host-backed service with `granted=[]` and firing its witnessed effect.
  2. `Gate.propose` (item 334) — the self-extension verb, whose reach came via
     the operator's trusted `providers` map.
  3. the DECIDER reach — the same decoy naming `Admission`, which drove
     `host_admit` -> `revl.mcp.admit_bridge.admit` and admitted further code with
     a granted set the untrusted turn chose itself. Item 334's FORBIDDEN_GRANT
     rule never fired, because the turn never NAMED a decider service in a
     granted set.

Plus the false-positive side: a candidate that genuinely provides and consumes
its OWN service, under the key it requires, must still admit.
"""

from __future__ import annotations

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

from revl import AdmissionProfile, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the admitted turn is wired into a live composition — needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)


# --------------------------------------------------------------------------- #
# Sources. The ambient provider carries the witnessed host body (trusted); every
# untrusted candidate below carries none.
# --------------------------------------------------------------------------- #

_OPS_HOST = (
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
    "  provide ops { fn stash(p) { effect stash_path(p) } }\n"
    "}\n"
)

_OPS_DECL = "service Ops { emission fn stash(p: Str) }\n"

# gen N for the `Session.admit` proofs: the ambient host-backed `ops` provider.
_ADMIT_BASE = _OPS_DECL + (
    "service Tool { fn describe() -> Str }\n"
) + _OPS_HOST + (
    "component ToolV1 provides tool: Tool {\n"
    '  provide tool { fn describe() = "v1" }\n'
    "}\n"
)

# the model-authored per-turn source. `Ops` is AMBIENT but is NOT granted.
_TURN_HONEST = _OPS_DECL + (
    "service Turn { emission fn go(p: Str) }\n"
    "component TurnC requires ops: Ops provides turn: Turn {\n"
    "  provide turn { fn go(p) { emit ops.stash(p) } }\n"
    "}\n"
)

# the whole exploit payload: five lines of decoy appended to a refused turn.
_TURN_DECOY = _TURN_HONEST + (
    "component Decoy provides decoy_ops: Ops {\n"
    "  provide decoy_ops { fn stash(p) { } }\n"
    "}\n"
)


def _admit_compile(turn_source: str, granted, base_source: str = _ADMIT_BASE):
    """The admit DECISION alone, as a pure compile against the ambient base — no
    runtime, so the refusal is provable without cordis."""
    return compile_source(
        turn_source, "<turn>.rvl",
        manifest=compile_source(base_source, "base.rvl"),
        profile=AdmissionProfile.untrusted_author(granted))


@pytest.fixture
def gate_factory():
    """Live `Gate`s, each guaranteed closed (the single-gate-per-process
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
            try:
                g.abort()
            except Exception:
                pass
            g.close()


@pytest.fixture
def artifact(tmp_path):
    p = tmp_path / "artifact.txt"
    p.write_text("deliverable", encoding="utf-8")
    return str(p)


def _mutated(path: str) -> bool:
    return not os.path.exists(path) and os.path.exists(path + ".bak")


# =========================================================================== #
# EXPLOIT 1 — the decoy at the per-turn `Session.admit` crossing (item 330).
# =========================================================================== #

def test_decoy_provision_does_not_exempt_an_ambient_reach():
    """The pure-compile core of exploit 1: `requires ops: Ops` binds to the
    AMBIENT `ops` provider, so a decoy providing `Ops` under `decoy_ops` cannot
    launder it into internal wiring."""
    with pytest.raises(RevlError) as exc:
        _admit_compile(_TURN_DECOY, granted=set())
    assert getattr(exc.value, "code", None) == "R2"
    msg = str(exc.value)
    assert "not in the granted set" in msg
    assert "`Ops`" in msg and "requires ops" in msg


def test_decoy_refusal_names_the_key_binding_confusion():
    """The repair signal names the decoy shape: the turn DOES provide the
    service, just not under the key the requirement binds to."""
    with pytest.raises(RevlError) as exc:
        _admit_compile(_TURN_DECOY, granted=set())
    hint = getattr(exc.value, "hint", "") or ""
    assert "`decoy_ops`" in hint
    assert "binds by KEY" in hint


def test_the_honest_turn_and_the_decoy_turn_are_refused_alike():
    """The decoy changes NOTHING: with `granted=[]` both shapes refuse with the
    same code on the same requirement."""
    with pytest.raises(RevlError) as honest:
        _admit_compile(_TURN_HONEST, granted=set())
    with pytest.raises(RevlError) as sneak:
        _admit_compile(_TURN_DECOY, granted=set())
    assert getattr(honest.value, "code", None) == "R2"
    assert getattr(sneak.value, "code", None) == "R2"
    # same refusal on the same requirement; only the repair hint differs, since
    # the decoy shape earns the extra "a `requires` binds by KEY" sentence.
    assert (str(honest.value).splitlines()[0]
            == str(sneak.value).splitlines()[0])


@needs_cordis
def test_decoy_turn_cannot_fire_an_ungranted_ambient_host_effect(gate_factory,
                                                                 artifact):
    """Exploit 1 end to end on the SHIPPED crossing: before the fix this admitted
    and `turn.go` fired the ambient witnessed `fs` effect with `granted=[]`."""
    gate = gate_factory(record=True)
    gate.load(_ADMIT_BASE)
    result = gate.admit(_TURN_DECOY, granted=[])
    assert not result.admitted, (
        f"decoy turn admitted with granted=[]: keys={result.keys}")
    assert result.code == "R2"
    # nothing was wired, so the turn's key is not callable and the artifact is
    # untouched.
    assert "turn" not in gate._session.state()["providedKeys"]
    assert not _mutated(artifact)


# =========================================================================== #
# EXPLOIT 2 — the decoy at `Gate.propose` (item 334, the self-extension verb).
# =========================================================================== #

_PROPOSE_DECLS = _OPS_DECL + (
    "service Tool {\n"
    "  fn describe() -> Str\n"
    "  emission fn run(p: Str)\n"
    "}\n"
)
_PROPOSE_BASE = _PROPOSE_DECLS + (
    "component ToolV1 requires ops: Ops provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v1"\n'
    "    fn run(p) { }\n"
    "  }\n"
    "}\n"
) + _OPS_HOST
_PROPOSE_CANDIDATE = _PROPOSE_DECLS + (
    "component ToolV2 requires ops: Ops provides tool: Tool {\n"
    "  provide tool {\n"
    '    fn describe() = "v2"\n'
    "    fn run(p) { emit ops.stash(p) }\n"
    "  }\n"
    "}\n"
)
_PROPOSE_DECOY = _PROPOSE_CANDIDATE + (
    "component Decoy provides dummy: Ops {\n"
    "  provide dummy { fn stash(p) { } }\n"
    "}\n"
)
_PROVIDERS = {"ops_provider.rvl": _OPS_HOST}


@needs_cordis
def test_decoy_candidate_is_refused_at_propose(gate_factory, artifact):
    """Exploit 2: `propose`'s STANDALONE decision compile hands the trusted
    providers in as `modules=`, so the candidate's `requires ops` binds to
    nothing there — indeterminate, and the decoy must not exempt it. Before the
    fix this swapped and `tool.run` fired the trusted provider's witnessed
    effect with `granted=[]`."""
    gate = gate_factory(record=True)
    gate.load(_PROPOSE_BASE)
    result = gate.propose(_PROPOSE_DECOY, granted=[], providers=_PROVIDERS)
    assert not result.admitted, "decoy candidate admitted with granted=[]"
    assert not result.swapped
    assert result.code == "R2"
    # gen N is still serving, unswapped.
    assert gate.call("tool", "describe", [])["result"] == "v1"
    gate.call("tool", "run", [artifact])
    assert not _mutated(artifact)


# =========================================================================== #
# EXPLOIT 3 — the escalation: the decoy reaches the DECIDER, past FORBIDDEN_GRANT.
# =========================================================================== #

_NESTED = _OPS_DECL + (
    "service Nested { emission fn go(p: Str) }\n"
    "component NestedC requires ops: Ops provides nested: Nested {\n"
    "  provide nested { fn go(p) { emit ops.stash(p) } }\n"
    "}\n"
)
_DECIDER_TURN = (
    "service Admission {\n"
    "  emission fn admit(source: Str, granted: Trusted[List[Str]]) -> Str\n"
    "}\n"
    "service Turn { emission fn go(s: Str) -> Str }\n"
    "component TurnC requires admission: Admission provides turn: Turn {\n"
    '  provide turn { fn go(s) = emit admission.admit(s, ["Ops"]) }\n'
    "}\n"
    "component Decoy provides decoy_adm: Admission {\n"
    '  provide decoy_adm { fn admit(source, granted) = "" }\n'
    "}\n"
)


@needs_cordis
def test_decoy_turn_cannot_reach_the_admit_decider(gate_factory):
    """Exploit 3: item 334's FORBIDDEN_GRANT only inspects the GRANTED SET, and
    the decoy bypass never has to name a decider service there. Before the fix
    the turn was admitted with `granted=[]`, bound to the live `admission` key of
    the running `AdmitGate`, and drove `host_admit` ->
    `revl.mcp.admit_bridge.admit` to admit further code with a granted set it
    chose itself."""
    gate = gate_factory(record=True)
    gate.load({"base.rvl": _OPS_DECL + _OPS_HOST,
               str(_ROOT / "stdlib" / "admit.rvl"): None})
    assert "admission" in gate._session.state()["providedKeys"]

    result = gate.admit(_DECIDER_TURN, granted=[])
    assert not result.admitted, (
        f"decoy turn reached the decider with granted=[]: keys={result.keys}")
    assert result.code == "R2"
    assert "Admission" in (result.message or "")
    # the decider was never driven, so no nested turn was admitted.
    live = gate._session.state()["providedKeys"]
    assert "nested" not in live and "turn" not in live


@needs_cordis
def test_forbidden_grant_still_refuses_a_named_decider(gate_factory):
    """The item-334 rule is untouched: naming the decider in `granted` is still
    refused before any compile."""
    gate = gate_factory()
    gate.load(_OPS_DECL + _OPS_HOST)
    result = gate.propose(_PROPOSE_CANDIDATE, granted=["Admission"])
    assert not result.admitted
    assert result.code == "FORBIDDEN_GRANT"


# =========================================================================== #
# FALSE POSITIVES — the honest cases must be unchanged.
# =========================================================================== #

def test_own_provision_under_the_required_key_still_admits():
    """The exemption's real purpose: a candidate that provides AND consumes its
    own service, under the very key it requires, reaches nothing external and
    admits under an EMPTY granted set — even though the ambient composition is
    right there."""
    src = (
        "service Inner { fn v() -> Str }\n"
        "service Turn { fn run() -> Str }\n"
        "component InnerProv provides inner: Inner {\n"
        '  provide inner { fn v() = "x" }\n'
        "}\n"
        "component TurnC requires inner: Inner provides turn: Turn {\n"
        "  provide turn { fn run() = inner.v() }\n"
        "}\n"
    )
    doc = _admit_compile(src, granted=set())
    assert {c["name"] for c in doc["components"]} == {"InnerProv", "TurnC"}


def test_an_explicitly_granted_ambient_reach_still_admits():
    """The allowlist is not a blanket refusal: granting `Ops` admits the very
    turn the empty granted set refuses."""
    doc = _admit_compile(_TURN_HONEST, granted={"Ops"})
    assert {c["name"] for c in doc["components"]} == {"TurnC"}


def test_a_granted_reach_admits_even_alongside_an_own_provision():
    """Both exemption routes at once: one requirement granted, one bound to the
    turn's own provision of that key."""
    src = (
        _OPS_DECL
        + "service Inner { fn v() -> Str }\n"
        + "service Turn { emission fn go(p: Str) }\n"
        + "component InnerProv provides inner: Inner {\n"
        '  provide inner { fn v() = "x" }\n'
        "}\n"
        "component TurnC requires ops: Ops, inner: Inner provides turn: Turn {\n"
        "  provide turn { fn go(p) { emit ops.stash(inner.v()) } }\n"
        "}\n"
    )
    doc = _admit_compile(src, granted={"Ops"})
    assert {c["name"] for c in doc["components"]} == {"InnerProv", "TurnC"}


@needs_cordis
def test_the_honest_granted_turn_still_runs_its_ambient_effect(gate_factory,
                                                               artifact):
    """The fix does not break the SHIPPED honest path: with `Ops` granted, the
    same turn admits, wires, and fires the ambient witnessed effect."""
    gate = gate_factory(record=True)
    gate.load(_ADMIT_BASE)
    result = gate.admit(_TURN_HONEST, granted=["Ops"])
    assert result.admitted, result.message
    result.handle.call("turn", "go", [artifact])
    assert _mutated(artifact)


@needs_cordis
def test_the_honest_granted_candidate_still_swaps(gate_factory, artifact):
    """`Gate.propose`'s honest path is unchanged: `granted=["Ops"]` admits and
    swaps, and the swapped-in candidate runs the trusted provider's effect."""
    gate = gate_factory(record=True)
    gate.load(_PROPOSE_BASE)
    result = gate.propose(_PROPOSE_CANDIDATE, granted=["Ops"],
                          providers=_PROVIDERS)
    assert result.admitted and result.swapped, result.message
    assert gate.call("tool", "describe", [])["result"] == "v2"
    gate.call("tool", "run", [artifact])
    assert _mutated(artifact)


def test_a_self_contained_turn_reaching_nothing_still_admits():
    """A turn with no `requires` at all is untouched by the check."""
    src = (
        "service Turn { fn run() -> Str }\n"
        "component TurnC provides turn: Turn {\n"
        '  provide turn { fn run() = "x" }\n'
        "}\n"
    )
    doc = _admit_compile(src, granted=set())
    assert {c["name"] for c in doc["components"]} == {"TurnC"}


def test_the_allowlist_stays_off_without_a_granted_set():
    """`granted=None` is the trusted-author rendering: the key-binding rule adds
    no refusal there."""
    doc = compile_source(
        _TURN_DECOY, "<turn>.rvl",
        manifest=compile_source(_ADMIT_BASE, "base.rvl"),
        profile=AdmissionProfile(no_extern=True, granted=None))
    assert {c["name"] for c in doc["components"]} == {"TurnC", "Decoy"}

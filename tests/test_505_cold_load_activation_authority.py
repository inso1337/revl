"""The cold-load exemption is narrowed to the case its justification covers.

Roadmap item 300 exempted the initial cold `revl_load` from the operator
capability gate (item 55) on the stated ground that *"loading a candidate is not
yet a privileged mutation - the gate belongs on swap/apply, not the first
load"*. That is true of a candidate whose body only wires provisions. It is
false of a candidate whose component-scope body EMITS: `Session.load` BOOTS,
activation runs, and an `emit` there is a one-way boundary crossing with no
inverse -- item 246's class (c), the very class the approval gate tickets
before boot.

So the exemption was wider than its reason, and the operator profile could not
express a prohibition on it. Measured on the pre-fix tree: an operator holding
`may swap on tenant_a` and nothing else -- or `may * on *` plus an explicit
`may not load on *` -- got `gated=False` from `decide`, the load ran, and the
emission crossed. The same verb against a LIVE composition was refused by the
same profile, so the exemption, not the grant model, was the inconsistency: it
defeated deny-wins, which `Operator.allows` documents as the model's own
invariant ("deny wins over allow").

The fix narrows the exemption to what item 300 actually argued: the cold load
is ungated only while the candidate's activation reaches no class-(c) crossing.
Everything item 300 was about -- inspecting and gauntleting a candidate that
wires provisions -- is unchanged. An emitting candidate falls through to the
ordinary gate, where `may load on ...` authorizes it and `may not load on *`
refuses it.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp.operator import Grant, Operator, decide  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

# A candidate whose ACTIVATION body emits: `emit` sits at component scope, so it
# runs when the component boots, not when a provided method is called.
EMITTING = """
service Db {
  fn query(sql: Str) -> Str
  emission fn execute(sql: Str) -> Int
}

service S { fn q() -> Str }

component Store provides db: Db {
  provide db {
    fn query(sql) = "row"
    fn execute(sql) = 1
  }
}

component Migrator requires db: Db provides s: S {
  emit db.execute("INSERT INTO migration_log VALUES (42)")
  provide s { fn q() = "ok" }
}
"""

# The shape item 300 was actually about: a candidate that only wires provisions.
PURE = """
service S { fn q() -> Str }
component A provides s: S { provide s { fn q() = "ok" } }
"""

ALLOW_ALL = (Grant(verbs=("*",), subjects=("*",), allow=True),)
MAY_NOT_LOAD = ALLOW_ALL + (
    Grant(verbs=("load",), subjects=("*",), allow=False),)
SWAP_ONLY = (Grant(verbs=("swap",), subjects=("tenant_a",), allow=True),)


def _cold(grants):
    """A genuinely cold session (`ir is None`) with `grants` bound."""
    session = Session()
    assert session.ir is None, "the fixture must start cold"
    session.operator = Operator("alice", grants)
    return session


def _decide(source, grants):
    return decide(_cold(grants), "revl_load", {"source": source})


# ============================================================================
# 1. the defect: an emitting cold load was ungated for every profile
# ============================================================================

def test_a_cold_load_whose_activation_emits_is_gated():
    """The headline. `may swap on tenant_a` is no authority to emit."""
    decision = _decide(EMITTING, SWAP_ONLY)

    assert decision.gated, "an emitting cold load is a privileged mutation"
    assert not decision.allowed
    assert decision.verb == "load"
    assert "load" in decision.message


def test_an_explicit_may_not_load_is_honoured_when_the_load_emits():
    """Deny-wins. The pre-fix tree returned `gated=False` here, so the one
    statement the profile language can make about `load` was silently ignored."""
    decision = _decide(EMITTING, MAY_NOT_LOAD)

    assert decision.gated, "an explicit `may not load on *` must be honoured"
    assert not decision.allowed


def test_a_grant_less_operator_cannot_emit_through_a_cold_load():
    """No grants at all is the least-authority operator, not a licence."""
    decision = _decide(EMITTING, ())

    assert decision.gated and not decision.allowed


# ============================================================================
# 2. the fix is NARROWER than the defect: item 300's own case is untouched
# ============================================================================

def test_a_cold_load_that_only_wires_provisions_stays_ungated():
    """Item 300's reason holds here and only here: nothing is activated that
    crosses a boundary, so there is still no privileged mutation to gate."""
    for grants in ((), SWAP_ONLY, MAY_NOT_LOAD):
        decision = _decide(PURE, grants)
        assert not decision.gated, \
            "a provisioning-only cold load must stay ungated (item 300)"


def test_a_may_load_grant_authorizes_an_emitting_cold_load():
    """The gate, not a ban: an operator who holds `load` proceeds, and the
    decision is reported as gated-and-allowed so it is stamped like any other."""
    decision = _decide(EMITTING, ALLOW_ALL)

    assert decision.gated and decision.allowed
    assert "Migrator" in decision.subjects


def test_an_undecidable_candidate_does_not_widen_the_exemption():
    """A candidate that does not compile cannot boot, so it cannot emit -- but
    the gate must never widen on an answer it could not derive."""
    decision = _decide("component Broken provides s: S { this is not revl }",
                       SWAP_ONLY)

    assert decision.gated and not decision.allowed


# ============================================================================
# 3. the reason the gate belongs here, pinned
# ============================================================================

def test_the_emitting_candidate_is_class_c_at_activation():
    """Pins the mechanism, not just the verdict: the gate fires because the
    candidate's activation reach IS class (c). If the class map ever stops
    reporting this, the gate above would silently stop firing."""
    from revl.compiler import compile_source  # noqa: PLC0415
    from revl.mcp.approval import ClassMap  # noqa: PLC0415

    reaches = {reach["component"]: reach["class"]
               for reach in ClassMap(compile_source(EMITTING, "e.rvl"))
               .activation_reaches()}

    assert reaches["Migrator"] == "c", "the activation emit is a class-(c) reach"
    assert reaches["Store"] is None, "wiring a provision is not a crossing"


def test_the_ungated_cold_load_really_emits():
    """The premise item 300 rests on, measured. With no operator bound the load
    is ungated by design -- and the timeline records the emission as a
    non-revertible, activation-phase boundary crossing, which is exactly why a
    bound profile must not be able to skip it."""
    from revl.compiler import compile_source  # noqa: PLC0415

    session = Session()  # no operator: the default posture, ungated
    assert not decide(session, "revl_load", {"source": EMITTING}).gated

    assert session.load(compile_source(EMITTING, "e.rvl"), {},
                        record=True).get("loaded") is True

    emissions = [step for component in session.timeline()["components"]
                 for step in component["steps"]
                 if step["kind"] == "emission"]

    assert emissions, "the cold load must have crossed a boundary"
    assert all(not step["revertible"] for step in emissions), \
        "an emission is one-way: it has no inverse"
    assert all(step["origin"]["phase"] == "activation" for step in emissions), \
        "it crossed because the LOAD ran the activation body"


def test_a_live_load_is_gated_the_same_way():
    """The consistency the exemption broke: the same verb, the same profile,
    must not read differently for being cold."""
    from revl.compiler import compile_source  # noqa: PLC0415

    live = _cold(SWAP_ONLY)
    live.load(compile_source(PURE, "pure.rvl"), {})

    assert live.ir is not None
    decision = decide(live, "revl_load", {"source": EMITTING})

    assert decision.gated and not decision.allowed, \
        "a live load was already gated; the cold verdict must match it"

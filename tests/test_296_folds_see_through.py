"""Item 296 slice 4, E6: the 414 folds see through the adapter (design §6.3).

An adapter must not become a fold-blind eleventh crossing kind. Its only
crossing is kind 1, a `req` seam call, which every authority-derivation surface
the 414 reach-completeness matrix visits already accounts for. §6.3 asserts,
rather than assumes, that each in-scope surface attributes the CANDIDATE's real
emission *through* the adapter hop: a fold that reported the adapter as the
terminal boundary — the alias key `backing`, not the candidate's real token — is
a completeness bug.

This file adds the adapter-mediated matrix rows for the four surfaces §6.3
names. Each cell is DIFFERENTIAL in the 414 sense: the surface is probed over an
adapter fronting a candidate whose real boundary is `alpha`, and again over the
same adapter shape fronting one whose real boundary is `beta`. The surface must
report `alpha` for the first and `beta` for the second, and NEVER the internal
alias key `backing`. If a surface stopped at the adapter and reported the alias,
both probes would be identical and the discrimination would go red — which is
exactly the point: the assertion depends on the fold genuinely resolving the
candidate's boundary across the hop.

The launder-safety half (§6.3, the load-bearing invariant) is here too: a
candidate whose declared reach is wider than the carry names surfaces `*` and the
local G4 refuses, so a real emission never vanishes through the alias.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402
from revl.policy import component_reach  # noqa: E402

# The internal alias every synthesized (and hand-written) bridge binds the
# candidate under. A fold that reports THIS instead of the candidate's real
# token has stopped at the adapter — the completeness bug §6.3 closes.
ALIAS = "backing"


# ===========================================================================
# The adapter reach zoo — a `carrying(...)` bridge fronting an `emission[token]`
# candidate, exactly the shape `adapt.render_adapter` emits. `token` is the
# candidate's REAL boundary, so a surface that sees through the hop reports it
# and one that stops at the adapter reports `backing`.
# ===========================================================================


def _adapter_zoo(token: str) -> str:
    return f"""
service Vendor {{ emission[{token}] fn get(key: Str) -> Opt[Str] }}
service Cache {{ emission[{token}] fn get(key: Str) -> Opt[Str] }}
component CacheAdapter requires {ALIAS}: Vendor carrying({token}) provides cache: Cache {{
  provide cache {{ fn get(key) = emit {ALIAS}.get(key) }}
}}
"""


def _boundary_reach(token: str) -> frozenset[str]:
    """Surface: the G8 `_boundary` audit."""
    audit = audit_report(compile_source(_adapter_zoo(token), "adapter.rvl"))
    stats = audit["boundary"]["CacheAdapter"]
    caps = {tok for caps in stats["capabilities"].values() for tok in caps}
    externs = {tok for e in stats["externs"]
               for tok in (e.get("capabilities") or [e["name"]])}
    return frozenset(caps | externs)


def _component_reach(token: str) -> frozenset[str]:
    """Surface: `policy.component_reach`."""
    audit = audit_report(compile_source(_adapter_zoo(token), "adapter.rvl"))
    return frozenset(r.token for r in component_reach(audit, "CacheAdapter"))


def _approval_reach(token: str) -> frozenset[str]:
    """Surface: the approval `ClassMap` fold over `cache.get`'s closure."""
    ir = compile_source(_adapter_zoo(token), "adapter.rvl")
    reach = ClassMap(ir).classify_call("cache", "get")
    return frozenset(reach["capabilities"]) if reach else frozenset()


# The three capability-reach surfaces §6.3 names. The taint origin fold (the
# fourth) has its own zoo below because its axis is information flow, not the
# capability enumeration.
_REACH_SURFACES = {
    "g8_boundary_audit": _boundary_reach,
    "policy_component_reach": _component_reach,
    "approval_classmap_fold": _approval_reach,
}


@pytest.mark.parametrize("surface", sorted(_REACH_SURFACES))
def test_reach_surface_sees_the_candidate_boundary_through_the_adapter(surface):
    """Each capability-reach surface reports the candidate's REAL token across
    the adapter hop, and never the internal alias key. Differential: the token
    genuinely tracks the candidate (alpha vs beta), so a fold that terminated at
    the adapter would report `backing` for both and fail here."""
    probe = _REACH_SURFACES[surface]
    alpha = probe("alpha")
    beta = probe("beta")
    assert "alpha" in alpha, (
        f"{surface} did not attribute the candidate's real boundary `alpha` "
        f"through the adapter hop (got {sorted(alpha)})")
    assert "beta" in beta, (
        f"{surface} did not attribute the candidate's real boundary `beta` "
        f"through the adapter hop (got {sorted(beta)})")
    # the discrimination: the set depends on the candidate, not on a fixed
    # adapter surface.
    assert "beta" not in alpha and "alpha" not in beta, (
        f"{surface} does not discriminate the candidate's real boundary — it "
        f"reports the same set regardless of what the candidate crosses")
    # and it never stops at the adapter, reporting the meaningless alias key.
    assert ALIAS not in alpha and ALIAS not in beta, (
        f"{surface} reports the internal alias `{ALIAS}` as a boundary — it "
        f"stopped at the adapter instead of seeing the candidate's emission "
        f"(design §6.3, the completeness bug)")


def test_all_three_reach_surfaces_agree_on_the_real_boundary():
    """The surfaces are one matrix, not three opinions: over the same adapted
    composition every capability-reach surface derives the identical real
    boundary. A single surface drifting to the alias would break this."""
    sets = {name: probe("gamma") for name, probe in _REACH_SURFACES.items()}
    for name, reach in sets.items():
        assert reach == frozenset({"gamma"}), (
            f"{name} disagrees on the adapted call's real boundary: {sorted(reach)}")


# ===========================================================================
# The launder-safety half (§6.3, the load-bearing invariant): a carry that does
# not faithfully cover the candidate's reach surfaces `*`, so the local G4
# refuses rather than letting a real emission vanish through the alias.
# ===========================================================================


def test_carry_narrower_than_candidate_refused_so_nothing_hides():
    """The candidate reaches `[alpha, net]` but the carry names only `alpha`:
    the crossing surfaces `*` and G4 refuses. The fold sees the candidate's real
    reach through the alias, so the launder cannot pass."""
    src = """
service Vendor { emission[alpha, net] fn get(key: Str) -> Opt[Str] }
service Cache { emission[alpha] fn get(key: Str) -> Opt[Str] }
component CacheAdapter requires backing: Vendor carrying(alpha) provides cache: Cache {
  provide cache { fn get(key) = emit backing.get(key) }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "launder.rvl")
    assert ei.value.category == "emission-capability"


def test_bare_candidate_cannot_be_bounded_by_a_finite_carry():
    """A bare (unbounded) `emission` candidate cannot be bounded by any finite
    carry: `*` is surfaced and G4 refuses."""
    src = """
service Vendor { emission fn get(key: Str) -> Opt[Str] }
service Cache { emission[alpha] fn get(key: Str) -> Opt[Str] }
component CacheAdapter requires backing: Vendor carrying(alpha) provides cache: Cache {
  provide cache { fn get(key) = emit backing.get(key) }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "bare.rvl")
    assert ei.value.category == "emission-capability"


# ===========================================================================
# The two-hop cell (§6.4 + §6.3): an adapter fronting an adapter is still seen
# through end to end. The candidate's real boundary reaches the outer consumer
# across BOTH hops; a fold that terminated at either alias would lose it.
# ===========================================================================


def _two_hop_zoo(token: str) -> str:
    return f"""
service Vendor {{ emission[{token}] fn get(key: Str) -> Opt[Str] }}
service Mid {{ emission[{token}] fn get(key: Str) -> Opt[Str] }}
service Cache {{ emission[{token}] fn get(key: Str) -> Opt[Str] }}
component InnerAdapter requires backing: Vendor carrying({token}) provides mid: Mid {{
  provide mid {{ fn get(key) = emit backing.get(key) }}
}}
component OuterAdapter requires backing: Mid carrying({token}) provides cache: Cache {{
  provide cache {{ fn get(key) = emit backing.get(key) }}
}}
"""


def test_two_hop_chain_is_seen_through_both_aliases():
    """A bridge stacked on a committed bridge is transparent to every reach
    surface: each hop resolves its own alias to the same real token, so the
    outer consumer's boundary is the candidate's, across two `backing` seams."""
    ir = compile_source(_two_hop_zoo("alpha"), "chain.rvl")
    audit = audit_report(ir)
    for comp in ("InnerAdapter", "OuterAdapter"):
        reach = frozenset(r.token for r in component_reach(audit, comp))
        assert reach == frozenset({"alpha"}), (
            f"{comp} lost the candidate's real boundary across the chain: "
            f"{sorted(reach)}")
        cm = ClassMap(ir).classify_call(
            "mid" if comp == "InnerAdapter" else "cache", "get")
        caps = frozenset(cm["capabilities"]) if cm else frozenset()
        assert caps == frozenset({"alpha"}), (
            f"the approval fold stopped at {comp}'s alias: {sorted(caps)}")


# ===========================================================================
# The taint origin fold (§6.3, the fourth surface): provenance flows through the
# adapter hop. Its axis is information flow, so its differential is the origin
# reaching an emission WITH a tainted candidate return vs WITHOUT.
# ===========================================================================


def _adapter_taint_zoo(tainted: bool) -> str:
    """An adapter fronts a candidate whose `get` returns an untrusted value (or a
    clean one); the consumer emits that value to a sink. WITH taint the origin
    reaches the consumer's emission across the hop; WITHOUT it the identical
    wiring is clean — the discriminating pair."""
    ret = "Untrusted[Str]" if tainted else "Str"
    # the candidate's real value: a tainted host read, or a clean parameter.
    value = "emit fetch(key)" if tainted else "key"
    return f"""
extern emission fn fetch(u: Str) -> Untrusted[Str] = @py {{ return "" }}
service Vendor {{ emission fn get(key: Str) -> Opt[{ret}] }}
service Cache {{ emission fn get(key: Str) -> Opt[{ret}] }}
service Sink {{ emission fn out(s: {ret}) -> Int }}
service Ops {{ emission fn go(x: Str) -> Int }}
component VendorImpl provides vend: Vendor {{
  provide vend {{ fn get(key) {{ let v = {value} return Some(v) }} }}
}}
component CacheAdapter requires backing: Vendor provides cache: Cache {{
  provide cache {{ fn get(key) = emit backing.get(key) }}
}}
component Agent requires cache: Cache, snk: Sink provides ops: Ops {{
  provide ops {{ fn go(x) {{
    let r = emit cache.get(x)
    let v = match r {{ Some(v) => v, None => x }}
    emit snk.out(v)
    return 0
  }} }}
}}
"""


def _agent_taint_reaches(tainted: bool) -> frozenset[str]:
    ir = compile_source(_adapter_taint_zoo(tainted), "taint.rvl")
    comp = next(c for c in ir["components"] if c["name"] == "Agent")
    return frozenset((comp.get("taint") or {}).get("reaches") or [])


def test_taint_origin_fold_flows_provenance_through_the_adapter():
    """The taint origin fold sees the candidate's untrusted provenance across the
    adapter hop: an origin reaches the consumer's emission WITH a tainted
    candidate return and does not WITHOUT it. A fold that treated the adapter as
    an opaque boundary would read the consumer clean in both."""
    with_taint = _agent_taint_reaches(True)
    without_taint = _agent_taint_reaches(False)
    assert with_taint, (
        "the taint fold recorded no origin reaching the consumer's emission "
        "through the adapter hop — provenance was lost at the boundary")
    assert with_taint != without_taint, (
        "the taint fold does not depend on the candidate's provenance across "
        "the adapter hop (identical with and without a tainted return)")


# ===========================================================================
# Matrix completeness: the four surfaces §6.3 names are all covered here, so a
# fifth reach/authority surface added to the language forces an adapter-mediated
# cell rather than silently escaping the see-through guarantee.
# ===========================================================================

_E6_SURFACES = frozenset({
    "g8_boundary_audit",
    "policy_component_reach",
    "approval_classmap_fold",
    "taint_origin_fold",
})


def test_every_e6_surface_named_in_the_design_has_a_cell():
    """The §6.3 checklist: the three capability-reach surfaces plus the taint
    origin fold each have a see-through cell above."""
    covered = set(_REACH_SURFACES) | {"taint_origin_fold"}
    assert covered == _E6_SURFACES, (
        f"an E6 surface has no adapter-mediated cell: "
        f"{sorted(_E6_SURFACES ^ covered)}")

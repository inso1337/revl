"""Item 296, slice 1: alias token carry-over as a general wiring feature, plus
the safety guard that keeps a launder refused at the local G4 bound.

A `requires backing: Vendor carrying(cache)` binding attributes an emission
crossing through `backing` to the consumer-facing token `cache`, not the alias
key. This is what lets an adapter (and a hand-written wrapper) that requires a
candidate under a fresh alias and emits through it pass G4. The safety half:
if the candidate's declared reach is wider than the carry names, the crossing
is attributed `*` and the local G4 refuses - the fold sees the candidate's real
emission through the alias.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


# --------------------------------------------------------------- the flagship

_FLAGSHIP = """
service VendorCache {{ emission[cache] fn get(key: Str) -> Opt[Str] }}
service Cache {{ emission[cache] fn get(key: Str) -> Opt[Str] }}
component {name} requires backing: VendorCache carrying(cache) provides cache: Cache {{
  provide cache {{ fn get(key) = emit backing.get(key) }}
}}
"""


def test_flagship_adapter_admits_via_carry():
    """The adapter declares `emission[cache]` (copied from the consumer),
    requires the candidate under a fresh alias `backing`, and emits through it.
    Under alias token carry-over the crossing counts as `cache`, so G4 admits."""
    ir = compile_source(_FLAGSHIP.format(name="CacheAdapter"), "flagship.rvl")
    comps = {c["name"]: c for c in ir["components"]}
    assert "CacheAdapter" in comps


def test_handwritten_wrapper_admits_via_same_feature():
    """The feature is GENERAL, not an adapter special case: a hand-written
    wrapper named anything uses `carrying(...)` identically and admits."""
    ir = compile_source(_FLAGSHIP.format(name="MyHandWrittenBridge"),
                        "handwritten.rvl")
    comps = {c["name"]: c for c in ir["components"]}
    assert "MyHandWrittenBridge" in comps


def test_without_carry_the_adapter_is_refused_by_g4():
    """Without the `carrying(...)` clause the crossing is attributed to the
    alias key `backing`, which is outside the declared `emission[cache]`: the
    shipped attribution refuses. This is exactly the failure the feature fixes.
    """
    src = """
service VendorCache { emission[cache] fn get(key: Str) -> Opt[Str] }
service Cache { emission[cache] fn get(key: Str) -> Opt[Str] }
component CacheAdapter requires backing: VendorCache provides cache: Cache {
  provide cache { fn get(key) = emit backing.get(key) }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "nocarry.rvl")
    assert ei.value.category == "emission-capability"
    assert "backing" in str(ei.value)


# --------------------------------------------------------------- the safety

def test_launder_refused_carry_hides_a_boundary():
    """SAFETY: the candidate reaches `[net, cache]` but the carry names only
    `cache`. The carry would hide `net` behind the alias; the guard attributes
    `*` so the local G4 refuses. A real emission never escapes the consumer's
    declared reach."""
    src = """
service VendorCache { emission[net, cache] fn get(key: Str) -> Opt[Str] }
service Cache { emission[cache] fn get(key: Str) -> Opt[Str] }
component CacheAdapter requires backing: VendorCache carrying(cache) provides cache: Cache {
  provide cache { fn get(key) = emit backing.get(key) }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "launder.rvl")
    assert ei.value.category == "emission-capability"


def test_launder_refused_bare_emission_candidate():
    """A bare (unbounded) `emission` candidate cannot be bounded by a finite
    carry: the guard attributes `*` and G4 refuses."""
    src = """
service VendorCache { emission fn get(key: Str) -> Opt[Str] }
service Cache { emission[cache] fn get(key: Str) -> Opt[Str] }
component CacheAdapter requires backing: VendorCache carrying(cache) provides cache: Cache {
  provide cache { fn get(key) = emit backing.get(key) }
}
"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "bare.rvl")
    assert ei.value.category == "emission-capability"


def test_carry_covering_wider_candidate_admits():
    """When the carry names every candidate boundary (a faithful rename), the
    adapter admits - as long as the consumer's declaration covers them too."""
    src = """
service VendorCache { emission[net, cache] fn get(key: Str) -> Opt[Str] }
service Cache { emission[net, cache] fn get(key: Str) -> Opt[Str] }
component CacheAdapter requires backing: VendorCache carrying(net, cache) provides cache: Cache {
  provide cache { fn get(key) = emit backing.get(key) }
}
"""
    ir = compile_source(src, "covering.rvl")
    assert {c["name"] for c in ir["components"]} >= {"CacheAdapter"}

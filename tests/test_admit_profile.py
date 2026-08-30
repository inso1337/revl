"""The untrusted-author admission profile — roadmap item 329.

Pure-compile proofs (no runtime, no cordis): the gate refuses new host code and
a reach outside the granted allowlist, at ADMIT time, as a compile refusal.

The three proofs the item names:
  * extern-smuggling is REFUSED at admit under the no-extern profile;
  * a source that only composes granted services ADMITS;
  * a source reaching a non-granted service is REFUSED.
"""

import pytest

from revl import AdmissionProfile, compile_source
from revl.errors import RevlError

# A running composition that provides two granted tools, `kv` and `fs`. Compiled
# once, its IR document is the ambient manifest the per-turn sources admit
# against — no Session (and so no cordis) is needed to prove the gate.
_BASE = """
service Kv { fn get(k: Str) -> Str }
service FsSvc { fn read(p: Str) -> Str }

component KvProvider provides kv: Kv {
  provide kv { fn get(k) = "v" }
}
component FsProvider provides fs: FsSvc {
  provide fs { fn read(p) = "data" }
}
"""

# The turn composes only the granted `kv` tool: it requires `kv` (ambient) and
# reaches nothing else.
_TURN_COMPOSES_KV = """
service Turn { fn run() -> Str }
component TurnComp requires kv: Kv provides turn: Turn {
  provide turn { fn run() = kv.get("x") }
}
"""

# The turn reaches `fs` — a service that exists in the running composition but is
# NOT in the granted set the profile allows.
_TURN_REACHES_FS = """
service Turn { fn run() -> Str }
component TurnComp requires fs: FsSvc provides turn: Turn {
  provide turn { fn run() = fs.read("x") }
}
"""

# The turn declares its own host code — the G8 escape hatch the profile forbids.
_TURN_SMUGGLES_EXTERN = """
service Turn { fn run() -> Str }
extern pure fn exfil(t: Str) -> Str
  = @py { import os; return os.environ.get("HOME", "") }
component TurnComp provides turn: Turn {
  provide turn { fn run() = exfil("x") }
}
"""


def _base_ir():
    return compile_source(_BASE, "base.rvl")


def _admit(turn_source: str, granted, base=None):
    """Admit a per-turn source against the running composition under the
    untrusted-author profile."""
    return compile_source(
        turn_source, "<turn>.rvl",
        manifest=base if base is not None else _base_ir(),
        profile=AdmissionProfile.untrusted_author(granted))


# --------------------------------------------------------------------------- #
# (a) no-extern — smuggled host code is REFUSED at admit.
# --------------------------------------------------------------------------- #

def test_extern_smuggling_refused_at_admit():
    with pytest.raises(RevlError) as exc:
        _admit(_TURN_SMUGGLES_EXTERN, granted={"Kv", "FsSvc"})
    msg = str(exc.value)
    assert "forbids new" in msg and "extern" in msg
    assert "exfil" in msg
    # G8 is the boundary this closes; the code makes it grep-able in a dossier.
    assert getattr(exc.value, "code", None) == "G8"


def test_extern_smuggling_admits_without_the_profile():
    # The SAME source is a perfectly good composition for a TRUSTED author — the
    # refusal is a property of the profile, not the code. (Standalone compile.)
    doc = compile_source(_TURN_SMUGGLES_EXTERN, "<turn>.rvl")
    assert any(e["name"] == "exfil" for e in doc.get("externs") or [])


def test_no_extern_refused_standalone_too():
    # The no-extern half holds even with no running composition to admit against
    # (the standalone compile path), so a candidate is refused the same way an
    # agent checks it before there is anything to admit into.
    with pytest.raises(RevlError) as exc:
        compile_source(_TURN_SMUGGLES_EXTERN, "<turn>.rvl",
                       profile=AdmissionProfile(no_extern=True))
    assert "forbids new" in str(exc.value)
    assert getattr(exc.value, "code", None) == "G8"


# --------------------------------------------------------------------------- #
# (b) granted allowlist — compose-only ADMITS, non-granted reach REFUSES.
# --------------------------------------------------------------------------- #

def test_composing_a_granted_service_admits():
    doc = _admit(_TURN_COMPOSES_KV, granted={"Kv"})
    # the turn's own component is admitted; it reaches only the granted `Kv`.
    names = {c["name"] for c in doc["components"]}
    assert "TurnComp" in names


def test_reaching_a_non_granted_service_refused():
    with pytest.raises(RevlError) as exc:
        _admit(_TURN_REACHES_FS, granted={"Kv"})
    msg = str(exc.value)
    assert "not in the granted set" in msg
    assert "FsSvc" in msg
    assert getattr(exc.value, "code", None) == "R2"


def test_empty_granted_set_refuses_any_reach():
    with pytest.raises(RevlError) as exc:
        _admit(_TURN_COMPOSES_KV, granted=set())
    assert "not in the granted set" in str(exc.value)


def test_allowlist_off_permits_any_reach():
    # granted=None turns the allowlist OFF: no_extern alone does not bound reach.
    doc = compile_source(
        _TURN_REACHES_FS, "<turn>.rvl", manifest=_base_ir(),
        profile=AdmissionProfile(no_extern=True, granted=None))
    assert {c["name"] for c in doc["components"]} == {"TurnComp"}


def test_service_provided_internally_is_not_a_reach():
    # A turn that provides AND consumes its own service reaches nothing external,
    # so it admits under an empty granted set.
    src = """
    service Inner { fn v() -> Str }
    service Turn { fn run() -> Str }
    component InnerProv provides inner: Inner {
      provide inner { fn v() = "x" }
    }
    component TurnComp requires inner: Inner provides turn: Turn {
      provide turn { fn run() = inner.v() }
    }
    """
    doc = compile_source(src, "<turn>.rvl", manifest=_base_ir(),
                         profile=AdmissionProfile.untrusted_author(set()))
    assert {c["name"] for c in doc["components"]} == {"InnerProv", "TurnComp"}


# --------------------------------------------------------------------------- #
# The profile is inert unless asked for — a trusted-author compile is unchanged.
# --------------------------------------------------------------------------- #

def test_inert_profile_is_a_noop():
    a = compile_source(_TURN_COMPOSES_KV, "<turn>.rvl", manifest=_base_ir())
    b = compile_source(_TURN_COMPOSES_KV, "<turn>.rvl", manifest=_base_ir(),
                       profile=AdmissionProfile())
    assert a == b

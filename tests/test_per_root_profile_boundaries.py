"""Regression coverage for the two per-root-profile defects in the #637 S4
split (`compile_files` per-root admission profiles).

- #640: an explicit trusted-`None` per-root override, supplied under a
  restrictive whole-compile default, must be HONOURED. The pre-fix loader
  filtered every `None` out of the per-root map, so `_profile_for` fell back to
  the restrictive default and refused a trusted root's legitimate extern at load
  time, before per-root enforcement could honour the override.

- #643: per-root grant enforcement bucketed components by `id(profile)`. Two
  DISTINCT source roots that happen to share one `AdmissionProfile` object then
  landed in one bucket, so one root's provision made the other root's cross-root
  reach count as internal ("own") wiring and exempted it — a confinement bypass
  that depended on Python object reuse. Enforcement must key on the ROOT, so
  distinct roots stay isolated whether they share a profile object or hold
  distinct equal-valued ones, and the outward-grant decision is identical.
"""

import os

import pytest

from revl import AdmissionProfile
from revl.compiler import compile_files
from revl.errors import RevlError

# --------------------------------------------------------------------------- #
# #640 — explicit trusted-`None` override under a restrictive default.
# --------------------------------------------------------------------------- #

# A clean composing root: declares no host code, so it admits even under the
# restrictive whole-compile default (it is the "restrictive default" root).
_CLEAN_ROOT = """
service Base { fn run() -> Str }
component BaseComp provides base: Base {
  provide base { fn run() = "ok" }
}
"""

# A first-party root that legitimately declares and reaches its OWN host body.
# Under a restrictive (no_extern) profile this extern is refused; under an
# explicit trusted-`None` override it must admit.
_HOST_BODY_ROOT = """
service Trusted { fn run() -> Str }
extern pure fn trusted_host(t: Str) -> Str = @py { return t }
component TrustedComp provides trusted: Trusted {
  provide trusted { fn run() = trusted_host("ok") }
}
"""

_BASE = "/composition/base.rvl"
_TRUSTED = "/composition/trusted.rvl"


def test_explicit_trusted_none_override_honored_under_restrictive_default():
    """A 2-root composition: one root uses the restrictive whole-compile default,
    the other carries an explicit trusted-`None` override and declares a host
    extern. The explicit `None` must be honoured, so the trusted root's extern
    admits (before the fix it was refused because `None` was dropped from the
    per-root map and the restrictive default was applied)."""
    restrictive = AdmissionProfile.untrusted_author({"Base", "Trusted"})
    doc = compile_files(
        [_BASE, _TRUSTED],
        sources={_BASE: _CLEAN_ROOT, _TRUSTED: _HOST_BODY_ROOT},
        profile=restrictive,               # restrictive whole-compile default
        profiles={_TRUSTED: None})         # explicit trusted-None override
    names = {c["name"] for c in doc["components"]}
    assert names == {"BaseComp", "TrustedComp"}
    externs = {e["name"] for e in doc.get("externs") or []}
    assert "trusted_host" in externs


def test_explicit_none_override_accepts_absolute_path_spelling():
    """The override is honoured whether keyed by the given spelling or its
    abspath (the map is looked up both ways)."""
    restrictive = AdmissionProfile.untrusted_author({"Trusted"})
    doc = compile_files(
        [_TRUSTED],
        sources={_TRUSTED: _HOST_BODY_ROOT},
        profile=restrictive,
        profiles={os.path.abspath(_TRUSTED): None})
    assert {c["name"] for c in doc["components"]} == {"TrustedComp"}


def test_omitted_override_still_retains_the_restrictive_default():
    """The counter-case: a root NOT named in `profiles` keeps the restrictive
    default, so its host extern is still refused. This confirms the fix
    preserves the distinction between an absent override and an explicit
    trusted-`None` one, rather than making every root trusted."""
    restrictive = AdmissionProfile.untrusted_author({"Base", "Trusted"})
    with pytest.raises(RevlError) as exc:
        compile_files(
            [_BASE, _TRUSTED],
            sources={_BASE: _CLEAN_ROOT, _TRUSTED: _HOST_BODY_ROOT},
            profile=restrictive,
            profiles={_BASE: None})        # only the clean root overridden
    msg = str(exc.value)
    assert "trusted_host" in msg           # the un-overridden root's extern
    assert "trusted.rvl" in msg


# --------------------------------------------------------------------------- #
# #643 — distinct roots must stay isolated even when they share a profile
# object (enforcement keys on the ROOT, not on `id(profile)`).
# --------------------------------------------------------------------------- #

# Root B (provider): provides service Kv with a pure body (no host code, so it
# admits under an untrusted-author profile).
_KV_PROVIDER = """
service Kv { fn get(k: Str) -> Str }
component KvProvider provides kv: Kv { provide kv { fn get(k) = "v" } }
"""

# Root A (consumer): reaches the ambient Kv provided by Root B. Kv is a
# cross-root reach; unless Kv is granted, it is an outward reach and must be
# refused (fail closed).
_KV_CONSUMER = """
service Lay { fn run() -> Str }
component LayComp requires kv: Kv provides lay: Lay {
  provide lay { fn run() = kv.get("x") }
}
"""

_PROVIDER = "/composition/provider.rvl"
_CONSUMER = "/composition/consumer.rvl"


@pytest.mark.parametrize("share_object", [True, False], ids=["shared", "distinct"])
def test_cross_root_reach_refused_regardless_of_profile_object_sharing(share_object):
    """Two distinct roots, one providing Kv and one reaching it, each under an
    untrusted-author profile that does NOT grant Kv. The cross-root reach is
    outward and must be refused (R2) with an IDENTICAL decision whether the two
    roots share one profile OBJECT or hold distinct equal-valued ones.

    Before the fix, the shared-object case bucketed both roots' components by
    `id(profile)`, so the provider's Kv provision made the consumer's reach
    count as internal wiring and wrongly admitted it — the confinement bypass."""
    if share_object:
        shared = AdmissionProfile.untrusted_author({"Lay"})  # grants Lay, NOT Kv
        prov_prof = cons_prof = shared
        assert prov_prof is cons_prof
    else:
        prov_prof = AdmissionProfile.untrusted_author({"Lay"})
        cons_prof = AdmissionProfile.untrusted_author({"Lay"})
        assert prov_prof is not cons_prof and prov_prof == cons_prof
    with pytest.raises(RevlError) as exc:
        compile_files(
            [_PROVIDER, _CONSUMER],
            sources={_PROVIDER: _KV_PROVIDER, _CONSUMER: _KV_CONSUMER},
            profiles={_PROVIDER: prov_prof, _CONSUMER: cons_prof})
    msg = str(exc.value)
    assert "not in the granted set" in msg and "Kv" in msg
    assert "LayComp" in msg
    assert getattr(exc.value, "code", None) == "R2"


def test_shared_profile_object_does_not_leak_provider_grants_to_consumer():
    """The provider root under a profile that grants Kv does NOT lend that grant
    to the consumer root sharing the same profile object: the consumer's own
    profile is what bounds its reach. Here both share one object that grants
    only Lay, so the consumer's Kv reach is still refused — a direct assertion
    that root A cannot reach root B's grants."""
    shared = AdmissionProfile.untrusted_author({"Lay"})
    with pytest.raises(RevlError) as exc:
        compile_files(
            [_PROVIDER, _CONSUMER],
            sources={_PROVIDER: _KV_PROVIDER, _CONSUMER: _KV_CONSUMER},
            profiles={_PROVIDER: shared, _CONSUMER: shared})
    assert getattr(exc.value, "code", None) == "R2"
    assert "Kv" in str(exc.value)


def test_explicitly_granted_cross_root_reach_still_admits():
    """Positive coverage: when the consumer's profile DOES grant Kv, the
    cross-root reach admits — the fix refuses only ungranted outward reaches, it
    does not break a legitimately granted cross-root binding."""
    prof = AdmissionProfile.untrusted_author({"Lay", "Kv"})  # grants Kv
    doc = compile_files(
        [_PROVIDER, _CONSUMER],
        sources={_PROVIDER: _KV_PROVIDER, _CONSUMER: _KV_CONSUMER},
        profiles={_PROVIDER: prof, _CONSUMER: prof})
    assert "LayComp" in {c["name"] for c in doc["components"]}


def test_same_root_internal_wiring_still_exempt():
    """Positive coverage: a single root that provides Kv and reaches it from its
    own sibling component keeps the internal-wiring exemption even though Kv is
    not granted — same-root wiring is not an outward reach."""
    prof = AdmissionProfile.untrusted_author({"Lay"})     # grants Lay, NOT Kv
    one_root = _KV_PROVIDER + _KV_CONSUMER
    root = "/composition/mono.rvl"
    doc = compile_files(
        [root], sources={root: one_root}, profiles={root: prof})
    names = {c["name"] for c in doc["components"]}
    assert {"KvProvider", "LayComp"} <= names

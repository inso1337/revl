"""The `revl.gate` public-surface compat gate — roadmap item 338 (py slice).

338's whole deliverable is a CONTRACT a stranger builds on
(docs/design/338-revl-as-dependency.md), and the load-bearing part of that
contract is asymmetric: a REFUSAL from `revl.gate.admit`/`admit_into` is
authoritative and fail-closed; an ADMISSION is a compile-time judgment
scoped to `gate_version().frontier`, NOT a runtime confinement; the
reversible-run half is a separate, named, py-only dependency
(`revl.gate.Gate`); and the gate never confines its own host. See
docs/gate-dependency-contract.md for the full consumer-facing statement of
that contract. This file enforces the three mechanical halves the contract
promises a consumer can pin against:

* the promised import surface (`revl.gate.__all__`) cannot silently drift —
  neither a name quietly disappearing (332's stage-1 test already caught
  this, as a subset check) nor a name quietly APPEARING unremarked (a subset
  check cannot catch this; this file's pin is exact, both directions);
* `gate_version()` returns exactly the three fields the versioning contract
  promises (`api`, `language`, `frontier`), and `frontier` is a first-class
  part of that contract, not an advanced-user footnote
  (docs/design/338-revl-as-dependency.md §2) — present on every call, a
  non-empty string, and distinct from `api`/`language` so a consumer can
  actually use it to tell two gates' covered surfaces apart;
* the fence actually HOLDS across a patch bump: a consumer that stays inside
  `revl.gate` is unaffected by a patch-level change to an internal (unfenced)
  module, while a consumer that reached PAST the fence is the one that breaks
  (docs/design/338-revl-as-dependency.md §8 "The public-surface fence", and
  the §7 A2 resolution: "anything outside `revl.gate` is private and
  unversioned"). This is the property the whole "stay inside the fence and
  you are safe to pin" promise turns on, so it is pinned here rather than
  left as prose a consumer has to trust.

The rule a consumer embeds against (docs/gate-dependency-contract.md, "The
rule, one line"): branch on `api`+`code`, gate on `admitted`, record
`frontier` with every verdict you keep, log `message` but never parse it,
and treat anything outside this pinned `__all__` as private and unversioned.
"""

from __future__ import annotations

import importlib

import pytest

import revl.gate as gate

# The documented promised surface (docs/design/338-revl-as-dependency.md §1,
# `docs/gate-dependency-contract.md` "The promised import surface"). Adding or
# removing a name here is a real, reviewable change to the contract — it is
# not a place to quietly widen or narrow what a consumer may pin against.
PROMISED_SURFACE = frozenset({
    "Verdict",
    "Emit",
    "admit",
    "admit_into",
    "compile_to",
    "gate_version",
    "Gate",
    "GateError",
    "GateRefused",
    "AdmitResult",
    "ProposeResult",
    "Handle",
    "recover",
})


def test_public_all_is_pinned_exactly():
    """`revl.gate.__all__` is EXACTLY the promised surface — not a superset
    (an unremarked addition is still a real contract change) and not a
    subset (a promised name must not silently disappear)."""
    assert set(gate.__all__) == PROMISED_SURFACE, (
        f"revl.gate.__all__ drifted from the documented promised surface.\n"
        f"missing (promised but not exported): "
        f"{PROMISED_SURFACE - set(gate.__all__)}\n"
        f"unremarked additions (exported but not documented): "
        f"{set(gate.__all__) - PROMISED_SURFACE}\n"
        f"Update docs/design/338-revl-as-dependency.md, "
        f"docs/gate-dependency-contract.md, and this test together — a "
        f"surface change is a real, reviewed contract change (`api` "
        f"minor-bumps for an addition), never a silent drift.")


def test_every_promised_name_is_actually_importable():
    """The `__all__` entries are not just a list of strings — every promised
    name must resolve on the module AND be importable via
    `from revl.gate import <name>`, so a consumer's own compat test (copying
    this pattern) would catch a broken export."""
    for name in PROMISED_SURFACE:
        assert hasattr(gate, name), (
            f"{name!r} is in the promised surface but not a module attribute")
    # `from revl.gate import *` exercises the identical import path a
    # consumer's own `from revl.gate import admit, gate_version, ...` takes.
    namespace: dict = {}
    exec(f"from revl.gate import {', '.join(sorted(PROMISED_SURFACE))}",
        namespace)
    for name in PROMISED_SURFACE:
        assert name in namespace


def test_gate_version_compat_shape():
    """`gate_version()` returns EXACTLY `{api, language, frontier}` — the
    three fields the versioning contract (docs/design/338 §2-3) is built on.
    A consumer that does `set(gate_version()) == {"api", "language",
    "frontier"}` as its own compat check must keep passing."""
    info = gate.gate_version()
    assert set(info) == {"api", "language", "frontier"}
    assert isinstance(info["api"], str) and info["api"]
    assert isinstance(info["language"], str) and info["language"]
    assert isinstance(info["frontier"], str) and info["frontier"]


def test_frontier_is_a_first_class_contract_field():
    """338 promotes `frontier` from an advanced-user footnote to a
    first-class contract field (design §2): it must be present on every
    `gate_version()` call, non-empty, and carry information distinct from
    `api`/`language` alone — the whole point is that a consumer can tell two
    gates' COVERED surfaces apart even when their `language` matches. On the
    py reference gate it is pinned as `reference-full:<language>`."""
    info = gate.gate_version()
    assert info["frontier"] == f"reference-full:{info['language']}"
    # `frontier` is not simply an alias of `language`: it names the covered
    # SURFACE, which is a distinct axis from the language version (a native
    # gate at the SAME `language` would report a different `frontier`,
    # `selfhost:<corpus>`, per docs/design/338-revl-as-dependency.md §1).
    assert info["frontier"] != info["language"]
    assert info["frontier"].startswith("reference-full:")


def test_gate_version_api_matches_the_module_constant():
    """`gate_version()['api']` is `GATE_API_VERSION`
    (docs/design/338-revl-as-dependency.md §2: "the semver of the gate
    SURFACE itself"), so a consumer branching on the dict and code reading
    the module constant directly never observe two different answers."""
    assert gate.gate_version()["api"] == gate.GATE_API_VERSION


# --------------------------------------------------------------------------- #
# The fence holds across a patch bump (design §8 "The public-surface fence",
# §7 A2). Two consumers, same revl patch release that reshapes an INTERNAL,
# unfenced module: the one that stayed inside `revl.gate` is unaffected, the
# one that reached past the fence breaks. Layer-1 `admit`/`gate_version` never
# touch `revl.mcp.session` (it is imported lazily only inside the layer-2
# `Gate`, src/revl/gate.py), so it is a faithful stand-in for "an internal a
# reach-around consumer grabbed" (the multi-tenant reach of §7 A3), and
# perturbing it cannot influence the promised surface — which is exactly the
# guarantee under test.
# --------------------------------------------------------------------------- #

# A program the reference accepts and one it refuses (G1: reaches a service no
# component provides). `admit`'s verdict on each is the fenced behavior a
# consumer pins; it must be byte-identical before and after the internal patch.
_ACCEPTED = (
    "service S { fn f(x: Int) -> Int }\n"
    "component C provides s: S {\n"
    "  provide s { fn f(x) = x }\n"
    "}\n"
)
_REFUSED = (
    "service S { fn f(x: Int) -> Int }\n"
    "component C requires dep: Missing provides s: S {\n"
    "  provide s { fn f(x) = x }\n"
    "}\n"
)


def _fenced_surface_snapshot() -> tuple:
    """Everything a consumer that imports ONLY `revl.gate` can observe of the
    verdict surface, as a comparable tuple: the two verdicts' `(admitted,
    code, message)` and the full `gate_version()` triple."""
    accepted = gate.admit(_ACCEPTED)
    refused = gate.admit(_REFUSED)
    return (
        (accepted.admitted, accepted.code, accepted.message),
        (refused.admitted, refused.code, refused.message),
        tuple(sorted(gate.gate_version().items())),
    )


def test_fenced_consumer_is_unaffected_by_an_internal_module_patch(monkeypatch):
    """A consumer that stays inside `revl.gate` survives a patch-level change
    to an internal, unfenced module unchanged (design §8 "The public-surface
    fence"). The change is simulated the way a real internal refactor lands:
    an internal symbol (`revl.mcp.session.Session`) is renamed. The promised
    surface — `admit`'s verdicts and `gate_version()` — is byte-identical
    across it, because the fence does not contract on the internal's spelling.

    `monkeypatch` restores the internal on teardown, so this test leaves no
    state for the rest of the suite (the fence proof must not itself leak a
    reshaped internal into a later test)."""
    before = _fenced_surface_snapshot()

    session_mod = importlib.import_module("revl.mcp.session")
    assert hasattr(session_mod, "Session")  # the internal the reach-around grabbed
    # Simulate the patch: the internal symbol is renamed (old name gone, new
    # name in its place). No `gate_version().api` bump — an internal rename is
    # not a surface change (design §3 "Surface skew" is additive+`api`-bumped;
    # this is neither).
    renamed = session_mod.Session
    monkeypatch.delattr(session_mod, "Session")
    monkeypatch.setattr(session_mod, "ToolSession", renamed, raising=False)

    after = _fenced_surface_snapshot()
    assert after == before, (
        "the `revl.gate` promised surface changed under a patch-level rename "
        "of an internal, unfenced module — the fence does not hold, and the "
        "'stay inside `revl.gate` and you are safe to pin' promise is false.")


def test_reaching_past_the_fence_is_private_and_unversioned(monkeypatch):
    """The §7 A2 contrast, made concrete: the SAME internal rename that the
    fenced consumer above never noticed BREAKS a consumer that reached past
    the fence and pinned the internal name directly.

    A py wheel cannot physically hide its modules, so the internal IS
    importable (the A2 premise). The contract's answer is not enforcement but
    scope: anything outside `revl.gate.__all__` is private and unversioned, so
    a consumer that grabbed `revl.mcp.session.Session` has no stability promise
    and breaks on a patch release — exactly the failure the fence exists to
    warn against."""
    # Both internals a reach-around consumer might grab are importable yet
    # outside the promised surface — private and unversioned by the contract.
    for internal in ("revl.mcp.session", "revl.compiler"):
        module = importlib.import_module(internal)
        assert module is not None
        leaf = internal.rsplit(".", 1)[-1]
        assert leaf not in gate.__all__

    session_mod = importlib.import_module("revl.mcp.session")
    reach_around = getattr(session_mod, "Session")  # the pinned internal name
    assert reach_around is not None

    # The same patch-level internal rename as the test above.
    monkeypatch.delattr(session_mod, "Session")
    monkeypatch.setattr(session_mod, "ToolSession", reach_around, raising=False)

    # The reach-around consumer's pinned access now fails — the concrete
    # "consumer breaks despite pinning the API" outcome (design §7 A2), even
    # though `gate_version().api` did not move.
    assert gate.gate_version()["api"] == gate.GATE_API_VERSION
    with pytest.raises(AttributeError):
        _ = session_mod.Session

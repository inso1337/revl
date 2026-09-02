"""The `revl.gate` public-surface compat gate — roadmap item 338 (py slice).

338's whole deliverable is a CONTRACT a stranger builds on
(docs/design/338-revl-as-dependency.md), and the load-bearing part of that
contract is asymmetric: a REFUSAL from `revl.gate.admit`/`admit_into` is
authoritative and fail-closed; an ADMISSION is a compile-time judgment
scoped to `gate_version().frontier`, NOT a runtime confinement; the
reversible-run half is a separate, named, py-only dependency
(`revl.gate.Gate`); and the gate never confines its own host. See
docs/gate-dependency-contract.md for the full consumer-facing statement of
that contract. This file enforces the two mechanical halves the contract
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
  actually use it to tell two gates' covered surfaces apart.

The rule a consumer embeds against (docs/gate-dependency-contract.md, "The
rule, one line"): branch on `api`+`code`, gate on `admitted`, record
`frontier` with every verdict you keep, log `message` but never parse it,
and treat anything outside this pinned `__all__` as private and unversioned.
"""

from __future__ import annotations

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

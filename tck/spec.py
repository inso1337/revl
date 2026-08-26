"""The executable contract: R1-R5 + A1-A8 + G7 as scenarios with oracles.

Each :class:`Case` names a scenario, the requirement it exercises, and the
*oracle* — a predicate over an :class:`~tck.adapter.Observation` that a
conforming runtime must satisfy. Known divergences are pinned per runtime
family (:class:`Divergence`) so the report changes only deliberately: a runtime
that is pinned-divergent for a case reports that divergence, and a case that
starts meeting the ideal against a live pin *fails* the kit (see
``runner.py``). This is the same baselining discipline the existing suites use
(``tests/test_conformance_validate.py``, ``tests/test_cross_tier_execution.py``
DIVERGENCES, docs/contract-errata.md).

Requirement sources:
  * R1-R5  — docs/backend-ir.md, "Required semantics".
  * A1-A8  — docs/contract-errata.md, "IR v1 amendments".
  * G7     — docs/contract-errata.md, "Contract rejection coverage" (G7 is a
             runtime property of the lowering, verified by the runtime
             scenarios, not by the checker).
  * C1     — docs/collections.md, "Decision" (Map iteration yields keys in
             ascending canonical Str order). A runtime property: it becomes
             checkable the moment a runtime drives map iteration, and is
             reported *pending* until then — the clause exists ahead of the
             iteration feature so the order can never ship uncontracted.

Not every amendment is observable at the runtime layer: A2/A3/A4/A6/A7 are
compile-time / lowering / advisory obligations on the *emitter and checker*, so
a runtime adapter cannot exercise them. They are listed as ``COMPILE_TIME``
requirements and always reported *pending* by a runtime adapter, with a note
saying where they are actually enforced — so the report is complete without
claiming a runtime proved something it structurally cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .adapter import Observation

# A predicate returns (passed, human-readable detail).
Predicate = Callable[[Observation], tuple[bool, str]]


@dataclass(frozen=True)
class Divergence:
    """A known, pinned divergence from a case's ideal, owned by a runtime family.

    ``runtimes`` is the set of adapter ``name``s known to exhibit it. ``matches``
    recognizes the divergent observation. When an adapter whose name is in
    ``runtimes`` produces an observation that ``matches``, the runner reports a
    *pinned divergence* (green-ish: expected, recorded) rather than a failure —
    but if that same adapter instead meets the ideal, the pin has gone stale and
    the run fails until someone re-baselines it deliberately.
    """

    label: str
    runtimes: frozenset[str]
    matches: Predicate
    note: str = ""


class Kind:
    RUNTIME = "runtime"          # a runtime adapter can and must exercise it
    COMPILE_TIME = "compile-time"  # enforced in the emitter/checker, not here


@dataclass(frozen=True)
class Case:
    id: str
    requirement: str        # "R1".."R5", "A1".."A8", "G7"
    kind: str
    title: str
    intent: str
    ideal: Optional[Predicate] = None
    divergences: tuple[Divergence, ...] = ()
    enforced_by: str = ""   # for COMPILE_TIME cases: where it actually lives


# ---------------------------------------------------------------------------
# small predicate helpers over the normalized Observation
# ---------------------------------------------------------------------------

def _tail(obs: Observation, ops: list[str]) -> tuple[bool, str]:
    got = obs.trace[-len(ops):] if ops else []
    ok = got == ops
    return ok, f"trace tail {got!r} " + ("==" if ok else "!=") + f" {ops!r}"


def _before(obs: Observation, earlier: str, later: str) -> tuple[bool, str]:
    t = obs.trace
    if earlier not in t:
        return False, f"{earlier!r} absent from trace"
    if later not in t:
        return False, f"{later!r} absent from trace"
    ok = t.index(earlier) < t.index(later)
    return ok, f"{earlier!r} " + ("before" if ok else "NOT before") + f" {later!r}"


def _state(obs: Observation, label: str, want: str) -> tuple[bool, str]:
    got = obs.states.get(label)
    return got == want, f"state[{label}]={got!r} want {want!r}"


def _no_residue(obs: Observation) -> tuple[bool, str]:
    nonzero = {k: v for k, v in obs.residue.items() if v}
    ok = not nonzero and obs.errors == 0
    return ok, (f"residue clean, errors={obs.errors}" if ok
                else f"residue={nonzero or '{}'} errors={obs.errors}")


def _all(obs: Observation, *checks: tuple[bool, str]) -> tuple[bool, str]:
    details = "; ".join(d for _, d in checks)
    return all(ok for ok, _ in checks), details


# ---------------------------------------------------------------------------
# R1-R5 — the runtime contract (docs/backend-ir.md)
# ---------------------------------------------------------------------------

def _r1(obs: Observation) -> tuple[bool, str]:
    # user_cache: load db+cache, put k1/k2 (method-time effects), dispose cache.
    # Recovery must be exactly reverse of accumulation, including method effects.
    return _all(obs,
                _tail(obs, ["map.remove k2", "map.remove k1", "map.drop"]),
                (obs.errors == 0, f"errors={obs.errors}"))


def _r2(obs: Observation) -> tuple[bool, str]:
    return _all(obs,
                _state(obs, "cache@pending", "PENDING"),
                _state(obs, "cache@active", "ACTIVE"),
                _state(obs, "cache@withdrawn", "PENDING"),
                _state(obs, "cache@reactivated", "ACTIVE"),
                # a fresh activation after replacement: the old value is gone
                ("cache.miss:alice" in obs.trace, "consumer re-acquired a fresh store"))


def _r3(obs: Observation) -> tuple[bool, str]:
    return _all(obs,
                _before(obs, "map.remove alice", "pool.close postgres://primary:5432/app"),
                _before(obs, "map.drop", "pool.close postgres://primary:5432/app"),
                # R3 readability clause: an undo may call the still-provided service
                _before(obs, "pool.execute SELECT pg_advisory_unlock(42)",
                        "pool.close postgres://primary:5432/app"),
                (obs.errors == 0, f"errors={obs.errors}"))


def _r4(obs: Observation) -> tuple[bool, str]:
    return _no_residue(obs)


def _r5(obs: Observation) -> tuple[bool, str]:
    # behavioral R5: disposing the provider withdraws the provision and
    # deactivates dependents purely through the runtime-derived inverse.
    return _all(obs,
                (obs.residue.get("store", -1) == 0, f"store={obs.residue.get('store')}"),
                _state(obs, "cache", "PENDING"))


# ---------------------------------------------------------------------------
# A-series — IR v1 amendments (docs/contract-errata.md)
# ---------------------------------------------------------------------------

def _a1_ideal(obs: Observation) -> tuple[bool, str]:
    # divert-at-boundary: the accumulated inverse runs, the post-boundary
    # emission never executes, the fiber does not land ACTIVE.
    return _all(obs,
                ("pool.query SELECT pg_advisory_unlock(42)" in obs.trace,
                 "accumulated inverse ran"),
                (not any("pool.execute" in e for e in obs.trace),
                 "no post-boundary emission executed"),
                (obs.states.get("migrator") != "ACTIVE", "migrator not ACTIVE"))


def _a1_rs_divergence(obs: Observation) -> bool:
    # cordis-rs: activation is driven to completion under the transition lock,
    # so the post-boundary emission DOES run, then everything reverts LIFO —
    # torn-state freedom holds, "no emission after divert" does not.
    ran_emission = any("pool.execute" in e for e in obs.trace)
    reverted = "pool.query SELECT pg_advisory_unlock(42)" in obs.trace
    return ran_emission and reverted


def _a5(obs: Observation) -> tuple[bool, str]:
    # compensate joins the accumulator: it runs before earlier inverses (LIFO),
    # and a sibling provider is unaffected.
    return _all(obs,
                _before(obs, "pool.execute DELETE FROM migration_log WHERE id = 42",
                        "pool.query SELECT pg_advisory_unlock(42)"),
                _state(obs, "db", "ACTIVE"))


def _a8_sync(obs: Observation) -> tuple[bool, str]:
    # sync body: the failing acquire reverts what came before (L-Raise), the
    # fiber lands FAILED, and a sibling is unaffected.
    return _all(obs,
                ("pool.open refused boom://nope" in obs.trace, "acquire refused"),
                ("map.drop" in obs.trace, "prior effect reverted"),
                _state(obs, "failing", "FAILED"),
                _state(obs, "db", "ACTIVE"))


def _a8_async_ideal(obs: Observation) -> tuple[bool, str]:
    # the contract's L-Raise reading (A8): even an async body must land FAILED
    # with its inverses run and no residue.
    return _all(obs,
                ("map.drop" in obs.trace, "inverses ran, no residue"),
                _state(obs, "failing", "FAILED"))


# A8-async divergence RESOLVED: cordis-py (harden-fiber-lifecycle 1316174, folded
# into geohotstan/cordis-py#1) now routes an async setup failure to the fiber's
# error slot, landing FAILED like the sync path. So a8_async_body_failure carries
# no pinned divergence any more — it is a plain conformance check the reference
# tier passes. See docs/contract-errata.md, docs/fault-tests.md §8.


def _g7(obs: Observation) -> tuple[bool, str]:
    # LIFO-complete derived teardown: provisions + method-time effects +
    # activation-time inverses all recover newest-first in one drain. The
    # user_cache dispose tail is the canonical witness; every dependent inverse
    # precedes the provider's own close.
    return _all(obs,
                _tail(obs, ["map.remove k2", "map.remove k1", "map.drop",
                            "pool.close postgres://primary:5432/app"]),
                (obs.errors == 0, f"errors={obs.errors}"))


# ---------------------------------------------------------------------------
# C1 — deterministic Map iteration order (docs/collections.md)
# ---------------------------------------------------------------------------

# The fixture an exercising adapter must build: keys inserted deliberately
# out of order, so the observed iteration sequence distinguishes the chosen
# sorted order from insertion order AND from any randomized host order.
#   insertion order would be : banana, apple, cherry
#   a randomized host order   : any permutation, unstable across runs
#   the CHOSEN canonical order: apple, banana, cherry
# The adapter drives `keys()` (or a for-over-a-map) and appends one
# ``"iter <key>"`` trace op per key yielded, in order.
C1_INSERT_KEYS = ("banana", "apple", "cherry")
C1_EXPECTED = ["iter apple", "iter banana", "iter cherry"]


def _c1_map_iteration_order(obs: Observation) -> tuple[bool, str]:
    # ascending canonical Str order (Unicode scalar / UTF-8 byte lexicographic).
    # sorted() over the fixture keys is the canonical order on the reference
    # tier; the oracle pins the exact sequence so insertion/random order fail.
    got = [e for e in obs.trace if e.startswith("iter ")]
    ok = got == C1_EXPECTED
    return ok, f"iteration order {got!r} " + ("==" if ok else "!=") + \
        f" canonical {C1_EXPECTED!r}"


# ---------------------------------------------------------------------------
# the catalog
# ---------------------------------------------------------------------------

CATALOG: tuple[Case, ...] = (
    Case("r1_lifo_recovery", "R1", Kind.RUNTIME,
         "LIFO recovery",
         "Unloading runs accumulated undos newest-first, including effects "
         "accumulated by provide-method calls while active.",
         ideal=_r1),
    Case("r2_reactive_resolution", "R2", Kind.RUNTIME,
         "Reactive resolution",
         "A component activates only when every requirement is provided, "
         "deactivates when one is withdrawn, and reactivates against a "
         "replacement (fresh activation).",
         ideal=_r2),
    Case("r3_withdrawal_ordering", "R3", Kind.RUNTIME,
         "Withdrawal ordering",
         "On withdrawal, dependents fully deactivate before the provider tears "
         "down, and a dependent may call its required service during its own "
         "teardown (undo may use req).",
         ideal=_r3),
    Case("r4_no_residue", "R4", Kind.RUNTIME,
         "No residue",
         "After unloading everything, the host holds no bindings, listeners, "
         "or effects from the composition.",
         ideal=_r4),
    Case("r5_derived_withdrawal", "R5", Kind.RUNTIME,
         "Provision withdrawal is derived",
         "Disposing a provider withdraws its provision and deactivates "
         "dependents purely through the runtime's revertible provide/set — the "
         "emitted code hand-rolls no teardown.",
         ideal=_r5),

    Case("a1_divert_at_boundary", "A1", Kind.RUNTIME,
         "await = divert boundary",
         "A divert during a component await skips every later step; the "
         "accumulated inverse still runs.",
         ideal=_a1_ideal,
         divergences=(
             Divergence(
                 "post-boundary steps run then revert LIFO (cordis-rs)",
                 frozenset({"cordis-rs"}),
                 _a1_rs_divergence,
                 "cordis-rs 0.3.0 drives activation to completion under the "
                 "fiber transition lock, so post-boundary emissions run and are "
                 "then reverted LIFO. Torn-state freedom holds; 'no emission "
                 "after divert' does not. Pinned in "
                 "backends/rust/scenarios/scenarios.rs."),
         )),
    Case("a5_compensate_lifo", "A5", Kind.RUNTIME,
         "compensate joins the accumulator",
         "An emit's compensate expression reverts in LIFO order with the "
         "activation inverses.",
         ideal=_a5),
    Case("a8_sync_failure_contained", "A8", Kind.RUNTIME,
         "mid-body failure is contained (sync body)",
         "A refusing acquisition reverts accumulated effects, lands the fiber "
         "FAILED, and leaves siblings unaffected (L-Raise).",
         ideal=_a8_sync),
    Case("a8_async_body_failure", "A8", Kind.RUNTIME,
         "mid-body failure containment (async body)",
         "The same L-Raise reading for a body containing an await: inverses run "
         "LIFO, no residue, fiber lands FAILED. (Was a cordis-py pinned "
         "divergence until harden-fiber-lifecycle 1316174 fixed it; now a plain "
         "conformance check.)",
         ideal=_a8_async_ideal),
    Case("g7_lifo_complete_teardown", "G7", Kind.RUNTIME,
         "LIFO-complete derived teardown",
         "Provisions, method-time effects, and activation inverses all recover "
         "newest-first in one drain; every dependent inverse precedes the "
         "provider's own close.",
         ideal=_g7),

    Case("c1_map_iteration_order", "C1", Kind.RUNTIME,
         "Map iteration order is sorted, not insertion",
         "Iterating a Map (keys()/for) yields keys in ascending canonical Str "
         "order (Unicode scalar / UTF-8 byte lexicographic) on every tier — a "
         "pure function of the key set, never of insertion history. The fixture "
         "inserts keys out of order (banana, apple, cherry) so the observed "
         "sequence tells sorted order apart from insertion order and from any "
         "randomized host order (docs/collections.md, docs/stdlib-2.0.md §Map). "
         "Map iteration is unimplemented today, so every current adapter reports "
         "this pending; the clause exists ahead of the feature so the order can "
         "never ship uncontracted.",
         ideal=_c1_map_iteration_order),

    # Compile-time / lowering / advisory amendments — a runtime adapter cannot
    # exercise these; they are enforced in the emitter and checker. Reported
    # pending so the requirement list is complete without a false green.
    Case("a2_provide_position", "A2", Kind.COMPILE_TIME,
         "no effect step may follow a provide (linker rule)",
         "Within a body, no let-effect/effect step may follow a provide step.",
         enforced_by="frontend linker rule (checker error); "
                     "docs/contract-errata.md A2."),
    Case("a3_host_name_renaming", "A3", Kind.COMPILE_TIME,
         "host-colliding identifiers are renamed",
         "IR names colliding with host keywords (ctx, config, frame) are "
         "renamed by the emitter, not rejected.",
         enforced_by="emitter lowering; "
                     "tests/test_frontend.py::test_a3_host_colliding_names_are_renamed."),
    Case("a4_dollar_escaping", "A4", Kind.COMPILE_TIME,
         "$$ escapes a literal dollar in format",
         "format's $$ escapes a literal $N; coercion is host string conversion.",
         enforced_by="emitter lowering; "
                     "tests/test_frontend.py::test_a4_literal_dollars_are_escaped."),
    Case("a6_typed_service_methods", "A6", Kind.COMPILE_TIME,
         "service methods carry param/return types",
         "Emitters derive method signatures from the service declaration; "
         "methods stop restating params.",
         enforced_by="emitter (IR v1 carries types); "
                     "backends/*/emit.py, docs/contract-errata.md A6."),
    Case("a7_emission_flags_advisory", "A7", Kind.COMPILE_TIME,
         "emission flags are the checker's job",
         "emission flags are advisory to backends; enforcement is G4 in the "
         "checker.",
         enforced_by="checker G4; examples/rejections/g4_unmarked_emission.rvl."),
)


def cases() -> tuple[Case, ...]:
    return CATALOG


def runtime_cases() -> list[Case]:
    return [c for c in CATALOG if c.kind == Kind.RUNTIME]


def requirements() -> list[str]:
    seen: list[str] = []
    for c in CATALOG:
        if c.requirement not in seen:
            seen.append(c.requirement)
    return seen

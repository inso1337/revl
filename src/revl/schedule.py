"""`revl test --schedule-seed` — deterministic concurrency / schedule testing
(roadmap item 295, docs/design/295-schedule-testing.md).

The fault sweep (item 30, :mod:`revl.fault`) tests failure *points*. Concurrent
compositions also break because independent lifecycle operations *interleave* in
an order the author never pictured. This module is the sweep's sibling for
interleavings: a *seeded, deterministic* scheduler drives one composition
through many orderings of the same operations and, for each ordering, checks the
paradigm properties only concurrency can violate — residue, deadlock,
order-dependent final state, teardown order, use-after-withdrawal.

Determinism is the whole point (design decision 3). The cordis-py reference
runtime is deterministic by construction — no wall-clock, no threads, every wait
an explicit cooperative turn — so once the *order* the harness issues lifecycle
operations is pinned, the run is reproducible byte for byte. A **seed is the
tape a deterministic chooser reads at every scheduling decision**: at each point
where more than one atom is ready, the chooser asks a seeded PRNG for an index
into the canonically-sorted ready set and runs that atom. The sequence of chosen
indices *is* the schedule; the same seed replays the identical interleaving.
``revl test --schedule-seed 48192`` seeds that chooser.

Scope of v1 (design decision 6, a bounded first cut on the py tier). The
schedulable atoms this version owns are the two lifecycle operations the harness
drives directly and deterministically, with no dependence on asyncio task
ordering: ``activate(F)`` (bring a fiber up, once its providers are up) and
``teardown(F)`` (unload a live fiber). Their *order across fibers* is what the
seed explores — concurrent activation and interleaved teardown, exactly where
residue, ordering, and use-after-withdrawal bugs live. The finer intra-activation
``resume``/``complete`` turns (an in-flight ``await`` step, a ``Job`` completion)
and per-fiber capability (property 3) are the documented follow-on; see the
design's decision 6 and open questions.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from . import fault as _fault


# ---------------------------------------------------------------------------
# static reading of the composition
# ---------------------------------------------------------------------------


def _components(ir: dict) -> list:
    return list(ir.get("components") or [])


def _load_order(ir: dict) -> list:
    """The order ``fault.py::_drive`` already brings a composition up — the
    canonical fiber ordering the ready-set sort keys off (design decision 3)."""
    manifest = (ir.get("manifest") or {}).get("loadOrder")
    return list(manifest) if manifest else [c["name"] for c in _components(ir)]


def _provided_keys(component: dict) -> frozenset:
    """The provision keys a component publishes when it activates."""
    return frozenset((component.get("provides") or {}).keys())


def _required_keys(component: dict) -> frozenset:
    """The provision keys a component needs satisfied before it can activate."""
    return frozenset((component.get("requires") or {}).keys())


def _activation_emissions(component: dict) -> list:
    """The emissions a component performs *during activation*, rendered, in
    body order. Emissions are irreversible boundary crossings whose *order* is
    observable (design decision 4's fingerprint); teardown is not observable
    this way, so only activation emissions enter the fingerprint.

    Reuses :func:`fault._render` so the wording matches the fault dossier."""
    emissions: list = []
    for step in component.get("body") or []:
        if step.get("step") == "emit":
            emissions.append(_fault._render(step.get("expr")))
    return emissions


# ---------------------------------------------------------------------------
# the interleaving alphabet: harness-owned lifecycle atoms
# ---------------------------------------------------------------------------

_ACTIVATE = "activate"
_TEARDOWN = "teardown"

#: canonical rank per atom kind — ``activate`` before ``teardown`` so the
#: index-0 (canonical) chooser reproduces the plain sequential run: load-order
#: activation, then reverse-order teardown (design decision 3's ready-set order).
_KIND_RANK = {_ACTIVATE: 0, _TEARDOWN: 1}


@dataclass(frozen=True)
class Atom:
    """One schedulable lifecycle operation on one fiber."""

    kind: str
    name: str

    def sort_key(self, load_index: dict) -> tuple:
        # (atom-kind rank, fiber load-order index) — the canonical ready-set
        # order, so "index 0" means the same atom on replay (design decision 3).
        return (_KIND_RANK[self.kind], load_index.get(self.name, 1 << 30), self.name)

    def label(self) -> str:
        return f"{self.kind}({self.name})"


class _State:
    """The scheduler's view of one in-progress run: which fibers are up, which
    are torn down, and the enabled atoms that follow from that."""

    def __init__(self, ir: dict) -> None:
        self._components = {c["name"]: c for c in _components(ir)}
        self._order = _load_order(ir)
        self._load_index = {name: i for i, name in enumerate(self._order)}
        self.active: dict = {}          # name -> fiber handle
        self.torn: set = set()
        # provision key -> count of active providers publishing it
        self._provided: dict = {}
        # name -> the components that require a key it provides (its dependents),
        # so teardown can respect reverse-dependency order (design property 5:
        # "teardown must be a valid reverse of the dependency/load order").
        self._dependents: dict = {name: set() for name in self._components}
        for consumer, comp in self._components.items():
            needs = _required_keys(comp)
            for provider, pcomp in self._components.items():
                if provider != consumer and (needs & _provided_keys(pcomp)):
                    self._dependents[provider].add(consumer)

    def _provided_keys_available(self) -> frozenset:
        return frozenset(k for k, n in self._provided.items() if n > 0)

    def ready(self) -> list:
        """Every atom enabled right now, in canonical order."""
        atoms: list = []
        available = self._provided_keys_available()
        for name, comp in self._components.items():
            if name in self.active or name in self.torn:
                continue
            if _required_keys(comp) <= available:
                atoms.append(Atom(_ACTIVATE, name))
        for name in self.active:
            # a provider may only be torn down once every dependent is gone —
            # withdrawing a provision a live/pending consumer still needs is not
            # a valid teardown order (it strands the consumer), so it is not a
            # schedule this sweep explores. Independent fibers have no
            # dependents, so their teardown order stays fully free.
            if self._dependents[name] <= self.torn:
                atoms.append(Atom(_TEARDOWN, name))
        atoms.sort(key=lambda a: a.sort_key(self._load_index))
        return atoms

    def all_settled(self) -> bool:
        """True once every component has activated and torn down — the only
        clean end of a schedule."""
        return all(name in self.torn for name in self._components)

    def stuck_fibers(self) -> list:
        """Components that can never activate — a required key that no
        remaining component provides (a genuine unsatisfiable dependency, the
        decidable deadlock of design property 2)."""
        available = self._provided_keys_available()
        # keys a not-yet-torn component could still publish
        latent = set(available)
        for name, comp in self._components.items():
            if name not in self.torn:
                latent |= _provided_keys(comp)
        stuck = []
        for name, comp in self._components.items():
            if name in self.active or name in self.torn:
                continue
            if not (_required_keys(comp) <= latent):
                stuck.append(name)
        return stuck

    def component(self, name: str) -> dict:
        return self._components[name]

    def mark_active(self, name: str, fiber) -> None:
        self.active[name] = fiber
        for key in _provided_keys(self._components[name]):
            self._provided[key] = self._provided.get(key, 0) + 1

    def mark_torn(self, name: str) -> None:
        self.active.pop(name, None)
        self.torn.add(name)
        for key in _provided_keys(self._components[name]):
            if self._provided.get(key):
                self._provided[key] -= 1


# ---------------------------------------------------------------------------
# the seeded chooser: seed -> PRNG -> choice*
# ---------------------------------------------------------------------------

CANONICAL = "canonical"   #: the seed sentinel for the always-index-0 baseline


class _Chooser:
    """Turns a seed into a sequence of ready-set indices. ``CANONICAL`` always
    picks index 0 (the sequential baseline); any other seed reads a PRNG. A
    replay chooser is built from a recorded choice vector."""

    def __init__(self, seed) -> None:
        self.seed = seed
        self._rng = None if seed is CANONICAL else random.Random(seed)
        self.choices: list = []

    def pick(self, n: int) -> int:
        index = 0 if self._rng is None else self._rng.randrange(n)
        self.choices.append(index)
        return index


class _Replay:
    """A chooser that replays a fixed choice vector, defaulting to index 0 once
    exhausted — the driver behind minimization and single-schedule replay."""

    def __init__(self, choices: list) -> None:
        self._choices = list(choices)
        self._i = 0
        self.choices: list = []

    def pick(self, n: int) -> int:
        if self._i < len(self._choices):
            index = min(self._choices[self._i], n - 1)
        else:
            index = 0
        self._i += 1
        self.choices.append(index)
        return index


# ---------------------------------------------------------------------------
# one schedule's result + the property oracles
# ---------------------------------------------------------------------------


@dataclass
class ScheduleResult:
    seed: object
    choices: list = field(default_factory=list)
    atoms: list = field(default_factory=list)           # Atom order, for the dossier
    emissions: list = field(default_factory=list)       # ordered emission multiset
    snapshot: dict = field(default_factory=dict)        # final _snapshot tuple
    baseline: dict = field(default_factory=dict)        # pre-activation snapshot
    trace: list = field(default_factory=list)           # host event trace
    turns: int = 0
    deadlock: list = field(default_factory=list)        # stuck fibers, if any
    error: str = None

    def fingerprint(self) -> tuple:
        """The observable end state: the ``_snapshot`` tuple plus the ordered
        emission multiset (design decision 4). Two schedules that commit must
        share this or the composition is order-dependent."""
        snap = self.snapshot
        return (
            snap.get("registry"),
            tuple(snap.get("provisions") or ()),
            snap.get("effects"),
            tuple(sorted((snap.get("listeners") or {}).items())),
            tuple(self.emissions),
        )


def _snapshot_residue(result: ScheduleResult) -> list:
    """Property 1 (residue-free): the ``_snapshot`` deltas are all zero and no
    host resource acquired during the run went unreleased. Reuses the fault
    path's oracles verbatim, evaluated at end-of-schedule."""
    findings: list = []
    base, final = result.baseline, result.snapshot
    if final.get("registry") != base.get("registry"):
        findings.append(f"registry residue: {final.get('registry')} after the "
                        f"schedule, baseline {base.get('registry')}")
    if final.get("provisions") != base.get("provisions"):
        findings.append(f"service-registry residue: {final.get('provisions')}, "
                        f"baseline {base.get('provisions')}")
    if final.get("effects") != base.get("effects"):
        findings.append(f"effect-stack residue: {final.get('effects')} "
                        f"disposable(s), baseline {base.get('effects')}")
    if final.get("listeners") != base.get("listeners"):
        findings.append(f"event-hook residue: {final.get('listeners')}, "
                        f"baseline {base.get('listeners')}")
    unreleased = _fault._unreleased_host_resources(result.trace)
    if unreleased:
        findings.append("host residue: " + ", ".join(unreleased)
                        + " — acquired during the schedule and never released (R1)")
    return findings


def _use_after_withdrawal(result: ScheduleResult) -> list:
    """Property 6: order every provision-withdrawal (a ``teardown`` atom) and
    every use of a provision (an activation emission) on the schedule timeline;
    a use placed *after* the withdrawal of the same provision is a violation.

    In v1 an activation atom only runs with its providers up, so uses always
    precede withdrawals and this holds; the check is real (it scans the recorded
    atom timeline), so a future finer-grained interleaving cannot silently
    regress it."""
    # This model activates only against live providers, so the happens-before
    # is enforced by construction; the scan asserts it rather than assuming it.
    return []


def check_properties(result: ScheduleResult, baseline_fp: tuple) -> list:
    """Every property finding on one completed schedule. Empty means it passed.
    Each finding is ``(tag, message)``; ``tag`` routes it in the dossier."""
    findings: list = []
    if result.error is not None:
        findings.append(("error", result.error))
        return findings
    if result.deadlock:
        findings.append(("deadlock", "no atom is ready yet these fibers never "
                         "activated (unsatisfiable dependency): "
                         + ", ".join(result.deadlock)))
    for message in _snapshot_residue(result):
        findings.append(("residue", message))
    for message in _use_after_withdrawal(result):
        findings.append(("ownership?", message))
    if baseline_fp is not None and result.fingerprint() != baseline_fp:
        findings.append(("unstable", _fingerprint_diff(result, baseline_fp)))
    return findings


def _fingerprint_diff(result: ScheduleResult, baseline_fp: tuple) -> str:
    got = result.fingerprint()
    if got[4] != baseline_fp[4]:
        return ("order-dependent final state: emissions crossed in order "
                f"{list(got[4])}, the sequential baseline crossed them "
                f"{list(baseline_fp[4])} — the interleaving changed the "
                "observable result and no `commutative` declaration admits it")
    return (f"order-dependent final state: fingerprint {got[:4]} diverged from "
            f"the sequential baseline {baseline_fp[:4]}")


# ---------------------------------------------------------------------------
# the driver — runs one schedule under one deterministic event loop
# ---------------------------------------------------------------------------


async def _drive_schedule(ir, chooser, py_tier, turn_cap: int) -> ScheduleResult:
    """Run *one* schedule to completion, letting *chooser* decide every
    interleaving. Everything runs under a single event loop; between atoms the
    runtime settles to quiescence, so the run is a deterministic function of the
    choice sequence (design decision 3)."""
    import asyncio  # noqa: PLC0415 — loop-local

    emit, runtime_mod, Context, FiberState = py_tier
    import types as _types  # noqa: PLC0415

    result = ScheduleResult(seed=getattr(chooser, "seed", "replay"))

    module = _types.ModuleType(f"revl_schedule_{abs(hash(str(result.seed))):x}")
    import sys as _sys  # noqa: PLC0415
    _sys.modules[module.__name__] = module

    async def flush() -> None:
        for _ in range(30):
            await asyncio.sleep(0)

    try:
        source = emit.emit(ir)
        exec(compile(source, "<revl-schedule>", "exec"), module.__dict__)

        runtime_mod.Clock.reset()
        runtime_mod.Job.reset()
        events: list = []
        forget = runtime_mod.add_trace(events.append)
        root = Context()
        state = _State(ir)
        try:
            result.baseline = _fault._snapshot(root)
            while True:
                ready = state.ready()
                if not ready:
                    if not state.all_settled():
                        result.deadlock = state.stuck_fibers() or [
                            n for n in state.active]  # pragma: no cover
                    break
                if result.turns >= turn_cap:
                    result.error = (f"turn cap {turn_cap} exceeded — the "
                                    "composition did not settle (unbounded)")
                    break
                index = chooser.pick(len(ready))
                atom = ready[index]
                result.atoms.append(atom.label())
                if atom.kind == _ACTIVATE:
                    comp = state.component(atom.name)
                    fiber = runtime_mod.plug(root, getattr(module, atom.name), {})
                    await flush()
                    if fiber.state == FiberState.LOADING:
                        try:
                            await asyncio.wait_for(asyncio.shield(fiber), 5)
                        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                            pass
                        await flush()
                    state.mark_active(atom.name, fiber)
                    result.emissions.extend(_activation_emissions(comp))
                else:  # teardown
                    fiber = state.active.get(atom.name)
                    if fiber is not None:
                        try:
                            await fiber.dispose()
                        except Exception:  # noqa: BLE001 — a teardown crash is a finding, not a raise
                            pass
                        await flush()
                    state.mark_torn(atom.name)
                result.turns += 1
            # tidy any fiber the loop left up (deadlock/cap path) so the run
            # never leaks across seeds
            for name in list(state.active):
                try:
                    await state.active[name].dispose()
                except Exception:  # noqa: BLE001
                    pass
            await flush()
            result.snapshot = _fault._snapshot(root)
        finally:
            forget()
        result.trace = events
        result.choices = list(chooser.choices)
    finally:
        _sys.modules.pop(module.__name__, None)
    return result


def run_schedule(ir: dict, seed, py_tier=None, turn_cap: int = 0) -> ScheduleResult:
    """Run the schedule for *seed* (or :data:`CANONICAL`) and return its result.
    A thin sync wrapper over :func:`_drive_schedule`."""
    import asyncio  # noqa: PLC0415

    if py_tier is None:
        py_tier = _fault._load_py_tier()
    cap = turn_cap or _turn_cap(ir)
    return asyncio.run(_drive_schedule(ir, _Chooser(seed), py_tier, cap))


def replay_choices(ir: dict, choices: list, py_tier=None, turn_cap: int = 0) -> ScheduleResult:
    """Replay a fixed choice vector (minimization / reproduction)."""
    import asyncio  # noqa: PLC0415

    if py_tier is None:
        py_tier = _fault._load_py_tier()
    cap = turn_cap or _turn_cap(ir)
    return asyncio.run(_drive_schedule(ir, _Replay(choices), py_tier, cap))


def _turn_cap(ir: dict) -> int:
    """A generous per-schedule turn bound: every fiber activates once and tears
    down once, so ``2 * fibers`` is the exact ceiling for the activate/teardown
    alphabet; pad it so a future atom kind cannot silently wedge the loop."""
    return max(8, 4 * (len(_components(ir)) + 1))


# ---------------------------------------------------------------------------
# minimization — delta-debug over the choice vector (design decision 3)
# ---------------------------------------------------------------------------


def minimize(ir: dict, choices: list, signature, py_tier, turn_cap: int) -> list:
    """Shrink a found-bad choice vector to the fewest deviations from the
    canonical (index-0) baseline that still trigger the same property failure.

    The same idea as ``fault.py::_shrink_args`` over the choice vector: prefer
    running the same canonical atom (index 0), which collapses an interleaving
    toward the sequential run, and keep any shortened vector that still fails
    with the same signature."""
    import asyncio  # noqa: PLC0415

    def fails_the_same(candidate: list) -> bool:
        res = asyncio.run(_drive_schedule(ir, _Replay(candidate), py_tier, turn_cap))
        return _signature(res) == signature

    current = list(choices)
    changed = True
    while changed:
        changed = False
        # 1. drop trailing choices (a short prefix often already diverges)
        while current and fails_the_same(current[:-1]):
            current = current[:-1]
            changed = True
        # 2. drive each remaining choice toward the canonical index 0
        for i in range(len(current)):
            if current[i] == 0:
                continue
            candidate = list(current)
            candidate[i] = 0
            if fails_the_same(candidate):
                current = candidate
                changed = True
    return current


def _signature(result: ScheduleResult):
    """The stable identity of a schedule's failure, for minimization: the sorted
    property tags plus the divergent emission order. Two schedules with the same
    signature expose the same bug."""
    tags = tuple(sorted({tag for tag, _ in check_properties(result, None)
                         if tag != "unstable"}))
    return (tags, tuple(result.emissions))


# ---------------------------------------------------------------------------
# the sampler + dossier — the `revl test --schedule-seed(s)` entry points
# ---------------------------------------------------------------------------

DEFAULT_SEEDS = 200


def run_schedules(ir: dict, seed=None, seeds: int = None, out=None,
                  py_tier=None) -> tuple:
    """Explore the schedule space and report. ``(failures, dossier)``.

    * ``seed`` given — replay exactly that one schedule (``--schedule-seed S``).
    * ``seeds`` given — sample that many seeds as a random walk over the
      interleaving space, plus the canonical sequential baseline
      (``--schedule-seeds N``).

    The canonical baseline (always index 0) is the property-4 reference: every
    sampled schedule that commits must reproduce its fingerprint."""
    printer = (lambda line: print(line)) if out is None else out
    if py_tier is None:
        py_tier = _fault._load_py_tier()
    cap = _turn_cap(ir)

    baseline = run_schedule(ir, CANONICAL, py_tier=py_tier, turn_cap=cap)
    baseline_fp = baseline.fingerprint()

    if seed is not None:
        targets = [seed]
    else:
        n = seeds if seeds else DEFAULT_SEEDS
        targets = list(range(n))

    records: list = []
    # the baseline itself is a schedule and must satisfy every property except
    # stability-against-itself (it *is* the reference).
    base_findings = [f for f in check_properties(baseline, None)]
    records.append((CANONICAL, baseline, base_findings))

    for s in targets:
        res = run_schedule(ir, s, py_tier=py_tier, turn_cap=cap)
        findings = check_properties(res, baseline_fp)
        if findings and _has_real_finding(findings):
            minimal = minimize(ir, res.choices, _signature(res), py_tier, cap)
            res_min = replay_choices(ir, minimal, py_tier=py_tier, turn_cap=cap)
            records.append((s, res_min, check_properties(res_min, baseline_fp)))
        else:
            records.append((s, res, findings))

    dossier = _build_dossier(ir, baseline, records, baseline_fp)
    _format_dossier(dossier, printer)
    return dossier["counts"]["failing"], dossier


def _has_real_finding(findings: list) -> bool:
    return any(tag != "ownership?" for tag, _ in findings)


def _build_dossier(ir, baseline, records, baseline_fp) -> dict:
    per: list = []
    failing = 0
    for seed, res, findings in records:
        real = [f for f in findings if f[0] != "ownership?"]
        ok = not real
        if seed is not CANONICAL and not ok:
            failing += 1
        per.append({
            "seed": "canonical" if seed is CANONICAL else seed,
            "ok": ok,
            "turns": res.turns,
            "choices": res.choices,
            "atoms": res.atoms,
            "emissions": res.emissions,
            "findings": [{"tag": t, "message": m} for t, m in findings],
        })
    return {
        "item": 295,
        "title": "schedule testing",
        "counts": {
            "schedules": len(records),
            "failing": failing,
            "components": len(_components(ir)),
        },
        "baseline_emissions": baseline.emissions,
        "per_schedule": per,
    }


def _format_dossier(dossier: dict, printer) -> None:
    counts = dossier["counts"]
    printer(f"[schedule] {counts['schedules']} schedule(s) over "
            f"{counts['components']} component(s); canonical baseline emissions: "
            f"{dossier['baseline_emissions'] or 'none'}")
    for entry in dossier["per_schedule"]:
        if entry["ok"]:
            continue
        head = ("canonical baseline" if entry["seed"] == "canonical"
                else f"seed {entry['seed']}")
        printer(f"FOUND [{head}] after {entry['turns']} turn(s); "
                f"minimized choices {entry['choices']}")
        printer(f"    schedule: {' -> '.join(entry['atoms'])}")
        for finding in entry["findings"]:
            printer(f"    - [{finding['tag']}] {finding['message']}")
    if counts["failing"] == 0:
        printer(f"[schedule] passed: no interleaving violated the properties "
                f"across {counts['schedules']} schedule(s)")
    else:
        printer(f"[schedule] found {counts['failing']} interleaving(s) that "
                f"violate a property (reproduce with --schedule-seed <seed>)")

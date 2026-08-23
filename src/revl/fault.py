"""`fault test` execution on the cordis-py reference tier (docs/fault-tests.md).

A `fault test` is an experiment on a component, not a function of it: it
activates the component for real, kills the activation at a chosen point, and
then interrogates the runtime about the wreckage.  What it checks is the
paradigm's L-Raise reading (IR contract amendment A8) and R4:

    the effects accumulated so far revert, newest first; the component lands
    FAILED with the error recorded; siblings are unaffected; and nothing is
    left behind.

Mechanism, in one line: the harness re-emits the target component with a
``fail`` step spliced in at the injection point.  ``fail`` is an IR step the
frontend and every backend already carry (it is how an author writes a
deliberate L-Raise), so a fault test exercises the *same* machinery the
hand-written A8 scenario scripts do — the only new thing is that it is
declarable in source and the assertions are checked mechanically instead of
by eye.  Nothing about the component under test is special-cased: it is the
component's own emitted module, loaded into a real ``cordis.Context``,
alongside its real providers.

Two orders are observed rather than inferred:

* the *inverse* order, through :class:`runtime.FaultProbe`, which tags each
  disposer the activation yields and records when it runs.  A host trace
  cannot do this — a provision withdrawal or a pure closure undo produces no
  trace event, and those are exactly the inverses a regression drops.
* the *residue*, through the same four runtime introspections
  ``revl run`` reports on teardown: registry, provisions, effects, listeners.

What the harness deliberately does not claim: that an emission before the
failure point was undone.  Emissions are irreversible by construction (§6.1);
they are reported as such, always, pass or fail.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKENDS = ROOT / "backends"


# ---------------------------------------------------------------------------
# static reading of the component body
# ---------------------------------------------------------------------------


def _render(expr) -> str:
    """A short, human-readable rendering of an IR expression node."""
    if not isinstance(expr, dict):
        return "…"
    kind = expr.get("kind")
    if kind == "lit":
        return repr(expr.get("value"))
    if kind in ("req", "name", "var"):
        return str(expr.get("name") or expr.get("id") or "…")
    if kind == "host":
        return str(expr.get("fn") or "…")
    if kind in ("call", "optcall"):
        args = ", ".join(_render(arg) for arg in expr.get("args") or [])
        return f"{_render(expr.get('target'))}.{expr.get('method')}({args})"
    if kind == "field":
        return f"{_render(expr.get('target'))}.{expr.get('name')}"
    return "…"


def _inverse_labels(body: list, upto: int) -> list:
    """Labels for the inverses steps ``1..upto`` accumulate, in order.

    Returns ``[]`` when the prefix contains an `if` — accumulation is then
    conditional and cannot be mapped to source positions statically, so the
    caller falls back to bare ordinals rather than printing a confident lie.
    """
    labels: list[str] = []
    for index, step in enumerate(body[:upto], 1):
        kind = step.get("step")
        if kind == "let-effect":
            labels.append(f"step {index} (undo of effect `{step.get('bind')}`)")
        elif kind == "effect":
            labels.append(f"step {index} (undo of anonymous effect)")
        elif kind == "provide":
            labels.append(f"step {index} (withdrawal of provision `{step.get('name')}`)")
        elif kind == "emit" and step.get("compensate") is not None:
            labels.append(f"step {index} (compensation for emit)")
        elif kind == "if":
            return []
    return labels


def _emissions_before(body: list, upto: int, conditional: bool = False) -> list:
    """Emissions the activation performs before the injection point.

    Each entry is ``(description, compensated, conditional)``.  These are the
    side effects that are *not* undone by the unwind — reporting them is the
    point, not a footnote.
    """
    found: list = []
    for index, step in enumerate(body[:upto], 1):
        kind = step.get("step")
        if kind == "emit":
            found.append((f"step {index}: emit {_render(step.get('expr'))}",
                          step.get("compensate") is not None, conditional))
        elif kind == "if":
            for branch in ("then", "else"):
                nested = step.get(branch) or []
                found.extend(_emissions_before(nested, len(nested), conditional=True))
    return found


def _inject(ir: dict, unit: dict) -> dict:
    """A copy of *ir* whose target component dies at the injection point.

    The spliced step *replaces* step N: steps 1..N-1 have run and accumulated
    their inverses, and step N — along with everything after it — never runs.
    """
    mutated = copy.deepcopy(ir)
    name = unit["component"]
    for component in mutated.get("components") or []:
        if component.get("name") == name:
            body = component.setdefault("body", [])
            body.insert(unit["step"] - 1, {
                "step": "fail",
                "message": {"kind": "lit",
                            "value": f'fault test "{unit["name"]}": injected failure'},
            })
            break
    else:  # pragma: no cover — lowering already validated the name
        raise KeyError(name)
    # the fault-test section itself must not ride into the emitted module we
    # are about to drive; it would just be dead weight in the experiment
    mutated.pop("fault_tests", None)
    return mutated


# ---------------------------------------------------------------------------
# runtime observation
# ---------------------------------------------------------------------------


def _snapshot(root) -> dict:
    """The four introspections ``revl run`` reports on teardown."""
    return {
        "registry": root.registry.size,
        "provisions": sorted(impl.name for impl in root.reflect.store.values()),
        "effects": root.fiber._disposables.length,
        "listeners": {name: len(callbacks)
                      for name, callbacks in root.events._hooks.items() if callbacks},
    }


async def _flush() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


class _Outcome:
    """Everything one injected activation produced, before any judging."""

    def __init__(self) -> None:
        self.state = None            # FiberState of the target after the fault
        self.siblings: dict = {}     # name -> FiberState of every other component
        self.baseline: dict = {}
        self.unwound: dict = {}      # snapshot with the FAILED handle still held
        self.settled: dict = {}      # snapshot after the host disposes the handle
        self.accumulated = 0
        self.ran: list = []
        self.never_ran: list = []
        self.lifo_violation = None
        self.labels: list = []
        self.emissions: list = []
        self.async_body = False   # the body has an `await` step (A1 boundary)
        self.error: str | None = None


def _label(outcome: _Outcome, index) -> str:
    if index is None:
        return "(nothing)"
    if 1 <= index <= len(outcome.labels):
        return outcome.labels[index - 1]
    return f"inverse #{index}"


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------


async def _drive(ir: dict, unit: dict, emit, runtime_mod, Context, FiberState,
                 exclude: frozenset = frozenset()) -> _Outcome:
    outcome = _Outcome()
    target = unit["component"]
    body = next((c.get("body") or [] for c in ir.get("components") or []
                 if c.get("name") == target), [])
    outcome.labels = _inverse_labels(body, unit["step"] - 1)
    outcome.emissions = _emissions_before(body, unit["step"] - 1)
    outcome.async_body = any(step.get("step") == "await" for step in body)

    module = types.ModuleType(f"revl_fault_{abs(hash(unit['name'])):x}")
    sys.modules[module.__name__] = module
    try:
        source = emit.emit(_inject(ir, unit))
        exec(compile(source, f"<revl-fault {unit['name']}>", "exec"), module.__dict__)

        order = ((ir.get("manifest") or {}).get("loadOrder")
                 or [c["name"] for c in ir.get("components") or []])
        root = Context()
        fibers: dict = {}
        try:
            # 1. bring up the rest of the composition, so the target activates
            #    against its real providers rather than a stub.  `exclude` names
            #    components the caller does not want brought up — used by the
            #    sweep to drop the target's downstream dependents, which would
            #    otherwise sit PENDING (their provider is the target, held back
            #    to activate last) and read as a false "sibling affected".
            for name in order:
                if name == target or name in exclude:
                    continue
                fibers[name] = runtime_mod.plug(root, getattr(module, name), {})
                await _flush()
            await _flush()
            outcome.baseline = _snapshot(root)

            # 2. activate the target with the fault armed
            probe = runtime_mod.arm_fault_probe(target)
            try:
                fiber = runtime_mod.plug(root, getattr(module, target), unit.get("config") or {})
                await _flush()
                if fiber.state == FiberState.LOADING:  # an `await` step in flight
                    try:
                        await asyncio.wait_for(asyncio.shield(fiber), 5)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        pass
                    await _flush()
            finally:
                runtime_mod.disarm_fault_probe()

            outcome.state = fiber.state
            outcome.unwound = _snapshot(root)
            outcome.accumulated = len(probe.accumulated)
            outcome.ran = list(probe.ran)
            outcome.never_ran = probe.never_ran()
            outcome.lifo_violation = probe.lifo_violation()
            outcome.siblings = {name: f.state for name, f in fibers.items()}

            # 3. the host drops the FAILED handle — A8's "error recorded" is
            #    a registration the host owns, so R4 is measured after it goes
            await fiber.dispose()
            await _flush()
            outcome.settled = _snapshot(root)
        finally:
            for name in reversed(order):
                fiber = fibers.pop(name, None)
                if fiber is not None:
                    try:
                        await fiber.dispose()
                    except Exception:  # noqa: BLE001 — cleanup must not mask a result
                        pass
            await _flush()
    finally:
        sys.modules.pop(module.__name__, None)
    return outcome


# ---------------------------------------------------------------------------
# judging
# ---------------------------------------------------------------------------


def _judge(unit: dict, outcome: _Outcome, FiberState) -> list:
    """Return the list of failure lines; empty means the fault test passed."""
    failures: list[str] = []
    wanted = unit.get("assert") or []

    if "failed" in wanted:
        state = outcome.state
        if state != FiberState.FAILED:
            name = getattr(state, "name", state)
            message = (f"the component did not land FAILED — it is {name}; "
                       f"the injected failure at step {unit['step']} did not become an L-Raise")
            if outcome.async_body and outcome.never_ran == []:
                # the divergence documented in docs/fault-tests.md §known
                # divergences: an `await` step makes the body an async
                # generator, and cordis-py routes an async setup failure to
                # the effect guard (auto-dispose) instead of to the fiber's
                # error slot, so the effects revert but the fiber stays ACTIVE
                message += (" — this body contains an `await` step, so it compiles to an "
                            "async generator; the inverses DID run, only the fiber state "
                            "is wrong (see docs/fault-tests.md, known divergences)")
            failures.append(message)

    if "no-residue" in wanted:
        base, unwound, settled = outcome.baseline, outcome.unwound, outcome.settled
        if outcome.never_ran:
            detail = ", ".join(_label(outcome, index) for index in outcome.never_ran)
            failures.append(
                f"residue in the host: {len(outcome.never_ran)} of {outcome.accumulated} "
                f"accumulated inverse(s) never ran — {detail}")
        leaked = [key for key in unwound["provisions"] if key not in base["provisions"]]
        if leaked:
            failures.append(
                "residue in the service registry: provision(s) "
                + ", ".join(f"`{key}`" for key in leaked)
                + " survived the unwind (baseline provisions: "
                + (", ".join(f"`{k}`" for k in base["provisions"]) or "none") + ")")
        if unwound["listeners"] != base["listeners"]:
            failures.append(
                f"residue in the event hooks: {unwound['listeners']} after the unwind, "
                f"baseline {base['listeners']}")
        if settled["effects"] != base["effects"]:
            failures.append(
                f"residue in the effect stack: {settled['effects']} disposable(s) on the "
                f"root fiber after the FAILED handle was disposed, baseline "
                f"{base['effects']}")
        if settled["registry"] != base["registry"]:
            failures.append(
                f"residue in the registry: size {settled['registry']} after the FAILED "
                f"handle was disposed, baseline {base['registry']}")
        if settled["provisions"] != base["provisions"]:
            failures.append(
                f"residue in the service registry after teardown: {settled['provisions']}, "
                f"baseline {base['provisions']}")

    if "inverses-lifo" in wanted:
        violation = outcome.lifo_violation
        if violation is not None:
            position, ran_index, expected = violation
            if ran_index is None:
                failures.append(
                    f"inverses stopped early: nothing ran at unwind position {position}, "
                    f"expected {_label(outcome, expected)} "
                    f"({outcome.accumulated} accumulated, {len(outcome.ran)} ran)")
            else:
                failures.append(
                    f"inverses ran out of LIFO order: unwind position {position} ran "
                    f"{_label(outcome, ran_index)}, expected {_label(outcome, expected)}"
                    f" — accumulation order was ["
                    + ", ".join(_label(outcome, i) for i in range(1, outcome.accumulated + 1))
                    + "]")

    if "no-emissions" in wanted and outcome.emissions:
        failures.append(
            "the activation emitted before it died, and an emission cannot be reverted: "
            + "; ".join(text for text, _, _ in outcome.emissions))

    if "siblings-unaffected" in wanted:
        harmed = {name: getattr(state, "name", state)
                  for name, state in outcome.siblings.items()
                  if state != FiberState.ACTIVE}
        if harmed:
            failures.append(
                "sibling components were affected by the failure: "
                + ", ".join(f"{name} is {state}" for name, state in harmed.items())
                + " (A8: a failing fiber is contained)")

    return failures


def _notes(outcome: _Outcome) -> list:
    """Lines printed for every fault test, pass or fail."""
    notes: list[str] = []
    for text, compensated, conditional in outcome.emissions:
        qualifier = " (conditional — inside an `if`, may not have run)" if conditional else ""
        if compensated:
            notes.append(f"irreversible: {text}{qualifier} — its `compensate` ran, "
                         f"but the emission itself stands (compensation is not inversion)")
        else:
            notes.append(f"irreversible: {text}{qualifier} — no inverse exists for an "
                         f"emission; it was NOT reverted by the unwind")
    if outcome.baseline and outcome.unwound:
        held = outcome.unwound["registry"] - outcome.baseline["registry"]
        if held:
            state = getattr(outcome.state, "name", outcome.state)
            notes.append(
                f"the {state} fiber stayed registered until the host disposed it "
                f"(registry {outcome.baseline['registry']} -> {outcome.unwound['registry']}) "
                f"— that registration is A8's \"error recorded\", not residue")
    return notes


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def fault_units(ir: dict, module=None) -> list:
    """The fault tests to run: the emitted module's manifest when there is
    one (the py tier lowers them), else the IR section."""
    emitted = getattr(module, "REVL_FAULT_TESTS", None) if module is not None else None
    if emitted:
        return [dict(unit) for unit in emitted]
    units = []
    for unit in ir.get("fault_tests") or []:
        at = unit.get("at") or {}
        units.append({
            "name": unit.get("name"),
            "component": unit.get("component"),
            "step": at.get("step"),
            "effect": at.get("effect"),
            "assert": list(unit.get("assert") or []),
            "config": dict(unit.get("config") or {}),
        })
    return units


def _load_py_tier():
    """Import the cordis-py reference tier: ``(emit, runtime, Context,
    FiberState)``.  Raises ``ModuleNotFoundError`` when the runtime is absent —
    the caller decides whether that is a skip or an error."""
    backend_dir = BACKENDS / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import emit  # noqa: PLC0415 — backend import after path setup
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

    return emit, runtime_mod, Context, FiberState


def run_fault_units(ir: dict, units: list, out=None) -> tuple[int, int]:
    """Run *units* against the py reference tier; ``(failures, total)``.

    Raises ``ModuleNotFoundError`` when the cordis-py runtime is absent — the
    caller decides whether that is a skip or an error.
    """
    printer = (lambda line: print(line)) if out is None else out
    emit, runtime_mod, Context, FiberState = _load_py_tier()

    failures = 0
    for unit in units:
        where = f"step {unit['step']}"
        if unit.get("effect"):
            where += f" (effect `{unit['effect']}`)"
        try:
            outcome = asyncio.run(_drive(ir, unit, emit, runtime_mod, Context, FiberState))
        except Exception as error:  # noqa: BLE001 — a driver crash is a test failure
            failures += 1
            printer(f"FAIL {unit['name']}: the fault-test driver raised "
                    f"{type(error).__name__}: {error}")
            continue
        problems = _judge(unit, outcome, FiberState)
        head = f"{unit['name']} [{unit['component']} dies at {where}]"
        if problems:
            failures += 1
            printer(f"FAIL {head}")
            for problem in problems:
                printer(f"    - {problem}")
        else:
            printer(f"PASS {head}")
        for note in _notes(outcome):
            printer(f"    note: {note}")
    return failures, len(units)


# ---------------------------------------------------------------------------
# the sweep — from "fail at a chosen step" to "fail at every step"
# ---------------------------------------------------------------------------
#
# A `fault test` proves A8/R4 at ONE author-chosen point.  The sweep upgrades
# the claim to an exhaustive one: the compiler already knows the complete step
# list from the IR, so it auto-generates the injection at *every* top-level
# body step of *every* component, runs the full assertion set at each, and
# reports.  A component with N steps gets N fault tests nobody wrote —
# "no mid-life failure point leaves residue", not "A8 held where I looked".
#
# It reuses the single-point machinery verbatim: the same `_inject`, `_drive`,
# `_judge`.  The sweep is a generator loop over the IR plus an aggregating
# report; the hard part (the injector and the runtime interrogation) is not
# re-implemented.
#
# The exhaustive verdict is honest about its edges.  A step the injection
# scheme cannot address — one nested inside a component `if` — is *named* in
# the report, never silently skipped: "nothing checked must never read as
# clean".  (The surface language forbids anything but `fail` inside a
# component `if` per G6, so nested steps arise only from hand-written or
# imported IR; the sweep still refuses to pretend it covered them.)
#
# OUT OF SCOPE, recorded for the horizon: seeded clock/random coeffects and
# async-interleaving exploration would turn this sequential sweep into a
# schedule sweep (fail at every step *under every interleaving*).  The
# sequential sweep captures the value now; the async/coeffect axis is the
# next frontier, not this one.

_SWEEP_ASSERTS = ["failed", "no-residue", "inverses-lifo", "siblings-unaffected"]

_ROADMAP_ITEM = 30
_SWEEP_TITLE = "fault sweep at every step"


def _body_of(ir: dict, component: str) -> list:
    return next((c.get("body") or [] for c in ir.get("components") or []
                 if c.get("name") == component), [])


def _step_where(step: dict, index: int) -> str:
    """A short label for a top-level body step, in the diagnostics' idiom."""
    kind = step.get("step")
    if kind == "let-effect":
        return f"step {index} (effect `{step.get('bind')}`)"
    if kind == "provide":
        return f"step {index} (provision `{step.get('name')}`)"
    if kind == "emit":
        return f"step {index} (emit)"
    if kind == "await":
        return f"step {index} (await)"
    return f"step {index} ({kind})"


def _provider_dependents(ir: dict) -> dict:
    """Map component -> the set of components that transitively require what it
    provides.  Computed from the manifest alone (no runtime): component B
    depends on A when B injects a key A provides.

    The sweep uses this to hold a target's *dependents* out of the bring-up.
    A dependent whose provider is the target could never activate (the target
    is deliberately plugged last, with the fault armed), so it would sit
    PENDING and read as a spurious "sibling affected".  Its unavailability is
    expected propagation of the injected failure, not a containment breach.
    """
    comps = (ir.get("manifest") or {}).get("components") or []
    provides = {c["name"]: set(c.get("provides") or []) for c in comps}
    injects = {c["name"]: set(c.get("inject") or []) for c in comps}
    names = [c["name"] for c in comps]
    direct = {a: {b for b in names
                  if b != a and injects.get(b, set()) & provides.get(a, set())}
              for a in names}
    closure: dict = {}
    for start in names:
        seen: set = set()
        stack = list(direct.get(start, ()))
        while stack:
            node = stack.pop()
            if node in seen or node == start:
                continue
            seen.add(node)
            stack.extend(direct.get(node, ()))
        closure[start] = seen
    return closure


def sweep_units(ir: dict, component: str) -> list:
    """One synthesized fault unit per *top-level* body step of *component*.

    The unit is exactly what an author would have written by hand — the same
    shape `fault_units` produces — carrying the full assertion set.  `effect`
    is filled for `let … effect` steps so the diagnostics can print the name.
    """
    units = []
    for index, step in enumerate(_body_of(ir, component), 1):
        effect = step.get("bind") if step.get("step") == "let-effect" else None
        units.append({
            "name": f"sweep {component} @ step {index}",
            "component": component,
            "step": index,
            "effect": effect,
            "assert": list(_SWEEP_ASSERTS),
            "config": {},
            "where": _step_where(step, index),
        })
    return units


def _unreachable_steps(body: list, prefix: str = "") -> list:
    """Steps the injection scheme cannot address — those nested inside a
    control-flow construct.  Returned as ``[{"where", "reason"}]`` so the
    report can name each one rather than skip it silently."""
    found: list = []
    for index, step in enumerate(body, 1):
        if step.get("step") == "if":
            here = f"{prefix}step {index}"
            for branch in ("then", "else"):
                nested = step.get(branch) or []
                for jndex, inner in enumerate(nested, 1):
                    where = f"{here} > {branch} > step {jndex} ({inner.get('step')})"
                    found.append({
                        "where": where,
                        "reason": "nested inside an `if`; the injection scheme "
                                  "addresses only top-level body steps "
                                  "(docs/fault-tests.md, section 5)",
                    })
                    found.extend(_unreachable_steps(
                        [inner], prefix=f"{here} > {branch} > "))
    return found


def _sweep_dossier(per_component: list) -> dict:
    """Aggregate per-component step results into a gauntlet-compatible dossier.

    *per_component* is a list of ``(name, excluded, step_results, unreachable)``
    where *step_results* is ``[(unit, problems, notes)]``.  Pure — no runtime,
    so the aggregation and the counts are testable against fabricated results.
    """
    components = []
    total_steps = total_passed = total_failed = total_unreachable = 0
    flat_unreachable: list = []
    for name, excluded, step_results, unreachable in per_component:
        steps = []
        passed = failed = 0
        for unit, problems, _notes_lines in step_results:
            ok = not problems
            passed += ok
            failed += not ok
            steps.append({
                "step": unit["step"],
                "where": unit.get("where") or f"step {unit['step']}",
                "status": "pass" if ok else "fail",
                "problems": list(problems),
            })
        for item in unreachable:
            flat_unreachable.append({"component": name, **item})
        components.append({
            "component": name,
            "swept": len(step_results),
            "passed": passed,
            "failed": failed,
            "excludedSiblings": list(excluded),
            "steps": steps,
            "unreachable": list(unreachable),
        })
        total_steps += len(step_results)
        total_passed += passed
        total_failed += failed
        total_unreachable += len(unreachable)
    return {
        "kind": "tested",
        "status": "failed" if total_failed else "passed",
        "roadmapItem": _ROADMAP_ITEM,
        "title": _SWEEP_TITLE,
        "tier": "py",
        "note": "every top-level body step of every component was made to fail "
                "in turn and checked for L-Raise / no-residue / LIFO / "
                "unaffected siblings; steps the scheme cannot address are "
                "named, never skipped (docs/fault-tests.md).",
        "counts": {
            "components": len(per_component),
            "steps": total_steps,
            "passed": total_passed,
            "failed": total_failed,
            "unreachable": total_unreachable,
        },
        "components": components,
        "unreachable": flat_unreachable,
    }


def _format_sweep(dossier: dict, per_component: list, printer) -> None:
    """Human-readable rendering of the sweep.  Mirrors the fault-test runner's
    PASS/FAIL idiom and always closes with the exhaustive line."""
    printer("fault sweep — py reference tier (the only tier that executes "
            "fault tests)")
    notes_by_step = {(name, unit["step"]): notes
                     for name, _e, results, _u in per_component
                     for unit, _p, notes in results}
    for section in dossier["components"]:
        name = section["component"]
        printer("")
        printer(f"{name}: {section['swept']} step(s) swept, "
                f"{section['passed']} passed, {section['failed']} failed")
        if section["excludedSiblings"]:
            printer("  (held out of the bring-up — they require what "
                    f"{name} provides: "
                    + ", ".join(section["excludedSiblings"]) + ")")
        for step in section["steps"]:
            tag = "PASS" if step["status"] == "pass" else "FAIL"
            printer(f"  {tag} {step['where']}")
            for problem in step["problems"]:
                printer(f"      - {problem}")
            for note in notes_by_step.get((name, step["step"]), []):
                printer(f"      note: {note}")
        for item in section["unreachable"]:
            printer(f"  UNREACHABLE {item['where']}: {item['reason']}")
    counts = dossier["counts"]
    if dossier["unreachable"]:
        printer("")
        printer(f"unreachable (enumerated, never silently skipped): "
                f"{counts['unreachable']} step(s)")
    printer("")
    printer(f"swept {counts['steps']} step(s) across {counts['components']} "
            f"component(s): {counts['passed']} passed, {counts['failed']} "
            f"failed, {counts['unreachable']} unreachable")


def run_sweep(ir: dict, out=None, only: str | None = None) -> tuple[int, dict]:
    """Sweep every top-level step of every component (or just *only*), run the
    full assertion set at each on the py reference tier, and report.

    Returns ``(failures, dossier)``.  The dossier's ``counts`` and ``status``
    are shaped to drop straight into the gauntlet's `faultSweep` slot
    (src/revl/mcp/gauntlet.py) — a later task wires it; here the shape is made
    compatible.  Raises ``ModuleNotFoundError`` when cordis-py is absent, so
    the caller can report a skip (never a pass) rather than crash.
    """
    printer = (lambda line: print(line)) if out is None else out
    emit, runtime_mod, Context, FiberState = _load_py_tier()

    dependents = _provider_dependents(ir)
    if only is not None:
        names = [only]
    else:
        names = [c.get("name") for c in ir.get("components") or []]

    per_component: list = []
    for name in names:
        excluded = frozenset(dependents.get(name, ()))
        results: list = []
        for unit in sweep_units(ir, name):
            try:
                outcome = asyncio.run(_drive(ir, unit, emit, runtime_mod,
                                             Context, FiberState, exclude=excluded))
            except Exception as error:  # noqa: BLE001 — a driver crash is a failure
                results.append((unit,
                                [f"the fault-test driver raised "
                                 f"{type(error).__name__}: {error}"], []))
                continue
            results.append((unit, _judge(unit, outcome, FiberState),
                            _notes(outcome)))
        per_component.append((name, sorted(excluded), results,
                              _unreachable_steps(_body_of(ir, name))))

    dossier = _sweep_dossier(per_component)
    _format_sweep(dossier, per_component, printer)
    return dossier["counts"]["failed"], dossier


def sweep_dossier(ir: dict, only: str | None = None) -> dict:
    """The sweep as a structured dossier only (no printing) — the entry the
    gauntlet's `faultSweep` slot consumes.  Same runtime requirement as
    :func:`run_sweep`."""
    return run_sweep(ir, out=lambda _line: None, only=only)[1]


# ===========================================================================
# `verified effect` — inverse round-trip testing (roadmap item 26)
# ===========================================================================
#
# The checker proves an inverse is *present and well-shaped* (G4:
# inverse-or-emit), never that it is *correct*.  docs/replay.md §4.1 names the
# gap outright: "an `undo` that is wrong, partial, or a no-op" is not caught.
# A `verified effect` opts an activation-body effect into a test the author
# did not write: snapshot the observable in-process state, activate the
# component (the effect runs and accumulates its inverse), tear it down (the
# inverse runs, LIFO), and assert the state fingerprint returned to where it
# started — N randomized rounds, on the real cordis-py runtime.
#
# This is emphatically NOT a proof (docs/verified-effect.md, and the report
# header below say so).  It upgrades "trust the author's undo" to "this undo
# survived N round-trips."  Scope is HONEST and narrow (replay.md §4.1): the
# fingerprint is the runtime's own observable-mutation ledger — host resources
# acquired/released, map keys inserted/removed, pool connections
# acquired/released.  It CANNOT see aliased references the component handed
# out, external effects an emission crossed, or clock/random-derived values.
# Those are out of reach and the header names them, every run.
#
# Machinery is shared with the fault runner: `_load_py_tier`, `plug`,
# `_flush`.  The one new capability is the fingerprint, which is derived
# purely from the runtime's trace ledger (the same `set_trace` stream the
# lifecycle harness pairs for its R1 residue check) — so it needs no reach
# into host objects and stays testable as a pure fold over a list of strings.
#
# DEPENDENCY NOTE (roadmap item 37, `prop test`): the roadmap intends a
# general property-testing form with item 26 as its first instance.  Item 37
# is NOT built; this is the specific inverse-round-trip generator, built
# directly.  When 37 lands, the snapshot/randomize/compare loop below is the
# machinery it generalizes (arbitrary property, arbitrary generators); the
# `verified effect` marker becomes one derived property among many.

_ROUNDTRIP_ITEM = 26
_ROUNDTRIP_TITLE = "verified-effect inverse round-trips"
_ROUNDTRIP_ROUNDS = 16

# Named, every run, so "the fingerprint matched" is never misread as "the
# undo is correct" (the same honesty rule OpenAPI import applies to
# safe-by-spec: it is the author's claim, machine-checked only so far).
_ROUNDTRIP_HEADER = (
    "verified-effect inverse round-trips — py reference tier (roadmap item 26)\n"
    "  This is a TEST THE AUTHOR DID NOT WRITE, not a proof. It upgrades "
    "trust-the-author's-undo\n"
    "  to this-undo-survived-{n}-round-trips: for each `verified effect`, activate the "
    "component,\n"
    "  tear it down so the inverse runs, and assert the observable-state fingerprint "
    "returned\n"
    "  to baseline — {n} randomized rounds.\n"
    "  IN SCOPE: in-process observable state only — the runtime's mutation ledger "
    "(host\n"
    "  acquire/release, map keys, pool connections). OUT OF REACH (replay.md §4.1), "
    "never\n"
    "  asserted: aliased references the component handed out; external effects an "
    "emission\n"
    "  crossed; clock- or random-derived values. A pass does not cover those."
)


def _outstanding(events: list) -> dict:
    """The net observable in-process mutations a trace `events` list leaves
    standing — a canonical, comparable fingerprint.

    Pure (a fold over the runtime's trace strings), so it is testable against
    fabricated ledgers with no runtime.  The vocabulary is the reference
    tier's (docs/backend-ir.md §Host builtins; runtime.Map / runtime.Pool):

        "<tag>.new" / "<tag>.drop"                 a Map came into / left being
        "<tag>.open <url>" / "<tag>.close <url>"    a Pool opened / closed
        "<tag>.insert <key>" / "<tag>.remove <key>" a Map key set / cleared
        "<tag>.acquire conn=<k> …" / ".release …"   a Pool connection checked out

    Anything the ledger does not carry (an aliased reference, an emission that
    crossed the boundary, a clock read) is by construction invisible here —
    which is exactly the honesty bound in the report header.
    """
    live: set = set()                 # tags currently in being (open Maps/Pools)
    keys: dict = {}                   # map tag -> set of outstanding keys
    conns: dict = {}                  # pool tag -> set of checked-out connections
    for event in events:
        head = event.split(" ", 1)[0]
        tag, _, verb = head.rpartition(".")
        if not tag:
            continue
        rest = event[len(head):].strip()
        if verb in ("new", "open"):
            live.add(tag)
            keys.setdefault(tag, set())
            conns.setdefault(tag, set())
        elif verb in ("drop", "close"):
            live.discard(tag)
            keys.pop(tag, None)
            conns.pop(tag, None)
        elif verb == "insert":
            if tag in keys:
                keys[tag].add(rest)
        elif verb == "remove":
            if tag in keys:
                keys[tag].discard(rest)
        elif verb == "acquire":
            conn = rest.split(" ", 1)[0]  # "conn=<k>"
            if tag in conns:
                conns[tag].add(conn)
        elif verb == "release":
            conn = rest.split(" ", 1)[0]
            if tag in conns:
                conns[tag].discard(conn)
    return {
        "live": sorted(live),
        "keys": {tag: sorted(ks) for tag, ks in keys.items() if ks},
        "conns": {tag: sorted(cs) for tag, cs in conns.items() if cs},
    }


def _fingerprint_delta(baseline: dict, final: dict) -> list:
    """Human-readable descriptions of every way *final* differs from
    *baseline* — empty means the round trip closed."""
    diffs: list = []
    base_live, final_live = set(baseline["live"]), set(final["live"])
    for tag in sorted(final_live - base_live):
        diffs.append(f"host resource `{tag}` was acquired and never released "
                     "(its inverse did not drop/close it)")
    for tag in sorted(base_live - final_live):
        diffs.append(f"host resource `{tag}` present at baseline is gone "
                     "(the inverse released something it should not have)")
    for tag in sorted(set(baseline["keys"]) | set(final["keys"])):
        b = set(baseline["keys"].get(tag, []))
        f = set(final["keys"].get(tag, []))
        for key in sorted(f - b):
            diffs.append(f"`{tag}` still holds key {key!r} the effect inserted "
                         "(its inverse did not remove it)")
        for key in sorted(b - f):
            diffs.append(f"`{tag}` lost key {key!r} that stood at baseline "
                         "(the inverse removed more than it added)")
    for tag in sorted(set(baseline["conns"]) | set(final["conns"])):
        b = set(baseline["conns"].get(tag, []))
        f = set(final["conns"].get(tag, []))
        for conn in sorted(f - b):
            diffs.append(f"`{tag}` left connection {conn} checked out "
                         "(the inverse did not release it)")
        for conn in sorted(b - f):
            diffs.append(f"`{tag}` released baseline connection {conn} "
                         "(the inverse released more than it acquired)")
    return diffs


def _verified_effect_labels(body: list) -> list:
    """The `verified` effect steps in *body*, as short labels, in order."""
    labels: list = []
    for index, step in enumerate(body, 1):
        if not step.get("verified"):
            continue
        kind = step.get("step")
        if kind == "let-effect":
            labels.append(f"step {index} (verified effect `{step.get('bind')}`)")
        else:
            labels.append(f"step {index} (verified anonymous effect)")
    return labels


def roundtrip_units(ir: dict) -> list:
    """One unit per component that has a `verified` activation-body effect.

    The marker is threaded by the frontend (`step["verified"]`), so a hand- or
    import-produced IR gets round-trip tested too.  Shape mirrors a fault unit.
    """
    units: list = []
    for component in ir.get("components") or []:
        labels = _verified_effect_labels(component.get("body") or [])
        if labels:
            units.append({"component": component.get("name"), "verified": labels})
    return units


def _config_schema(ir: dict, component: str) -> list:
    return next((c.get("config") or [] for c in ir.get("components") or []
                 if c.get("name") == component), [])


def _random_config(schema: list, rng) -> dict:
    """A randomized, *valid* config for a component's declared fields.

    Type-directed generators (the surface `verified effect` inputs vary across
    rounds).  Deliberately conservative — positive ints, printable strings —
    so a generated value never trips a host precondition (e.g. `Pool.open`
    needs size >= 1 and a non-`boom://` url) and turns a round-trip round into
    a spurious activation failure.  A field the generator does not recognize
    is left to its default (omitted).
    """
    config: dict = {}
    for field_ in schema:
        name = field_.get("name")
        typ = field_.get("type")
        if typ == "Int":
            config[name] = rng.randint(1, 64)
        elif typ == "Bool":
            config[name] = rng.choice([True, False])
        elif typ in ("Float", "F64", "Num"):
            config[name] = round(rng.uniform(1.0, 64.0), 3)
        elif typ == "Str":
            config[name] = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789")
                                   for _ in range(rng.randint(3, 10)))
        # unknown type: fall through — its default (if any) applies
    return config


async def _drive_roundtrip(ir: dict, component: str, configs: dict,
                           emit, runtime_mod, Context, FiberState) -> tuple:
    """One round: bring up providers, snapshot the ledger, activate *component*,
    tear it down so its inverses run, and return ``(ok, detail)`` — ``ok``
    False with a reason when the delta is non-empty or the activation could not
    complete.

    *configs* maps every component name to the config it is brought up with
    (the target's is the randomized one; providers get valid randomized config
    too so a provider with a required field still activates).
    """
    order = ((ir.get("manifest") or {}).get("loadOrder")
             or [c["name"] for c in ir.get("components") or []])
    config = configs.get(component) or {}

    module = types.ModuleType(f"revl_roundtrip_{abs(hash(component)):x}")
    sys.modules[module.__name__] = module
    events: list = []
    try:
        source = emit.emit(ir)
        exec(compile(source, f"<revl-roundtrip {component}>", "exec"), module.__dict__)

        root = Context()
        fibers: dict = {}
        runtime_mod.set_trace(events.append)
        try:
            # providers up first, so the verified effect activates against its
            # real dependencies (the same discipline the fault driver uses)
            for name in order:
                if name == component:
                    continue
                fibers[name] = runtime_mod.plug(root, getattr(module, name),
                                                configs.get(name) or {})
                await _flush()
            await _flush()
            baseline = _outstanding(list(events))

            fiber = runtime_mod.plug(root, getattr(module, component), config)
            await _flush()
            if fiber.state == FiberState.LOADING:
                try:
                    await asyncio.wait_for(asyncio.shield(fiber), 5)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass
                await _flush()
            if fiber.state != FiberState.ACTIVE:
                state = getattr(fiber.state, "name", fiber.state)
                return (False, f"the component did not reach ACTIVE (it is {state}) with "
                               f"config {config!r} — the verified effect never ran to "
                               "completion, so the round-trip is inconclusive")

            # tear the target down: its accumulated inverses run, LIFO
            await fiber.dispose()
            await _flush()
            final = _outstanding(list(events))
        finally:
            runtime_mod.set_trace(None)
            for name in reversed(order):
                fiber = fibers.pop(name, None)
                if fiber is not None:
                    try:
                        await fiber.dispose()
                    except Exception:  # noqa: BLE001 — cleanup must not mask a result
                        pass
            await _flush()
    finally:
        sys.modules.pop(module.__name__, None)

    diffs = _fingerprint_delta(baseline, final)
    if diffs:
        return (False, "; ".join(diffs))
    return (True, None)


def _roundtrip_dossier(results: list, rounds: int) -> dict:
    """Aggregate ``(component, labels, round_results)`` into a gauntlet-shaped
    dossier.  ``round_results`` is ``[(ok, detail, config)]``.  Pure."""
    components = []
    total = passed = failed = 0
    for component, labels, round_results in results:
        first_fail = next(((detail, cfg) for ok, detail, cfg in round_results if not ok),
                          None)
        ok = first_fail is None
        total += 1
        passed += ok
        failed += not ok
        entry = {
            "component": component,
            "verifiedEffects": list(labels),
            "rounds": len(round_results),
            "status": "pass" if ok else "fail",
        }
        if first_fail is not None:
            entry["counterexample"] = {"config": first_fail[1], "reason": first_fail[0]}
        components.append(entry)
    return {
        "kind": "tested",
        "status": "failed" if failed else "passed",
        "roadmapItem": _ROUNDTRIP_ITEM,
        "title": _ROUNDTRIP_TITLE,
        "tier": "py",
        "note": "each `verified effect` was round-tripped (activate, tear down, "
                "compare the observable-state fingerprint) over "
                f"{rounds} randomized rounds; a test the author did not write, not a "
                "proof — in-process state only, aliases/emissions/clock out of reach "
                "(docs/verified-effect.md).",
        "counts": {
            "effects": total,
            "passed": passed,
            "failed": failed,
            "rounds": rounds,
        },
        "components": components,
    }


def run_roundtrip_units(ir: dict, units: list, out=None,
                        rounds: int = _ROUNDTRIP_ROUNDS) -> tuple:
    """Run the round-trip property for each *unit* on the py reference tier;
    ``(failures, dossier)``.

    Raises ``ModuleNotFoundError`` when cordis-py is absent — the caller
    decides skip vs error, exactly as the fault runner does.
    """
    import random  # noqa: PLC0415 — stdlib, only needed when the runner runs

    printer = (lambda line: print(line)) if out is None else out
    emit, runtime_mod, Context, FiberState = _load_py_tier()

    for line in _ROUNDTRIP_HEADER.format(n=rounds).split("\n"):
        printer(line)

    all_components = [c.get("name") for c in ir.get("components") or []]
    results: list = []
    for unit in units:
        component = unit["component"]
        labels = unit["verified"]
        rng = random.Random(f"revl-roundtrip:{component}")
        round_results: list = []
        for round_index in range(rounds):
            # a fresh, valid, randomized config for every component (the
            # target's is the input surface under test; providers get one too
            # so a provider with a required field still activates)
            configs = {name: _random_config(_config_schema(ir, name), rng)
                       for name in all_components}
            config = configs[component]
            try:
                ok, detail = asyncio.run(_drive_roundtrip(
                    ir, component, configs, emit, runtime_mod, Context, FiberState))
            except Exception as error:  # noqa: BLE001 — a driver crash is a failure
                ok, detail = False, (f"the round-trip driver raised "
                                     f"{type(error).__name__}: {error}")
            round_results.append((ok, detail, config))
            if not ok:
                break  # a shrinking-free counterexample: report the first failing config
        results.append((component, labels, round_results))

    dossier = _roundtrip_dossier(results, rounds)
    for entry in dossier["components"]:
        effects = ", ".join(entry["verifiedEffects"])
        if entry["status"] == "pass":
            printer(f"PASS {entry['component']}: {entry['rounds']} round(s), "
                    f"inverse held for [{effects}]")
        else:
            ce = entry.get("counterexample") or {}
            printer(f"FAIL {entry['component']}: inverse round-trip broke for [{effects}]")
            printer(f"    - config {ce.get('config')!r}: {ce.get('reason')}")
    counts = dossier["counts"]
    printer(f"round-tripped {counts['effects']} verified effect(s): "
            f"{counts['passed']} held, {counts['failed']} broke "
            f"({counts['rounds']} randomized rounds each)")
    return counts["failed"], dossier


def roundtrip_dossier(ir: dict, rounds: int = _ROUNDTRIP_ROUNDS) -> dict:
    """The round-trip run as a structured dossier only (no printing) — shaped
    to drop into the gauntlet's `inverseRoundTrip` slot
    (src/revl/mcp/gauntlet.py, roadmapItem 26).  Same runtime requirement as
    :func:`run_roundtrip_units`; returns an empty ``passed`` dossier when the
    document declares no `verified effect`."""
    units = roundtrip_units(ir)
    if not units:
        return {
            "kind": "tested", "status": "passed", "roadmapItem": _ROUNDTRIP_ITEM,
            "title": _ROUNDTRIP_TITLE, "tier": "py",
            "note": "no `verified effect` declared.",
            "counts": {"effects": 0, "passed": 0, "failed": 0, "rounds": rounds},
            "components": [],
        }
    return run_roundtrip_units(ir, units, out=lambda _line: None, rounds=rounds)[1]


# ===========================================================================
# `prop test` — property testing with type-derived generators (roadmap item 37)
# ===========================================================================
#
# `prop test "name" (a: Int, b: Money) { assert … }` states a property that
# must hold for EVERY value of its generated inputs.  The generators are
# DERIVED from the parameter types the checker fully knows — the i64 edge
# values for `Int`, both arms of every `Opt`, empty and non-empty `List`s,
# every constructor of an ADT, and each field of a record — so the search is
# not a blind random walk but one that provably visits the type's boundaries.
# On failure the runner SHRINKS the offending input to a minimal counterexample
# (fewer list elements, values pulled toward zero, `Some` collapsed to `None`,
# a payload case collapsed to a base case) and reports that, not the first
# messy input it happened to hit.
#
# Scope, like `fault test` / `verified effect`, is the PY REFERENCE TIER.  The
# property body is a pure function of its parameters, so the runner lowers it
# to an ordinary emitted function (injected into the module the same way the
# fault runner injects a `fail` step), execs it, and calls it with generated
# arguments in-process.  It needs no cordis Context — a `prop test` never
# activates a component — which is why it can run even where a `lifecycle` or
# `fault` test would be skipped.
#
# HORIZON (noted, NOT built here): the roadmap's bonus is compiling a
# `prop test` to all six tiers as a cross-tier differential fuzzer (the same
# generated inputs run on every backend, and a divergence is the finding).
# That is an emit-side feature (it touches every backend's `emit.py` and the
# backend golden suite); this pass keeps `prop test` a py-runtime feature.
# RELATIONSHIP TO ITEM 26: `verified effect` is the inverse-round-trip property
# — "for a generated activation, undo∘do leaves no observable residue."  It is
# item 37's first, hand-written instance; its snapshot/randomize/compare loop
# is exactly what this general runner does, specialized to one property.  It is
# left in place (docs/verified-effect.md) and can later be re-expressed as a
# derived `prop test` once activation-valued generators exist.

_PROP_ITEM = 37
_PROP_TITLE = "property tests"
_PROP_RANDOM_ROUNDS = 64      # random-search rounds, on top of the edge rounds
_PROP_MAX_SHRINK = 4000       # safety bound on total shrink trials
_PROP_LIST_MAX = 4            # longest list a random round generates
_PROP_DEPTH = 4              # recursion bound for self-referential ADTs

_I64_MIN = -(2 ** 63)
_I64_MAX = 2 ** 63 - 1
_I32_MIN = -(2 ** 31)
_I32_MAX = 2 ** 31 - 1
# the edge/near-edge values the generator guarantees to visit for every `Int`
_I64_EDGES = [0, 1, -1, 2, -2, _I64_MAX, _I64_MIN, _I64_MAX - 1, _I64_MIN + 1]
_I32_EDGES = [0, 1, -1, 2, -2, _I32_MAX, _I32_MIN, _I32_MAX - 1, _I32_MIN + 1]
_STR_EDGES = ["", "a", "Z", "0", " ", "aa", "revl"]
_FLOAT_EDGES = [0.0, 1.0, -1.0, 0.5]
_STR_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "

_PROP_HEADER = (
    "property tests — py reference tier (roadmap item 37)\n"
    "  For each `prop test`, inputs are DERIVED from the parameter types the checker "
    "knows\n"
    "  (i64 edges, both arms of every Opt, empty/non-empty Lists, every ADT constructor, "
    "each\n"
    "  record field) and the property is checked over every generated round; on failure the\n"
    "  counterexample is SHRUNK to a minimal input. Py-runtime scope (docs/prop-test.md); "
    "the\n"
    "  cross-tier differential-fuzzer horizon is noted there, not built."
)


def prop_units(ir: dict) -> list:
    """The document's `prop test` units (docs/prop-test.md).

    Reads the IR section directly — no cordis needed to enumerate or count
    them, so `revl test` on any tier can report how many were not run there.
    """
    units: list = []
    for unit in ir.get("prop_tests") or []:
        units.append({
            "name": unit.get("name"),
            "params": [dict(p) for p in unit.get("params") or []],
            "body": unit.get("body") or [],
        })
    return units


def _parse_prop_type(type_str: str) -> tuple:
    """`"List[Opt[Int]]"` -> `("List", ["Opt[Int]"])`; `"Int"` -> `("Int", [])`.

    A tiny, self-contained splitter for the canonical type spellings lowering
    produces (no function types reach a prop-test parameter), so fault.py needs
    no import from the checker.
    """
    text = type_str.strip()
    if text.endswith("]") and "[" in text:
        head, _, rest = text.partition("[")
        inner = rest[:-1]
        args: list = []
        depth = 0
        current = ""
        for char in inner:
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            if char == "," and depth == 0:
                args.append(current.strip())
                current = ""
            else:
                current += char
        if current.strip():
            args.append(current.strip())
        return head.strip(), args
    return text, []


_PROP_PRIMITIVES = {"Int", "Int32", "Bool", "Str", "Float", "F64", "Num"}


def _case_is_recursive(payload: str | None, adt_head: str) -> bool:
    """Whether an ADT case payload refers back to its own ADT (a recursive
    constructor), so the depth bound knows to prefer base cases near the limit."""
    if payload is None:
        return False
    head, args = _parse_prop_type(payload)
    if head == adt_head:
        return True
    return any(_case_is_recursive(arg, adt_head) for arg in args)


# --- construction (mirrors backends/python/emit.py type emission) -----------


def _make_record(module, name: str, spec: dict, field_values: dict):
    cls = getattr(module, name)
    return cls(*[field_values[field] for field in spec.get("fields", {})])


def _make_case(module, case_name: str, payload_value):
    cls = getattr(module, case_name)
    return cls() if payload_value is _NO_PAYLOAD else cls(payload_value)


_NO_PAYLOAD = object()   # sentinel: construct a no-payload ADT case


# --- edge generation (guarantees coverage) ----------------------------------


def _edge_values(type_str: str, types: dict, module, depth: int = 0) -> list:
    """A deterministic, finite list of representative values for *type_str* —
    the i64 edges, both Opt arms, empty/one/two-element Lists, every ADT
    constructor, and each record field varied through its edges.  Used for the
    coverage-guaranteeing rounds (every entry here is actually run)."""
    head, args = _parse_prop_type(type_str)
    if head in ("Int",):
        return list(_I64_EDGES)
    if head == "Int32":
        return list(_I32_EDGES)
    if head == "Bool":
        return [False, True]
    if head == "Str":
        return list(_STR_EDGES)
    if head in ("Float", "F64", "Num"):
        return list(_FLOAT_EDGES)
    if head == "Opt" and args:
        inner = _edge_values(args[0], types, module, depth + 1)
        return [None] + inner[:3]           # None + a few Some(...) values
    if head == "List" and args:
        inner = _edge_values(args[0], types, module, depth + 1)
        first = inner[:2]
        return [[], first[:1], first]        # empty, one, two
    spec = types.get(head) if head and not args else None
    if spec and spec.get("kind") == "record":
        fields = spec.get("fields", {})
        base = {name: _edge_values(ftype, types, module, depth + 1)[0]
                for name, ftype in fields.items()}
        values = [_make_record(module, head, spec, base)]
        for name, ftype in fields.items():
            for edge in _edge_values(ftype, types, module, depth + 1)[:3]:
                variant = dict(base)
                variant[name] = edge
                values.append(_make_record(module, head, spec, variant))
        return values
    if spec and spec.get("kind") == "variant":
        values = []
        for case in spec.get("cases") or []:
            payload = case.get("payload")
            if payload is None:
                values.append(_make_case(module, case["name"], _NO_PAYLOAD))
            elif depth >= _PROP_DEPTH and _case_is_recursive(payload, head):
                continue                     # too deep to expand a recursive case
            else:
                for pv in _edge_values(payload, types, module, depth + 1)[:2]:
                    values.append(_make_case(module, case["name"], pv))
        if not values:                       # all cases recursive at the limit
            for case in spec.get("cases") or []:
                if case.get("payload") is None:
                    values.append(_make_case(module, case["name"], _NO_PAYLOAD))
        return values
    raise ValueError(f"prop generator: unsupported type {type_str!r}")


# --- random generation (search) ---------------------------------------------


def _gen_random(type_str: str, types: dict, module, rng, depth: int = 0):
    head, args = _parse_prop_type(type_str)
    if head == "Int":
        return rng.choice(_I64_EDGES + [rng.randint(_I64_MIN, _I64_MAX)])
    if head == "Int32":
        return rng.choice(_I32_EDGES + [rng.randint(_I32_MIN, _I32_MAX)])
    if head == "Bool":
        return rng.choice([False, True])
    if head == "Str":
        length = rng.randint(0, 8)
        return rng.choice(_STR_EDGES + ["".join(rng.choice(_STR_ALPHABET)
                                                for _ in range(length))])
    if head in ("Float", "F64", "Num"):
        return rng.choice(_FLOAT_EDGES + [round(rng.uniform(-1e6, 1e6), 3)])
    if head == "Opt" and args:
        if rng.random() < 0.3:
            return None
        return _gen_random(args[0], types, module, rng, depth + 1)
    if head == "List" and args:
        cap = 1 if depth >= _PROP_DEPTH else _PROP_LIST_MAX
        return [_gen_random(args[0], types, module, rng, depth + 1)
                for _ in range(rng.randint(0, cap))]
    spec = types.get(head) if head and not args else None
    if spec and spec.get("kind") == "record":
        return _make_record(module, head, spec, {
            name: _gen_random(ftype, types, module, rng, depth + 1)
            for name, ftype in spec.get("fields", {}).items()})
    if spec and spec.get("kind") == "variant":
        cases = list(spec.get("cases") or [])
        if depth >= _PROP_DEPTH:
            base = [c for c in cases if not _case_is_recursive(c.get("payload"), head)]
            cases = base or cases
        case = rng.choice(cases)
        payload = case.get("payload")
        if payload is None:
            return _make_case(module, case["name"], _NO_PAYLOAD)
        return _make_case(module, case["name"],
                          _gen_random(payload, types, module, rng, depth + 1))
    raise ValueError(f"prop generator: unsupported type {type_str!r}")


# --- coverage observation (a fold over what was generated) -------------------


def _new_coverage() -> dict:
    return {"int_edges": set(), "bool": set(), "str": set(),
            "opt": set(), "list": set(), "float": set(),
            "adt": {}}


def _observe(value, type_str: str, types: dict, cov: dict) -> None:
    """Record which type boundaries *value* exercised — a pure fold, so the
    coverage claim is testable and never depends on runtime objects beyond the
    value's own class name."""
    head, args = _parse_prop_type(type_str)
    if head == "Int" and value in _I64_EDGES:
        cov["int_edges"].add(value)
    elif head == "Int32" and value in _I32_EDGES:
        cov["int_edges"].add(value)
    elif head == "Bool":
        cov["bool"].add(bool(value))
    elif head == "Str":
        cov["str"].add("empty" if value == "" else "nonempty")
    elif head in ("Float", "F64", "Num"):
        cov["float"].add("zero" if value == 0.0 else "nonzero")
    elif head == "Opt" and args:
        if value is None:
            cov["opt"].add("None")
        else:
            cov["opt"].add("Some")
            _observe(value, args[0], types, cov)
    elif head == "List" and args:
        cov["list"].add("empty" if not value else "nonempty")
        for item in value:
            _observe(item, args[0], types, cov)
    else:
        spec = types.get(head) if head and not args else None
        if spec and spec.get("kind") == "record":
            for name, ftype in spec.get("fields", {}).items():
                _observe(getattr(value, name), ftype, types, cov)
        elif spec and spec.get("kind") == "variant":
            case_name = type(value).__name__
            cov["adt"].setdefault(head, set()).add(case_name)
            for case in spec.get("cases") or []:
                if case["name"] == case_name and case.get("payload") is not None:
                    _observe(value.value, case["payload"], types, cov)


def _coverage_report(params: list, cov: dict, types: dict) -> list:
    """Per-parameter coverage lines — what the generators actually reached."""
    lines: list = []
    for param in params:
        head, args = _parse_prop_type(param["type"])
        detail = None
        if head in ("Int", "Int32"):
            detail = f"{len(cov['int_edges'])} i64 edge value(s) visited"
        elif head == "Bool":
            detail = f"{len(cov['bool'])}/2 boolean value(s)"
        elif head == "Opt":
            detail = "arms: " + (", ".join(sorted(cov["opt"])) or "none")
        elif head == "List":
            detail = "lengths: " + (", ".join(sorted(cov["list"])) or "none")
        if detail:
            lines.append(f"    coverage {param['name']}: {param['type']} — {detail}")

    reported: set = set()

    def walk_adts(type_str: str, seen_types: frozenset = frozenset()) -> None:
        h, a = _parse_prop_type(type_str)
        if h in ("Opt", "List") and a:
            walk_adts(a[0], seen_types)
            return
        if h in seen_types:                   # a recursive type — already noted
            return
        spec = types.get(h) if h and not a else None
        if spec and spec.get("kind") == "variant":
            if h not in reported:
                declared = [c["name"] for c in spec.get("cases") or []]
                seen = cov["adt"].get(h, set())
                missing = [c for c in declared if c not in seen]
                note = "all constructors visited" if not missing else \
                       f"MISSED {', '.join(missing)}"
                lines.append(f"    coverage {h}: {len(seen)}/{len(declared)} "
                             f"constructor(s) — {note}")
                reported.add(h)
            for case in spec.get("cases") or []:
                if case.get("payload"):
                    walk_adts(case["payload"], seen_types | {h})
        elif spec and spec.get("kind") == "record":
            for ftype in spec.get("fields", {}).values():
                walk_adts(ftype, seen_types | {h})

    for param in params:
        walk_adts(param["type"])
    return lines


# --- shrinking --------------------------------------------------------------


def _shrink_value(value, type_str: str, types: dict, module):
    """Yield strictly-smaller candidates for *value*, closer-to-minimal first.

    "Smaller" is: an integer nearer zero, a shorter/emptier string or list, a
    `Some` collapsed to `None`, a payload case collapsed to a base case, a
    record with one field shrunk — the reductions a human would try to boil a
    counterexample down to its essence."""
    head, args = _parse_prop_type(type_str)
    if head in ("Int", "Int32"):
        seen = set()
        for cand in (0, value // 2, value - (1 if value > 0 else -1)):
            if abs(cand) < abs(value) and cand not in seen:
                seen.add(cand)
                yield cand
        return
    if head in ("Float", "F64", "Num"):
        for cand in (0.0, value / 2):
            if abs(cand) < abs(value):
                yield cand
        return
    if head == "Bool":
        if value:
            yield False
        return
    if head == "Str":
        if value:
            yield ""
            for i in range(len(value)):
                yield value[:i] + value[i + 1:]
        return
    if head == "Opt" and args:
        if value is None:
            return
        yield None                            # collapse Some(x) to None
        yield from _shrink_value(value, args[0], types, module)
        return
    if head == "List" and args:
        if value:
            yield []
            for i in range(len(value)):
                yield value[:i] + value[i + 1:]   # drop one element
            for i in range(len(value)):
                for smaller in _shrink_value(value[i], args[0], types, module):
                    yield value[:i] + [smaller] + value[i + 1:]
        return
    spec = types.get(head) if head and not args else None
    if spec and spec.get("kind") == "record":
        fields = list(spec.get("fields", {}).items())
        current = {name: getattr(value, name) for name, _ in fields}
        for name, ftype in fields:
            for smaller in _shrink_value(current[name], ftype, types, module):
                variant = dict(current)
                variant[name] = smaller
                yield _make_record(module, head, spec, variant)
        return
    if spec and spec.get("kind") == "variant":
        case_name = type(value).__name__
        # first, collapse to any base (no-payload) case — structurally smaller
        for case in spec.get("cases") or []:
            if case.get("payload") is None and case["name"] != case_name:
                yield _make_case(module, case["name"], _NO_PAYLOAD)
        # then shrink this case's own payload
        for case in spec.get("cases") or []:
            if case["name"] == case_name and case.get("payload") is not None:
                for smaller in _shrink_value(value.value, case["payload"], types, module):
                    yield _make_case(module, case["name"], smaller)
        return


def _shrink_args(args: tuple, param_types: list, types: dict, module, run_once) -> tuple:
    """Greedily minimise a failing *args* tuple: repeatedly replace one
    component with a smaller value that still fails, until no single reduction
    fails.  Bounded by ``_PROP_MAX_SHRINK`` trials so a pathological property
    cannot loop forever."""
    current = list(args)
    trials = 0
    improved = True
    while improved and trials < _PROP_MAX_SHRINK:
        improved = False
        for index in range(len(current)):
            for candidate in _shrink_value(current[index], param_types[index], types, module):
                trials += 1
                if trials > _PROP_MAX_SHRINK:
                    break
                trial = list(current)
                trial[index] = candidate
                ok, _reason = run_once(tuple(trial))
                if not ok:
                    current[index] = candidate
                    improved = True
                    break
            if improved:
                break
    return tuple(current)


# --- the driver -------------------------------------------------------------


def _load_py_emitter():
    """Import the cordis-py backend *emitter only* (no cordis runtime): a prop
    test's body is a pure function, so it needs the emitter to lower+exec it but
    never a live ``Context``.  Returns the ``emit`` module."""
    backend_dir = BACKENDS / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import emit  # noqa: PLC0415 — backend import after path setup
    return emit


def _prop_module(ir: dict, unit: dict, index: int, emit):
    """Emit and exec a pure module carrying *unit*'s property as an ordinary
    function ``_revl_prop_<index>(params…)``; return ``(module, fn)``.

    The property lowers to a normal emitted function — the same injection trick
    the fault runner uses for its `fail` step — so no backend emitter change is
    needed.  Only the pure surface (types, functions, externs) is kept; the
    component/test/fault/prop sections are dropped so the module imports nothing
    from cordis and execs standalone.
    """
    # the emitter reserves leading-underscore names, so the synthetic function
    # is camel-cased without one (and stays clear of user fn names)
    fn_name = f"revlProp{index}"
    pure = dict(ir)
    for section in ("components", "tests", "fault_tests", "prop_tests", "manifest",
                    "services", "holes"):
        pure.pop(section, None)
    pure["functions"] = list(ir.get("functions") or []) + [{
        "name": fn_name,
        "params": list(unit["params"]),
        "returns": None,
        "public": False,
        "body": list(unit["body"]),
    }]
    module = types.ModuleType(f"revl_prop_{abs(hash(unit['name'])):x}")
    sys.modules[module.__name__] = module
    try:
        source = emit.emit(pure)
        exec(compile(source, f"<revl-prop {unit['name']}>", "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(module.__name__, None)
        raise
    return module, getattr(module, fn_name)


def _run_once_factory(fn):
    """A ``run_once(args) -> (ok, reason)`` over the emitted property function.

    A failing `assert` (AssertionError) is the property being false; any other
    exception (an overflow at an i64 edge, a host precondition) is equally a
    counterexample — the property did not hold for that input — and is reported
    with its type, never swallowed."""
    def run_once(args: tuple) -> tuple:
        try:
            fn(*args)
        except AssertionError as error:
            return (False, str(error).strip() or "assertion failed")
        except Exception as error:  # noqa: BLE001 — any raise is a counterexample
            return (False, f"{type(error).__name__}: {error}")
        return (True, None)
    return run_once


def _render_arg(value) -> str:
    """A short, faithful rendering of a generated argument for the report."""
    cls = type(value).__name__
    if isinstance(value, (int, float, str, bool)) or value is None:
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_render_arg(v) for v in value) + "]"
    if hasattr(value, "value"):              # ADT payload case
        return f"{cls}({_render_arg(value.value)})"
    fields = getattr(value, "__dataclass_fields__", None)
    if fields:                               # record
        inner = ", ".join(f"{name}={_render_arg(getattr(value, name))}" for name in fields)
        return f"{cls}({inner})"
    return f"{cls}()"                        # no-payload ADT case


def _render_args(params: list, args: tuple) -> str:
    return ", ".join(f"{p['name']}={_render_arg(v)}" for p, v in zip(params, args))


def _prop_dossier(results: list, random_rounds: int) -> dict:
    """Aggregate ``(unit, status, detail)`` into a gauntlet-shaped dossier.
    ``detail`` carries the counterexample (on fail) and the coverage lines.
    Pure."""
    props = []
    total = passed = failed = 0
    for unit, status, detail in results:
        total += 1
        ok = status == "pass"
        passed += ok
        failed += not ok
        entry = {
            "name": unit["name"],
            "params": [{"name": p["name"], "type": p["type"]} for p in unit["params"]],
            "status": status,
            "rounds": detail.get("rounds", 0),
        }
        if not ok and "counterexample" in detail:
            entry["counterexample"] = detail["counterexample"]
        if "error" in detail:
            entry["error"] = detail["error"]
        props.append(entry)
    return {
        "kind": "tested",
        "status": "failed" if failed else "passed",
        "roadmapItem": _PROP_ITEM,
        "title": _PROP_TITLE,
        "tier": "py",
        "note": "each `prop test` was checked over type-derived inputs (i64 edges, "
                "Opt arms, empty/non-empty Lists, every ADT constructor, record fields) "
                f"plus {random_rounds} random rounds; a failing input was shrunk to a "
                "minimal counterexample (docs/prop-test.md).",
        "counts": {
            "props": total,
            "passed": passed,
            "failed": failed,
            "randomRounds": random_rounds,
        },
        "properties": props,
    }


def _run_prop_unit(ir: dict, unit: dict, index: int, emit, rng,
                   random_rounds: int) -> tuple:
    """Run one `prop test`: emit its property, generate the edge rounds (which
    guarantee coverage) then the random rounds, find the first failing input,
    shrink it, and fold coverage.  Returns ``(status, detail)``."""
    types_ = ir.get("types") or {}
    params = unit["params"]
    param_types = [p["type"] for p in params]

    module, fn = _prop_module(ir, unit, index, emit)
    try:
        run_once = _run_once_factory(fn)

        # 1. the coverage-guaranteeing edge rounds: vary one parameter at a
        #    time through its representative values, the others held at a base.
        try:
            per_param_edges = [_edge_values(t, types_, module) for t in param_types]
        except Exception as error:  # noqa: BLE001 — a generator crash is a hard error
            return ("fail", {"rounds": 0,
                             "error": f"could not derive a generator: "
                                      f"{type(error).__name__}: {error}"})
        bases = tuple(edges[0] for edges in per_param_edges)
        edge_tuples: list = [bases]
        for i, edges in enumerate(per_param_edges):
            for value in edges:
                combo = list(bases)
                combo[i] = value
                edge_tuples.append(tuple(combo))

        # 2. random-search rounds
        random_tuples = [tuple(_gen_random(t, types_, module, rng) for t in param_types)
                         for _ in range(random_rounds)]

        all_tuples = edge_tuples + random_tuples

        # coverage is a fold over every input the generators produced
        cov = _new_coverage()
        for combo in all_tuples:
            for value, type_str in zip(combo, param_types):
                _observe(value, type_str, types_, cov)
        coverage_lines = _coverage_report(params, cov, types_)

        rounds = len(all_tuples)
        failing = None
        for combo in all_tuples:
            ok, reason = run_once(combo)
            if not ok:
                failing = (combo, reason)
                break

        if failing is None:
            return ("pass", {"rounds": rounds, "coverage": coverage_lines})

        minimal = _shrink_args(failing[0], param_types, types_, module, run_once)
        _ok, reason = run_once(minimal)
        return ("fail", {
            "rounds": rounds,
            "coverage": coverage_lines,
            "counterexample": {
                "args": _render_args(params, minimal),
                "reason": reason,
                "raw": _render_args(params, failing[0]),
            },
        })
    finally:
        sys.modules.pop(module.__name__, None)


def run_prop_units(ir: dict, units: list, out=None,
                   random_rounds: int = _PROP_RANDOM_ROUNDS) -> tuple:
    """Run every `prop test` on the py reference tier; ``(failures, dossier)``.

    Unlike the fault/round-trip runners this needs only the backend *emitter*
    (a prop body is pure), so it does not raise when cordis is absent."""
    import random  # noqa: PLC0415 — stdlib, only needed when the runner runs

    printer = (lambda line: print(line)) if out is None else out
    emit = _load_py_emitter()

    for line in _PROP_HEADER.split("\n"):
        printer(line)

    results: list = []
    for index, unit in enumerate(units):
        # a per-property seed keeps a run reproducible while still exploring
        rng = random.Random(f"revl-prop:{unit['name']}")
        try:
            status, detail = _run_prop_unit(ir, unit, index, emit, rng, random_rounds)
        except Exception as error:  # noqa: BLE001 — a driver crash is a failure
            status, detail = "fail", {"rounds": 0,
                                      "error": f"the prop-test driver raised "
                                               f"{type(error).__name__}: {error}"}
        results.append((unit, status, detail))

        if status == "pass":
            printer(f"PASS {unit['name']}: property held over {detail['rounds']} "
                    f"generated input(s)")
        else:
            if "error" in detail:
                printer(f"FAIL {unit['name']}: {detail['error']}")
            else:
                ce = detail["counterexample"]
                printer(f"FAIL {unit['name']}: property is false")
                printer(f"    counterexample (shrunk): {ce['args']}")
                printer(f"    because: {ce['reason']}")
                if ce["raw"] != ce["args"]:
                    printer(f"    (first failing input was: {ce['raw']})")
        for line in detail.get("coverage") or []:
            printer(line)

    dossier = _prop_dossier(results, random_rounds)
    counts = dossier["counts"]
    printer(f"checked {counts['props']} property/properties: {counts['passed']} held, "
            f"{counts['failed']} broke")
    return counts["failed"], dossier


def prop_dossier(ir: dict, random_rounds: int = _PROP_RANDOM_ROUNDS) -> dict:
    """The prop run as a structured dossier only (no printing).  Returns an
    empty ``passed`` dossier when the document declares no `prop test`."""
    units = prop_units(ir)
    if not units:
        return {
            "kind": "tested", "status": "passed", "roadmapItem": _PROP_ITEM,
            "title": _PROP_TITLE, "tier": "py",
            "note": "no `prop test` declared.",
            "counts": {"props": 0, "passed": 0, "failed": 0, "randomRounds": random_rounds},
            "properties": [],
        }
    return run_prop_units(ir, units, out=lambda _line: None, random_rounds=random_rounds)[1]

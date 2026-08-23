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

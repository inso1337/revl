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


async def _drive(ir: dict, unit: dict, emit, runtime_mod, Context, FiberState) -> _Outcome:
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
            #    against its real providers rather than a stub
            for name in order:
                if name == target:
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


def run_fault_units(ir: dict, units: list, out=None) -> tuple[int, int]:
    """Run *units* against the py reference tier; ``(failures, total)``.

    Raises ``ModuleNotFoundError`` when the cordis-py runtime is absent — the
    caller decides whether that is a skip or an error.
    """
    printer = (lambda line: print(line)) if out is None else out
    backend_dir = BACKENDS / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import emit  # noqa: PLC0415 — backend import after path setup
    import runtime as runtime_mod  # noqa: PLC0415
    from cordis import Context  # noqa: PLC0415
    from cordis.fiber import FiberState  # noqa: PLC0415

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

"""Auto-mocks: every `service` declaration ships a free fake (roadmap item 60).

A consumer component cannot be developed or tested until something provides its
`requires`.  Today that something is a *real* provider — a live database, a
running cache — and standing one up is the setup tax that keeps a consumer's
`lifecycle test` from being cheap.  This module removes the tax: from a
`service` declaration alone it derives an **in-memory mock provider** whose each
operation returns an item-37-generated value of the declared return type
(typed, seeded, deterministic), so booting a composition in "mock world" needs
zero setup code.

The mock is cheap *by construction*, and that is the whole design bet: the
service's operation types are already known to the checker, and item 37's
type-derived generators (`src/revl/fault.py`) already synthesize a value for any
such type.  So the mock reuses those generators verbatim — it invents no new
value-synthesis machinery — seeding each operation with a fixed, per-operation
seed so a mocked run is reproducible.

Emissions are the one operation a mock must NOT execute.  A `service` operation
classified `emission` crosses a boundary the moment it runs (docs/backend-ir.md
§6.1); a mock that made that crossing would defeat the point of testing in
isolation.  So a mocked emission is **recorded, not crossed**: the mock *counts*
the crossing and records the arguments that *would* have crossed, then returns a
generated value like any other op.  The recording is itself an assertion surface
— the mock-world report says exactly what the composition would have emitted,
so a test can pin down "this activation emits `db.execute` once, with this SQL"
without a real database ever hearing about it.

Scope, like `fault test` / `verified effect` / `prop test`, is the **py
reference tier, in-memory**: the mock is a runtime-constructed cordis component
(a `provide` + `ctx.set`, exactly the shape the emitter renders for a real
provider), driven by the py runner.  Nothing here is emitted — no `emit.py` is
touched — so a mock is a test-time provider, never code that ships.
"""

from __future__ import annotations

import random
from typing import Any, Callable, Optional

# item-37's type-derived value generators live in fault.py; this module REUSES
# them (read-only) rather than re-deriving value synthesis.  `_gen_random` is
# the seeded, type-directed generator; `_parse_prop_type` splits a canonical
# type spelling; `_render_arg` renders a generated/host value for the report.
from . import fault


# ---------------------------------------------------------------------------
# response synthesis — one item-37-generated value per operation
# ---------------------------------------------------------------------------


def gen_value(type_str: Optional[str], types: dict, module: Any, rng: random.Random):
    """An item-37-generated value of *type_str* (``None``/``Unit`` -> ``None``).

    Delegates to :func:`fault._gen_random` — the same seeded, type-directed
    generator a `prop test` uses — so a mocked response visits the same typed
    value space (i64 edges, both Opt arms, every ADT constructor, record
    fields).  Determinism is the caller's *rng*: seed it per operation and a
    mocked run is reproducible.

    Robust by design: a return type the generator cannot synthesize (an
    undeclared record like `Row`, a `Result`/`Map` head the prop generator does
    not cover, an `extern` resource handle) must not crash a mock — a mock that
    raised would be worse than useless.  On any generator miss it falls back to
    the type's structural zero (`None` for `Opt`/scalars, `[]` for `List`, `{}`
    for `Map`, `Ok(<inner>)` for `Result` when the runtime carries `Ok`), which
    is a valid inhabitant of the declared shape.
    """
    if not type_str or type_str == "Unit":
        return None
    try:
        return fault._gen_random(type_str, types, module, rng)
    except Exception:  # noqa: BLE001 — a generator miss is a fallback, never a crash
        head, args = fault._parse_prop_type(type_str)
        if head == "Opt":
            return None
        if head == "List":
            return []
        if head == "Map":
            return {}
        if head == "Result" and args:
            ok_cls = getattr(module, "Ok", None)
            if ok_cls is not None:
                try:
                    return ok_cls(gen_value(args[0], types, module, rng))
                except Exception:  # noqa: BLE001
                    return None
        return None


# ---------------------------------------------------------------------------
# recorded-not-crossed emissions
# ---------------------------------------------------------------------------


class MockRecorder:
    """The ledger of boundary crossings a mock world *counted but never made*.

    Every `emission`-classified operation a mocked provider is asked to perform
    lands here instead of crossing.  The ledger is the mock world's assertion
    surface: after a lifecycle test runs, it says precisely what the composition
    would have emitted (which operation, how many times, with what arguments).
    """

    def __init__(self) -> None:
        self.crossings: list[dict] = []

    def record(self, service: str, key: str, method: str,
               param_names: list, args: tuple, returns: Optional[str]) -> None:
        rendered = ", ".join(
            f"{name}={fault._render_arg(value)}"
            for name, value in zip(param_names or [f"arg{i}" for i in range(len(args))], args)
        )
        self.crossings.append({
            "service": service,
            "key": key,
            "method": method,
            "args": rendered,
            "returns": returns,
        })

    def count(self) -> int:
        return len(self.crossings)

    def report(self) -> list:
        """Human-readable lines describing every recorded-not-crossed emission,
        grouped by the operation that would have crossed.  Empty when the mock
        world crossed nothing."""
        if not self.crossings:
            return []
        grouped: dict = {}
        order: list = []
        for entry in self.crossings:
            ck = (entry["key"], entry["method"], entry["service"])
            if ck not in grouped:
                grouped[ck] = []
                order.append(ck)
            grouped[ck].append(entry)
        lines = [
            "recorded-not-crossed emissions (the mock counted each boundary "
            "crossing; it never made one):",
        ]
        for key, method, service in order:
            entries = grouped[(key, method, service)]
            n = len(entries)
            lines.append(f"  {key}.{method} ({service}) — {n} crossing"
                         f"{'s' if n != 1 else ''} recorded, none made")
            for entry in entries:
                would = entry["args"] or "(no arguments)"
                lines.append(f"      would have emitted: {would}")
        return lines


# ---------------------------------------------------------------------------
# the mock provider — an in-memory cordis component
# ---------------------------------------------------------------------------


def _make_operation(service: str, key: str, method: str, spec: dict,
                    types: dict, module: Any, recorder: MockRecorder) -> Callable:
    """One mocked operation: returns an item-37-generated value; if the
    operation is `emission`-classified it first *records* the crossing (counted,
    with the arguments that would have crossed) instead of making it.

    Seeded per operation (`revl-mock:<service>:<key>:<method>`) so the sequence
    of responses is deterministic and reproducible across runs; the rng advances
    per call, so repeated calls return a deterministic *sequence* of typed values
    rather than one frozen value.
    """
    param_names = [p.get("name") for p in spec.get("params") or []]
    returns = spec.get("returns")
    is_emission = bool(spec.get("emission"))
    is_async = bool(spec.get("async"))
    rng = random.Random(f"revl-mock:{service}:{key}:{method}")

    def _respond(args: tuple):
        if is_emission:
            recorder.record(service, key, method, param_names, args, returns)
        return gen_value(returns, types, module, rng)

    if is_async:
        async def _op(*args):
            return _respond(args)
        return _op

    def _op(*args):
        return _respond(args)
    return _op


def _build_impl(service: str, key: str, service_spec: dict,
                types: dict, module: Any, recorder: MockRecorder) -> Any:
    """The in-memory provider object — one attribute per service operation.

    Its methods are plain functions (no ``self``): the runtime resolves a
    provision through ``root.get(key)`` and calls ``impl.method(*args)``, the
    same call shape a real provider gets."""
    impl = type(f"_Mock_{service}", (), {})()
    for method, spec in (service_spec.get("methods") or {}).items():
        setattr(impl, method, _make_operation(service, key, method, spec,
                                              types, module, recorder))
    return impl


def make_mock_component(key: str, service: str, service_spec: dict,
                        types: dict, module: Any, recorder: MockRecorder,
                        Frame: Any) -> dict:
    """A runtime-constructed cordis component that provides *key* with a mock of
    *service* — the exact shape the emitter renders for a real provider
    (``yield ctx.provide(key)`` for the revertible provision + ``ctx.set(key,
    impl)`` for the object), so it plugs, resolves, and reverts like any
    provider and leaves no residue on teardown.

    In-memory only: the returned dict is fed straight to ``runtime.plug`` — it
    is never emitted.
    """
    name = f"_RevlMock_{service}_{key}"

    def _apply(ctx, config):  # noqa: ARG001 — a mock takes no config
        frame = Frame(ctx, name)

        def _body():
            impl = _build_impl(service, key, service_spec, types, module, recorder)
            # runtime-derived revertible provision (R5): ctx.provide's own
            # disposer withdraws the provision on teardown — the mock leaves
            # nothing behind, exactly like an emitted provider.
            yield ctx.provide(key)
            ctx.set(key, impl)
            yield frame.drain

        frame.install(_body)

    return {"name": name, "inject": [], "apply": _apply}


# ---------------------------------------------------------------------------
# the mock-world lifecycle driver — `revl test --mock-requires`
# ---------------------------------------------------------------------------
#
# A `lifecycle test` scripts a live composition: load components, call through
# provision keys, assert, unload, assert no residue (docs/syntax-2.0.md §7.1).
# In mock world the driver runs that same script, but a `load C` first
# auto-satisfies any of C's `requires` the composition has NOT already provided
# — with a generated mock.  So a consumer can be lifecycle-tested with ZERO real
# providers and zero setup code: `load <consumer>` alone brings its whole
# `requires` surface up as mocks.
#
# It reuses the fault runner's runtime seam verbatim (`_load_py_tier`,
# `_snapshot`, `_unreleased_host_resources`, `_flush`) and the backend's own
# `_expr` renderer for call arguments / assert expressions — so no expression
# evaluator is re-implemented and no emitter is touched.


def _strip_to_base(ir: dict) -> dict:
    """*ir* minus the sections that must not ride into the emitted base module.

    The mock driver replays each `lifecycle test` script itself (never the
    emitted ``REVL_TESTS``), but the module must still be emitted with the
    ``tests`` section PRESENT: the driver evaluates call arguments and assert
    expressions through the backend's own ``_expr`` renderer, and the renderer
    references module-scope helpers (``_revl_i64``/``_revl_i32``/``_revl_div``/
    ``_revl_field``/``_revl_ftoa``, the built-in ``Ok``/``Err`` cases) whose
    emission the emitter gates on scanning the *whole* IR — strip the tests and
    a test-body expression that needs one of them NameErrors (the fault runner
    keeps the full document for exactly this reason). The emitted lifecycle
    functions are inert: the driver never calls them, and their cordis import
    lives inside the function body, so the module execs without a runtime.

    ``fault_tests`` is stripped because the injection scheme splices a `fail`
    step into component bodies — a fault-swept component is not the composition
    mock world wants to boot. ``prop_tests`` is stripped as dead weight (the py
    emitter ignores it, but it is not the surface mock world runs).
    """
    base = dict(ir)
    for section in ("fault_tests", "prop_tests"):
        base.pop(section, None)
    return base


def _component(ir: dict, name: str) -> dict:
    return next((c for c in ir.get("components") or [] if c.get("name") == name), {})


def _residue_delta(baseline: dict, now: dict, events: list) -> list:
    """How the host runtime's residue now differs from *baseline* (R4), plus any
    host resource acquired during the test and never released (R1) — the same
    two halves the emitted `assert no_residue` harness checks."""
    diffs: list = []
    for field_ in ("registry", "provisions", "effects", "listeners"):
        if now[field_] != baseline[field_]:
            diffs.append(f"{field_}: {baseline[field_]!r} -> {now[field_]!r} (R4)")
    unreleased = fault._unreleased_host_resources(events)
    if unreleased:
        diffs.append("host resources never released: " + ", ".join(unreleased) + " (R1)")
    return diffs


async def _drive_mock_lifecycle(ir: dict, test: dict, module: Any, emit_mod: Any,
                                runtime_mod: Any, Context: Any,
                                recorder: MockRecorder) -> None:
    """Run one `lifecycle test` in mock world; raise ``AssertionError`` on any
    failed assertion / residue check (the caller turns that into a FAIL)."""
    Frame = runtime_mod.Frame
    types = ir.get("types") or {}
    where = f"lifecycle test {test['name']!r} (mock world)"

    root = Context()
    events: list = []
    runtime_mod.set_trace(events.append)
    fibers: dict = {}          # component name -> fiber (real, loaded consumers)
    mock_fibers: dict = {}     # provision key -> the mock provider's fiber
    mock_refs: dict = {}       # provision key -> live consumers relying on the mock
    bindings: dict = {}        # `let x = call ...` bindings, by name
    try:
        baseline = fault._snapshot(root)
        for step in test.get("body") or []:
            kind = step.get("step")

            if kind == "load":
                comp_name = step["component"]
                comp = _component(ir, comp_name)
                # auto-mock every `requires` key the composition has not already
                # satisfied (a real provider loaded earlier keeps its place).
                for local, service in (comp.get("requires") or {}).items():
                    if root.get(local) is None and local not in mock_fibers:
                        spec = (ir.get("services") or {}).get(service) or {}
                        mock = make_mock_component(local, service, spec, types,
                                                   module, recorder, Frame)
                        mock_fibers[local] = runtime_mod.plug(root, mock, {})
                        await fault._flush()
                    if local in mock_fibers:
                        mock_refs[local] = mock_refs.get(local, 0) + 1
                config = {name: eval(emit_mod._expr(value), module.__dict__, bindings)  # noqa: S307
                          for name, value in (step.get("config") or {}).items()}
                fibers[comp_name] = runtime_mod.plug(root, getattr(module, comp_name), config)
                await fault._flush()

            elif kind == "unload":
                comp_name = step["component"]
                comp = _component(ir, comp_name)
                fiber = fibers.pop(comp_name, None)
                if fiber is not None:
                    await fiber.dispose()
                    await fault._flush()
                # release the mocks this consumer was relying on; drop a mock
                # when its last consumer is gone, so residue returns to baseline.
                for local in (comp.get("requires") or {}):
                    if local in mock_refs:
                        mock_refs[local] -= 1
                        if mock_refs[local] <= 0:
                            del mock_refs[local]
                            mock_fiber = mock_fibers.pop(local, None)
                            if mock_fiber is not None:
                                await mock_fiber.dispose()
                                await fault._flush()

            elif kind == "call":
                key = step["key"]
                impl = root.get(key)
                if impl is None:
                    raise AssertionError(
                        f"{where}: no provider for key {key!r} — its component is "
                        f"loaded but not ACTIVE (a component with an unmet `requires` "
                        f"stays PENDING, R2)")
                args = [eval(emit_mod._expr(arg), module.__dict__, bindings)  # noqa: S307
                        for arg in step.get("args") or []]
                result = getattr(impl, step["method"])(*args)
                if hasattr(result, "__await__"):
                    result = await result
                if step.get("bind"):
                    bindings[step["bind"]] = result
                await fault._flush()

            elif kind == "assert":
                ok = eval(emit_mod._expr(step["expr"]), module.__dict__, bindings)  # noqa: S307
                if not ok:
                    raise AssertionError(f"{where}: assertion failed")

            elif kind == "assert_no_residue":
                diffs = _residue_delta(baseline, fault._snapshot(root), events)
                if diffs:
                    raise AssertionError(f"{where}: residue — " + " | ".join(diffs))

            else:  # pragma: no cover — the lowerer emits nothing else
                raise AssertionError(f"{where}: unknown lifecycle step {kind!r}")
    finally:
        runtime_mod.set_trace(None)
        for fiber in reversed(list(fibers.values())):
            try:
                await fiber.dispose()
            except Exception:  # noqa: BLE001 — cleanup must not mask a result
                pass
        for fiber in reversed(list(mock_fibers.values())):
            try:
                await fiber.dispose()
            except Exception:  # noqa: BLE001
                pass
        await fault._flush()


def lifecycle_tests(ir: dict) -> list:
    """The document's `lifecycle test` blocks — the ones the mock world runs."""
    return [t for t in ir.get("tests") or [] if t.get("lifecycle")]


def run_mock_requires(ir: dict, out=None) -> tuple[int, int]:
    """Run every `lifecycle test` in mock world on the py reference tier;
    ``(failures, total)``.

    Every `requires` a loaded component leaves unsatisfied is filled by a
    generated mock, so the composition boots with no real providers.  Raises
    ``ModuleNotFoundError`` when the cordis-py runtime is absent — the caller
    decides skip vs error, exactly as the fault runner does.
    """
    import asyncio  # noqa: PLC0415 — only needed when the runner runs

    printer = (lambda line: print(line)) if out is None else out
    emit_mod, runtime_mod, Context, _FiberState = fault._load_py_tier()

    tests = lifecycle_tests(ir)
    if not tests:
        printer("no `lifecycle test` to run in mock world")
        return (0, 0)

    # emit the base module once (types + components + services), register it so
    # the emitter's @dataclass records resolve, then keep the module object for
    # value construction and expression evaluation.
    import sys  # noqa: PLC0415
    import types as _types  # noqa: PLC0415
    source = emit_mod.emit(_strip_to_base(ir))
    module = _types.ModuleType("revl_mock_module")
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, "<revl-mock>", "exec"), module.__dict__)  # noqa: S102
    finally:
        sys.modules.pop(module.__name__, None)

    printer("auto-mocks — py reference tier (roadmap item 60)")
    printer("  Every `requires` a loaded component leaves unsatisfied is filled by a "
            "generated")
    printer("  mock: responses are item-37-typed/seeded/deterministic; an `emission` "
            "operation is")
    printer("  recorded-not-crossed (the mock counts the crossing, never makes it). "
            "Zero real")
    printer("  providers, zero setup code (docs/auto-mocks.md).")

    failures = 0
    for test in tests:
        recorder = MockRecorder()
        try:
            asyncio.run(_drive_mock_lifecycle(ir, test, module, emit_mod,
                                              runtime_mod, Context, recorder))
        except AssertionError as error:
            failures += 1
            printer(f"FAIL {test['name']} [mock world]: {error}")
        except Exception as error:  # noqa: BLE001 — a driver crash is a failure
            failures += 1
            printer(f"FAIL {test['name']} [mock world]: "
                    f"{type(error).__name__}: {error}")
        else:
            crossed = recorder.count()
            tail = (f" ({crossed} emission{'s' if crossed != 1 else ''} "
                    f"recorded-not-crossed)" if crossed else "")
            printer(f"PASS {test['name']} [mock world]{tail}")
        for line in recorder.report():
            printer(f"    {line}")

    printer(f"ran {len(tests)} lifecycle test(s) in mock world: "
            f"{len(tests) - failures} passed, {failures} failed")
    return failures, len(tests)

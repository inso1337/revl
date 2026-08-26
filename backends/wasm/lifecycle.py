"""wasm-tier lifecycle-test classification + spec building (roadmap item 142).

The revl-side half of the wasm lifecycle driver. It runs in the *revl*
interpreter (it needs the IR, not wasmtime): given a document's `lifecycle
test` blocks it decides which the substrate can actually express, and reduces
each runnable one to the scalar-only step script the cordis-wasm executor
(``lifecycle_harness.py``) drives on a live runtime.

A `lifecycle test` over wasm-expressible components *runs* (boots, calls,
unloads, checks R4/R1 residue) — it is no longer a blanket skip. What the
substrate genuinely cannot express is skipped *per test, with a reason*, never
faked as a pass:

* a `load … with { … }` — the wasm tier has no instantiation-config channel,
  so a configured component does not even lower (docs/wasm-capabilities.md);
* a `call` whose signature crosses a non-scalar boundary (Str/List/record/
  variant/Opt/Result/Float) — those cross as pointers into the module's own
  linear memory, which the Python host cannot marshal at the coeffect table
  seam (the once-mode boot only ever moves Int/Bool across it);
* an `assert` over a non-scalar value (e.g. `hit == Some("v")`) — same reason,
  there is nothing scalar to compare host-side;
* an `advance` step — timers (`every`/`after`, item 57) are not yet lowerable
  on the wasm tier (docs/time-coeffect.md), the same follow-on the `test.py`
  timer gate reports.

The scalar boundary is exactly the one the wasm README calls out: Int is i64,
Bool is i32, and everything richer needs the WIT/hosted tiers. Keeping the
type logic here (one place, with the IR's service table in hand) lets the
executor stay a thin scalar interpreter.
"""

from __future__ import annotations

# Int is i64, Bool is i32 (backends/wasm/README.md "Widths"); everything richer
# crosses the service boundary as a linear-memory pointer the host cannot read.
_SCALAR_TYPES = frozenset({"Int", "Bool"})


def provided_keys(ir: dict) -> dict[str, str]:
    """``{provision key -> service name}`` across every component in the doc."""
    provided: dict[str, str] = {}
    for comp in ir.get("components") or []:
        for key, service in (comp.get("provides") or {}).items():
            provided[key] = service
    return provided


def _is_scalar_type(surface) -> bool:
    return surface in _SCALAR_TYPES


def _scalar_expr(expr, scalar_binds: set[str]) -> bool:
    """True when *expr* evaluates to a scalar (Int/Bool) host-side.

    Only literals (Int/Bool), prior scalar bindings, and scalar arithmetic/
    comparison/boolean operators over them qualify. A `Some(..)`/`None`/string
    literal, a method call, or a reference to a non-scalar binding does not —
    the executor has no value for it, so the whole test is skipped honestly.
    """
    if not isinstance(expr, dict):
        return False
    kind = expr.get("kind")
    if kind == "lit":
        return isinstance(expr.get("value"), (int, bool))
    if kind == "var":
        return expr.get("name") in scalar_binds
    if kind == "unary":
        return expr.get("op") in ("!", "not", "-") and _scalar_expr(
            expr.get("operand"), scalar_binds)
    if kind == "bin":
        return expr.get("op") in (
            "==", "!=", "<", ">", "<=", ">=", "+", "-", "*",
            "&&", "and", "||", "or",
        ) and _scalar_expr(expr.get("left"), scalar_binds) and _scalar_expr(
            expr.get("right"), scalar_binds)
    return False


def classify(ir: dict, test: dict) -> tuple[bool, str]:
    """``(runnable, reason)`` — whether the wasm substrate can express *test*.

    ``runnable`` True means every step lowers to the scalar coeffect-table
    seam and the test should *run* on the live runtime. False pairs with a
    one-line reason for an honest per-test skip (never a false pass).
    """
    services = ir.get("services") or {}
    provided = provided_keys(ir)
    scalar_binds: set[str] = set()
    for step in test.get("body") or []:
        kind = step.get("step")
        if kind == "load":
            if step.get("config"):
                return (False,
                        f"loads `{step.get('component')}` with a `config` block — "
                        "the wasm tier has no instantiation-config channel, so a "
                        "configured component does not lower (docs/wasm-capabilities.md)")
        elif kind == "call":
            key, method = step.get("key"), step.get("method")
            service = provided.get(key)
            if service is None:
                return (False, f"calls `{key}.{method}`, but no loaded component "
                               f"provides key {key!r}")
            sig = ((services.get(service) or {}).get("methods") or {}).get(method)
            if sig is None:
                return (False, f"calls unknown method `{method}` on service {service!r}")
            for param in sig.get("params") or []:
                if not _is_scalar_type(param.get("type")):
                    return (False,
                            f"calls `{key}.{method}`, whose parameter `"
                            f"{param.get('name')}: {param.get('type')}` crosses a "
                            "non-scalar (Str/List/record/variant/Opt/Float) boundary "
                            "the wasm substrate cannot marshal from the host "
                            "(docs/wasm-capabilities.md)")
            for arg in step.get("args") or []:
                if not _scalar_expr(arg, scalar_binds):
                    return (False,
                            f"calls `{key}.{method}` with a non-scalar argument the "
                            "wasm substrate cannot pass across the coeffect seam")
            returns = sig.get("returns")
            bind = step.get("bind")
            if bind is not None:
                if returns is not None and not _is_scalar_type(returns):
                    return (False,
                            f"binds the result of `{key}.{method}` whose return "
                            f"`{returns}` is a non-scalar the host cannot read back "
                            "from the module's memory (docs/wasm-capabilities.md)")
                scalar_binds.add(bind)
        elif kind == "assert":
            if not _scalar_expr(step.get("expr"), scalar_binds):
                return (False,
                        "asserts over a non-scalar value (e.g. an Opt/Str/record "
                        "comparison), which the wasm substrate cannot evaluate "
                        "host-side (docs/wasm-capabilities.md)")
        elif kind == "advance":
            return (False,
                    "uses an `advance` step — timers (`every`/`after`, item 57) are "
                    "not yet lowerable on the wasm tier (docs/time-coeffect.md)")
        elif kind in ("unload", "assert_no_residue"):
            continue
        else:  # pragma: no cover — the lowerer emits only the shapes above
            return (False, f"uses an unsupported lifecycle step {kind!r}")
    return (True, "")


def build_spec_test(test: dict) -> dict:
    """Reduce a runnable lifecycle test to the executor's step script.

    A `call`'s IR ``method`` becomes the executor's ``op``; a `load`'s (empty,
    for a runnable test) ``config`` is dropped; every other step passes through
    unchanged (``assert`` carries its scalar expr AST, which the executor's
    scalar interpreter evaluates).
    """
    steps: list[dict] = []
    for step in test.get("body") or []:
        kind = step.get("step")
        if kind == "load":
            steps.append({"step": "load", "component": step["component"]})
        elif kind == "unload":
            steps.append({"step": "unload", "component": step["component"]})
        elif kind == "call":
            steps.append({
                "step": "call",
                "key": step["key"],
                "op": step["method"],
                "args": step.get("args") or [],
                "bind": step.get("bind"),
            })
        elif kind == "assert":
            steps.append({"step": "assert", "expr": step["expr"]})
        elif kind == "assert_no_residue":
            steps.append({"step": "assert_no_residue"})
    return {"name": test.get("name") or "lifecycle", "steps": steps}


# ---------------------------------------------------------------------------
# witnessed-effects teardown (item 243 Slice 2b, docs/design/teardown-
# contract.md): the first-party wasmtime-driving half of the two-phase
# accumulator `backends/wasm/emit.py` compiles into every component's
# `deactivate_step`/`deactivate`/`committed` exports.
#
# Everything above this line is pure Python (no wasmtime import) and runs in
# the *revl* process. This section is the one place in this file that talks
# to a live module, mirroring the split the module docstring already draws
# between "revl-side" (this file) and "cordis-wasm-side" (`lifecycle_harness.
# py`) — except this driver needs neither cordis-wasm nor its runtime module:
# it drives the compiled component directly against the `wasmtime` Python
# bindings, the same first-party pattern `test_accessor_exec.py`/
# `test_spawn_exec.py` already use for exec-level proofs. `wasmtime` is
# imported lazily inside `drive_teardown`, so importing this module (as
# `src/revl/test.py` does, wasmtime-less) is unaffected.
# ---------------------------------------------------------------------------

import json as _json
import re as _re

#: matches the `revl:teardown` custom section `_ComponentEmitter.
#: _teardown_section` (backends/wasm/emit.py) embeds — a WAT string literal,
#: so an escaped inner `"` (`\"`) or `\\` must not end the match early.
_TEARDOWN_SECTION_RE = _re.compile(r'\(@custom "revl:teardown" "((?:[^"\\]|\\.)*)"\)')


def _wat_string_unescape(payload: str) -> str:
    """The inverse of `emit.py`'s `_wat_string` (`\\` -> `\\\\` -> `\\"` ->
    `\\n`, applied in that order at emit time, so undone in reverse here)."""
    return payload.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def parse_teardown_descriptor(wat: str) -> dict | None:
    """This component's static WAL index (item 243 Slice 2b), or `None` when
    it registers no `transactional`/`compensation` entry (the common case).

    The shape (`_ComponentEmitter._teardown_section`, backends/wasm/emit.py):
    ``{"record": "discharge-descriptor", "entries": [{"seq": int, "entry":
    "transactional"|"compensation", "dispatch": int}, ...], "phase1Count":
    int, "phase2Count": int}``. `dispatch` is the entry's `$__dstep` position
    (0-based) — what a driver walking `deactivate_step` calls actually sees;
    `seq` is its registration order on the shared stack (the contract's own
    numbering). `phase1Count`/`phase2Count` locate the dispatch split
    (bracket+transactional LIFO, then compensation LIFO) — the piece a host
    driver needs and the entry list alone does not carry, since a bracket
    needs no WAL row (G5 infallible) but still occupies a Phase-1 dispatch
    slot.
    """
    match = _TEARDOWN_SECTION_RE.search(wat)
    if match is None:
        return None
    return _json.loads(_wat_string_unescape(match.group(1)))


def _residue_record(kind: str, seq: int, *, error: dict | None,
                    attempted: bool, outcome: str) -> dict:
    """One record in the merged residue schema (docs/design/teardown-
    contract.md "The merged residue schema"), the wasm tier's honest subset:
    `crossing`/`referent`/`hint` need durable argument capture this tier does
    not carry (see `_teardown_section`'s docstring) and are left `None` rather
    than faked; `kind`/`attempted`/`error`/`outcome` are exactly what a live
    `deactivate_step` call can observe."""
    return {
        "kind": kind,
        "seq": seq,
        "attempted": {"call": None, "phase": 2} if attempted else None,
        "attemptedFlag": attempted,
        "error": error,
        "outcome": outcome,
        "crossing": None,
        "referent": None,
        "hint": None,
    }


def drive_teardown(engine, store, exports: dict, wat: str, *,
                   phase2_budget_ms: int | None = None,
                   phase2_per_call_ms: int | None = None) -> dict:
    """Drive one already-instantiated component's two-phase teardown to
    completion, one entry per `deactivate_step` call, bounding Phase 2
    (compensations) with a wasmtime epoch deadline where the entry's own
    inverse/compensate expression reaches guest code (item 243 Slice 2b's
    first-party wiring, docs/design/teardown-contract.md "wasm" row).

    Per-tier bound honesty (the contract's own qualification, restated here
    because this is where it becomes an actual guarantee or the lack of one):
    an epoch/fuel deadline only fires when wasmtime CHECKS it, and it checks
    at a function-call entry or a loop back-edge — GUEST code. A Phase-2
    compensation that calls another wasm function (`call $name`, the only
    shape a `compensate` expression on this tier renders as) is bounded; a
    HOST IMPORT the compensation calls into (a `req`/coeffect call) is not —
    wasmtime cannot preempt code it does not control once control has left
    the guest. This is not a partial implementation of the bound; it is the
    substrate's real ceiling, and the contract says to state that honestly
    rather than claim more.

    `engine` must have been built with `Config().epoch_interruption = True`
    when `phase2_per_call_ms` is given (a `wasmtime.WasmtimeError` surfaces
    otherwise — this driver does not silently no-op the bound). `store` and
    `exports` are the caller's: instantiation/import wiring is a runtime
    composition concern this driver does not own.

    Returns ``{"clean": bool, "outstanding": [Record, ...]}`` — the schema's
    envelope, minus the fields this tier cannot durably carry (see
    `_residue_record`).
    """
    import threading
    import time as _time

    import wasmtime

    descriptor = parse_teardown_descriptor(wat) or {
        "entries": [], "phase1Count": 0, "phase2Count": 0,
    }
    by_dispatch = {e["dispatch"]: e["entry"] for e in descriptor.get("entries") or []}
    seq_by_dispatch = {e["dispatch"]: e["seq"] for e in descriptor.get("entries") or []}
    phase1_count = descriptor.get("phase1Count", 0)
    # `phase1Count`/`phase2Count` describe the ABORT dispatch chain only
    # (`_module`'s `abort_chain`); a COMMITTED activation dispatches the
    # shorter `commit_chain` (bracket entries only), whose length this
    # descriptor does not carry — reading `committed()` up front, and driving
    # `deactivate_step` off its OWN "more" return rather than a precomputed
    # count, makes this correct for both without needing that extra count.
    is_committed = bool(exports["committed"](store))

    deactivate_step = exports["deactivate_step"]
    outstanding: list[dict] = []
    deadline = (_time.monotonic() + phase2_budget_ms / 1000.0
               if phase2_budget_ms is not None else None)
    # the ABORT dispatch chain's exact length (`phase1Count + phase2Count`,
    # `_module`'s `abort_chain`) — needed only to record every REMAINING
    # skipped entry once the budget expires, without calling the guest for
    # them (there is nothing to advance `$__dstep` past for an entry never
    # dispatched). The commit chain's length is not in the descriptor, but a
    # committed activation never reaches the budget branch below (brackets
    # carry no Phase-2 bound), so `more`, not this count, drives that case.
    abort_total = phase1_count + descriptor.get("phase2Count", 0)

    k = 0
    more = 1
    while more:
        in_phase2 = (not is_committed) and k >= phase1_count
        if in_phase2 and deadline is not None and _time.monotonic() >= deadline:
            # between-compensation check (the contract's NORMATIVE minimum,
            # every tier): the budget is already spent, so no further
            # compensation starts. Every skip is recorded, none silently
            # dropped — but this driver cannot advance `$__dstep` past a slot
            # it never called, so it records the rest in one pass and stops;
            # a caller that still wants the module's own teardown state fully
            # advanced (e.g. before reusing the instance) can fall back to
            # the legacy `deactivate` export, which has no such budget.
            for skipped in range(k, abort_total):
                outstanding.append(_residue_record(
                    "compensation-residue", seq_by_dispatch.get(skipped, skipped + 1),
                    error={"type": "deadline-expired", "message": "phase-2 budget exhausted"},
                    attempted=False, outcome="not-attempted"))
            break

        timer = None
        if in_phase2 and phase2_per_call_ms is not None:
            store.set_epoch_deadline(1)
            timer = threading.Timer(phase2_per_call_ms / 1000.0, engine.increment_epoch)
            timer.start()
        try:
            more = deactivate_step(store)
        except wasmtime.Trap as trap:
            code = getattr(trap, "trap_code", None)
            # `seq_by_dispatch` (and `by_dispatch`) are keyed by DISPATCH
            # position (K, this loop's own `k`), not registration seq — a
            # bracket entry carries no WAL row (see `_teardown_section`) and
            # so has no seq to report; its dispatch position is the fallback,
            # documented as such rather than silently substituted.
            reported_seq = seq_by_dispatch.get(k, k + 1)
            if code in (wasmtime.TrapCode.INTERRUPT, wasmtime.TrapCode.OUT_OF_FUEL):
                outstanding.append(_residue_record(
                    "compensation-residue", reported_seq,
                    error={"type": str(code), "message": str(trap)},
                    attempted=True, outcome="unknown"))
            else:
                kind = ("bracket-fault" if by_dispatch.get(k) is None
                       else "restore-residue" if not in_phase2
                       else "compensation-residue")
                outstanding.append(_residue_record(
                    kind, reported_seq, error={"type": "trap", "message": str(trap)},
                    attempted=True, outcome="failed"))
            # core wasm has no catch/continue across the trapping `call`
            # inside `deactivate_step` itself, but each entry is its OWN
            # call from THIS driver, and emit.py advances `$__dstep` BEFORE
            # running the entry (`_dispatch`), so the driver — unlike a
            # single legacy `deactivate()` call — simply continues to the
            # NEXT entry on its next loop iteration. This is the per-entry
            # continue-and-record the contract specifies, achieved at the
            # granularity core wasm actually allows.
            more = 1
        finally:
            if timer is not None:
                timer.cancel()
        k += 1

    return {"clean": not outstanding, "outstanding": outstanding}

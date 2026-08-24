"""Lifecycle-test executor for the wasm tier (roadmap item 142).

`revl test --backend wasm` on a document carrying a `lifecycle test`
(syntax-2.0 §7.1) drives that test's script over a *live* cordis-wasm
composition — the substrate sibling of the py reference tier's in-process
lifecycle runner and the go tier's `func TestXxx` over a live stc-go context
(``backends/go/emit.py`` ``_emit_stc_lifecycle_tests``). A lifecycle test is
not a pure test unit: it loads components into a running runtime, calls through
provision keys, unloads them LIFO, and asserts the runtime holds nothing
afterwards (R4 registry residue + R1 live resources).

That runtime lives in its own wasmtime-bearing repo/venv, so — exactly as the
once-mode driver splits :mod:`revl.run_wasm` (revl side: emit the WAT) from
``run_harness.py`` (cordis-wasm side: boot it) — the lifecycle driver in
``src/revl/test.py`` does the revl-side work (compile + emit the modules,
decide which tests the substrate can express) and hands this harness a spec of
pre-emitted modules plus each runnable test's reduced, scalar-only step script.
The harness needs only wasmtime + the cordis-wasm ``runtime`` module, never the
revl toolchain.

Each test runs on a *fresh* ``Runtime`` (independent of every other), mirroring
the go tier's per-test ``stc.New()``:

* ``load``   — ``rt.plug(component, wat)``; a consumer stays INACTIVE until its
  coeffect resolves, so the author's provider-first order brings the mesh up.
* ``call``   — resolve the provision key in the coeffect table Σ
  (``rt.table[key]``) and invoke ``.ops[op](*args)`` — the exact key-resolved
  read the once-mode boot and the reference tier use (R2: a call must find the
  key ACTIVE). Scalar (Int/Bool) args/returns only; the driver skips any test
  whose calls cross a non-scalar boundary (no host marshalling here).
* ``assert`` — a scalar boolean expression over prior ``call`` bindings.
* ``unload`` — ``rt.unplug(fiber)``; the compiled ``deactivate`` state machine
  replays the component's inverses (LIFO across a mesh via the author's order).
* ``assert no_residue`` — the live runtime must hold nothing: no fiber left in
  ``rt.fibers`` (R4, the registry mirror of ``registry().len() == 0``) and no
  key left in the coeffect table ``rt.table`` (Σ empty). This is the substrate
  mirror of the py driver's ``registry.size == 0`` / ``reflect.store == {}``.

Usage (driven by ``src/revl/test.py``, not by hand):

    CORDIS_WASM=<dir> <cordis-wasm-venv-python> lifecycle_harness.py <spec.json>

Output is line-oriented so the revl-side driver can parse it without importing
the runtime: ``PASS <name>`` / ``FAIL <name>: <reason>`` per test, then a final
``SUMMARY <passed> <total>`` line.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys


class _StepError(Exception):
    """A step could not be carried out — reported as a test failure."""


def _eval(expr, env):
    """Evaluate a scalar (Int/Bool) expression AST over the binding env.

    Only the scalar subset the driver admits reaches here (lit/var/bin/unary);
    an Opt/Str/record comparison is never emitted into a runnable test's spec —
    the driver skips such a test with a reason instead. An unknown shape is a
    hard error, not a silent pass.
    """
    kind = expr.get("kind")
    if kind == "lit":
        return expr.get("value")
    if kind == "var":
        name = expr.get("name")
        if name not in env:
            raise _StepError(f"unbound variable {name!r} in assertion")
        return env[name]
    if kind == "unary":
        operand = _eval(expr["operand"], env)
        op = expr.get("op")
        if op in ("!", "not"):
            return not operand
        if op == "-":
            return -operand
        raise _StepError(f"unsupported unary operator {op!r}")
    if kind == "bin":
        left = _eval(expr["left"], env)
        right = _eval(expr["right"], env)
        op = expr.get("op")
        ops = {
            "==": lambda: left == right,
            "!=": lambda: left != right,
            "<": lambda: left < right,
            ">": lambda: left > right,
            "<=": lambda: left <= right,
            ">=": lambda: left >= right,
            "+": lambda: left + right,
            "-": lambda: left - right,
            "*": lambda: left * right,
            "&&": lambda: bool(left) and bool(right),
            "and": lambda: bool(left) and bool(right),
            "||": lambda: bool(left) or bool(right),
            "or": lambda: bool(left) or bool(right),
        }
        if op not in ops:
            raise _StepError(f"unsupported operator {op!r} in assertion")
        return ops[op]()
    raise _StepError(f"unsupported expression {kind!r} in a wasm lifecycle test")


def _run_test(Runtime, test, modules):
    """Drive one lifecycle test on a fresh runtime; raise _StepError on failure."""
    rt = Runtime()
    fibers = {}
    env = {}
    for step in test.get("steps") or []:
        kind = step.get("step")
        if kind == "load":
            component = step["component"]
            if component not in modules:
                raise _StepError(f"load {component}: no emitted module for it")
            fibers[component] = rt.plug(component, modules[component])
        elif kind == "unload":
            component = step["component"]
            fiber = fibers.pop(component, None)
            if fiber is not None:
                rt.unplug(fiber)
        elif kind == "call":
            key, op = step["key"], step["op"]
            provider = rt.table.get(key)
            if provider is None:
                raise _StepError(
                    f"call {key}.{op}: key {key!r} is not ACTIVE (R2) — "
                    "its provider never resolved on the live runtime")
            handler = provider.ops.get(op)
            if handler is None:
                raise _StepError(f"call {key}.{op}: provider exposes no op {op!r}")
            args = [_eval(a, env) for a in step.get("args") or []]
            result = handler(*args)
            bind = step.get("bind")
            if bind is not None:
                env[bind] = result
        elif kind == "assert":
            if not _eval(step["expr"], env):
                raise _StepError("assertion failed")
        elif kind == "assert_no_residue":
            live_fibers = len(rt.fibers)
            live_services = len(rt.table)
            if live_fibers != 0 or live_services != 0:
                raise _StepError(
                    f"residue — {live_fibers} live plugin(s), "
                    f"{live_services} service(s) still provided (R4/R1)")
        else:  # pragma: no cover — the driver only serialises the shapes above
            raise _StepError(f"unknown lifecycle step {kind!r}")


def main() -> int:
    spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    modules = spec.get("modules") or {}
    tests = spec.get("tests") or []

    cordis_wasm = os.environ.get("CORDIS_WASM") or str(
        pathlib.Path.home() / "Projects" / "cordis-wasm")
    sys.path.insert(0, cordis_wasm)
    from runtime import Runtime  # noqa: PLC0415 — cordis-wasm, wasmtime-backed

    passed = 0
    for test in tests:
        name = test.get("name") or "lifecycle"
        try:
            _run_test(Runtime, test, modules)
        except _StepError as error:
            print(f"FAIL {name}: {error}", flush=True)
        except Exception as error:  # noqa: BLE001 — any runtime fault is a failure
            print(f"FAIL {name}: {type(error).__name__}: {error}", flush=True)
        else:
            passed += 1
            print(f"PASS {name}", flush=True)

    print(f"SUMMARY {passed} {len(tests)}", flush=True)
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())

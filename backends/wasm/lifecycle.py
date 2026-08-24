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

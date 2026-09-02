"""Roadmap items 109/141 (harness finding #38), py tier: an emission of an
async service op nested in an EXPRESSION position of an `async` provide method
must be awaited inline.

The defect: the py await-seed fired only on statement/return positions, so
`async fn route(p) { return p == "go" ? emit m.complete(p) : "idle" }` emitted
an `async def` that RETURNED the ternary without awaiting the nested `emit`.
The caller then got a coroutine object back out of its own `await`
(`coroutine ... was never awaited`), and the assertion failed. `042c4987`
extended the seed; nothing on the py side pinned it, which is what this file
does — for the ternary the item quotes and for the other expression positions
an emission can land in (template interpolation, a binary operand, a list
element, a record field).

The SYNC twin of the same shape — roadmap items 111/117, harness finding #40,
`fn route(p) { return p == "go" ? emit m.complete(p) : "idle" }` — is not an
emitter concern at all: per A1 a sync method has no in-flight window, so the
checker refuses it. That refusal is pinned by
`examples/rejections/a1_async_op_sync_ternary.rvl` in the frontend suite;
`test_sync_method_reaching_async_op_in_expression_is_refused` below re-checks
it from this tier, because it is the reason no py/ts emitter fix is owed for it.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import types

import pytest

from cordis import Context

import emit

# revl (the frontend) lives beside this backend; setup.sh installs this venv
# editable against the checkout, so compiling from SOURCE here runs the real
# compile -> emit -> run pipeline rather than a hand-built IR.
_SRC = pathlib.Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl import compile_source  # noqa: E402


# One async emission (`m.complete`) reached from an async provide method through
# every expression position an emission can occupy. `chain` is the shape the
# harness's web_shell `dispatch` has: a ternary CHAIN whose arms are emissions.
_SOURCE = """
service Marker {
  fn seen(p: Str) -> Opt[Str]
  emission async fn complete(p: Str) -> Str
}

service Router {
  emission async fn chain(p: Str) -> Str
  emission async fn interp(p: Str) -> Str
  emission async fn concat(p: Str) -> Str
  emission async fn listy(p: Str) -> Str
  emission async fn reccy(p: Str) -> Str
}

component Marks provides m: Marker {
  let store = effect Map.new() undo store.drop()

  provide m {
    fn seen(p) = store.get(p)
    async fn complete(p) {
      effect store.insert(p, "done")
      undo   store.remove(p)
      return "done"
    }
  }
}

component Route requires m: Marker provides r: Router {
  provide r {
    async fn chain(p) {
      return p == "a" ? emit m.complete(p) : (p == "b" ? emit m.complete(p) : "idle")
    }
    async fn interp(p)  { return `<${emit m.complete(p)}>` }
    async fn concat(p)  { return (emit m.complete(p)) + "!" }
    async fn listy(p)   { let xs = [emit m.complete(p), "x"]  return xs.join(",") }
    async fn reccy(p)   { let rec = { tag: emit m.complete(p) }  return rec.tag }
  }
}
"""

_EXPECTED = {
    "chain": ("b", "done"),
    "interp": ("i", "<done>"),
    "concat": ("c", "done!"),
    "listy": ("d", "done,x"),
    "reccy": ("e", "done"),
}


def _emitted() -> str:
    return emit.emit(compile_source(_SOURCE))


async def _settle() -> None:
    """Let cordis finish wiring the fibers (the emitted driver's `_revl_settle`)."""
    for _ in range(20):
        await asyncio.sleep(0)


def _module(source: str, name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    exec(compile(source, f"{name}.py", "exec"), module.__dict__)
    return module


def test_every_nested_emission_is_awaited_inline():
    """Textual pin: no call of the async emission is left un-awaited. Before the
    fix the ternary arms rendered as a bare `_revl_ctx.m.complete(p)`."""
    source = _emitted()
    calls = source.count("_revl_ctx.m.complete(")
    awaited = source.count("await _revl_ctx.m.complete(")
    assert calls == 6, f"expected six emission call sites, found {calls}"
    assert awaited == calls, (
        "an async emission in expression position was emitted without `await`:\n"
        + "\n".join(line for line in source.splitlines() if "m.complete(" in line)
    )


@pytest.mark.asyncio
async def test_nested_emissions_return_values_not_coroutines():
    """Runtime pin: awaiting the provide method yields the emission's VALUE. With
    the un-awaited seed each of these returned a coroutine object out of the
    caller's own await, which is how the harness saw `coroutine ... was never
    awaited` and a failed assertion."""
    module = _module(_emitted(), "async_expr_await_141")
    root = Context()
    root.plugin(module.Marks)
    route = root.plugin(module.Route)
    await _settle()

    impl = root.get("r")
    assert impl is not None, "provision `r` never became ACTIVE"

    for method, (argument, expected) in _EXPECTED.items():
        result = await getattr(impl, method)(argument)
        assert isinstance(result, str), (
            f"{method} returned {type(result).__name__}, not a value — "
            "the nested emission was not awaited"
        )
        assert result == expected, f"{method} returned {result!r}, expected {expected!r}"

    await route.dispose()
    await _settle()


def test_sync_method_reaching_async_op_in_expression_is_refused():
    """Items 111/117: the same ternary in a SYNC provide method has no in-flight
    window to await in, so it never reaches an emitter — the checker refuses it.
    This is why finding #40's `{}` needs no py/ts emitter change."""
    sync_source = _SOURCE.replace(
        "  emission async fn chain(p: Str) -> Str", "  emission fn chain(p: Str) -> Str"
    ).replace("    async fn chain(p) {", "    fn chain(p) {")

    with pytest.raises(Exception) as excinfo:
        compile_source(sync_source)
    message = str(excinfo.value)
    assert "declared sync" in message and "async operation `m.complete`" in message, message
    assert "(A1)" in message, message

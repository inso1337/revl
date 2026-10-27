"""Declared emission ceilings are enforced when the program runs (issue #2688).

The compiler already REFUSES a countable body whose proved cardinality exceeds
a declared `calls` ceiling, attenuates budgets at every spawn edge, and carries
the ceilings into the audit. The `*/host-extern case` is the one a static count
cannot prove, and `size`/`time` have no static analogue at all, so a declared
`bytes` or `time` was accepted, attenuated, reported, and then not enforced.

This slice enforces the three kinds at the Python tier's one host-extern
crossing seam (`runtime.extern_emit`), per activation, BEFORE the WAL record and
the host body:

  * `calls` - a crossing that would be number limit + 1 is refused;
  * `size`  - the canonical payload bytes accumulate per capability, and a
              crossing whose payload would exceed the ceiling never fires;
  * `time`  - a deadline anchored at the capability's first crossing, measured
              on the monotonic clock and checked before every crossing, and
              again when the crossing returns (a synchronous host call cannot
              be preempted mid-call on this tier).

Each over-budget case is refused with the named `CeilingExceeded`, and a
within-budget control completes. A `calls`/`size` refusal, and a `time` refusal
of a crossing whose deadline was already spent, happen BEFORE the fire and
before the emission step exists, so the host body does not run and the WAL has
no step for that crossing. A `time` overrun the host body itself caused is only
observable when the call returns, so the crossing DID fire: its journal outcome
is `emitted-then-refused` and the compensation it registered at the seam, when
the body returned and before the refusal was decided, stays owed. A host body
that RAISES registers nothing, because the crossing was never confirmed (item
247's rule). An `await emit` and an `emit ... with a` edge use the same seam
ordering. A budget-free extern still emits byte-identically and traces nothing.

The budget is per ACTIVATION (one component activation, not one provide call):
`Frame` owns the counters, so a second activation starts fresh. That is the
design's stated unit (`docs/design/260-emission-cardinality-bounds.md` §3: a
per-activation max), and it is intended - a ceiling bounds what one activation
may spend, and a re-activation is a new spend.

A wrapper must not launder the declaration. `emit go(x)` where
`fn go(x) = call(x)` is still a `kind: "fn"` crossing, but the emit names `go`,
not the extern, so a direct declaration lookup found nothing and the crossing
fired unmetered. The emitter resolves the callee through the document's local
functions (`_function_ceiling_caps`) and sends the reached extern's ceiling
declarations; the runtime refuses a budgeted callee that arrives with empty caps
(`CeilingCapsMissing`) as a second, fail-closed line of defence.

Only the exact pass-through wrapper is attributable: the meter charges one
crossing with the outer arguments. A value-position wrapper never reaches the
seam at all, a payload-transforming wrapper would be charged on the wrong
arguments, and a multi-call wrapper would be charged once however many crossings
it makes. Those shapes are refused at emit time with the named
`CeilingUnattributable`, naming the capability and the shape, so a declared
budget is enforced or refused rather than silently promised. A function value
(`let f = call`, a wrapper alias, `apply(call)`) is refused earlier still, at
admission, by the `emission-value` rule from #2979; the emitter's function-value
refusal remains the backstop for a shape that reaches it.
"""

from __future__ import annotations

import asyncio
import importlib
import itertools
import json
import pathlib
import sys
import tempfile
import types

import pytest

from cordis import Context

import emit
import replay
import runtime as runtime_mod

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl.compiler import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402

#: A trusted host module (reached through `@py ref`, the sanctioned way for a
#: fixture to have a side effect). It counts the crossings that actually reach
#: the host body, records the compensations that run, and optionally sleeps so a
#: `time` deadline can be crossed.
_HOST_SOURCE = """\
import time

CALLS = []
UNDOS = []


def call(x):
    CALLS.append(x)
    if {sleep!r}:
        time.sleep({sleep!r})
    if {raises!r}:
        raise RuntimeError("host body failed before it acted")
    return 0


def revert(x):
    UNDOS.append(x)
    return 0
"""

#: The async twin: the host body is the awaited coroutine, so a `time` overrun
#: is only observable after the await settles. `hook`/`drop` are the bracket a
#: Phase-1-before-Phase-2 ordering test acquires before the refused crossing.
_ASYNC_HOST_SOURCE = """\
import asyncio

CALLS = []
UNDOS = []


async def call(x):
    CALLS.append(x)
    await asyncio.sleep({sleep!r})
    return 0


def hook(x):
    return 0


def drop(x):
    UNDOS.append("drop")
    return 0


def revert(x):
    UNDOS.append(x)
    return 0
"""


_counter = itertools.count()


def _program(decl: str, body: str, sleep: float = 0.0, prelude: str = "",
             raises: bool = False):
    """Write a program plus its trusted host module into a fresh directory and
    return `(path, host_module_name)`. The host module name is unique per
    program, so its embedded sleep value cannot collide across the tests in
    this file. `prelude` is module-level text between the extern and the
    component - the wrapper function the bypass tests need. `raises` makes the
    host body raise before it returns."""
    host = f"ceiling_host_{next(_counter)}"
    directory = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_"))
    (directory / f"{host}.py").write_text(
        _HOST_SOURCE.format(sleep=sleep, raises=raises), encoding="utf-8")
    source = (
        f'extern emission[{decl}] fn call(x: Str) -> Int = @py ref call from "{host}.py"\n'
        f"{prelude + chr(10) if prelude else ''}"
        f"component C {{\n{body}\n}}\n")
    main = directory / "main.rvl"
    main.write_text(source, encoding="utf-8")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return str(main), host


def _load(decl: str, body: str, sleep: float = 0.0, prelude: str = "",
          raises: bool = False):
    path, host = _program(decl, body, sleep, prelude, raises)
    code = emit.emit(compile_files([path]))
    module = types.ModuleType(f"ceiling_module_{next(_counter)}")
    exec(compile(code, "ceiling_module.py", "exec"), module.__dict__)  # noqa: S102
    return module, importlib.import_module(host), code


async def _activate(module):
    """Plug the program's one component and drive it to activation, returning
    the raised exception (None on a clean activation)."""
    root = Context()
    root.logger.error = lambda *args, **kwargs: None
    fiber = root.plugin(module.C)
    try:
        await fiber
    except BaseException as error:  # noqa: BLE001 - the test asserts the type
        return error
    return None


def _run(decl: str, body: str, sleep: float = 0.0, prelude: str = ""):
    module, host, _code = _load(decl, body, sleep, prelude)
    error = asyncio.run(_activate(module))
    return error, host.CALLS


def _compensated_program(decl: str, sleep: float = 0.0, calls=("a",),
                         raises: bool = False):
    """A program whose component emits the budgeted extern once per entry in
    `calls`, each with a site-spelled compensation, plus its trusted host
    module. Returns `(path, host_module_name)`."""
    host = f"ceiling_host_{next(_counter)}"
    directory = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_"))
    (directory / f"{host}.py").write_text(
        _HOST_SOURCE.format(sleep=sleep, raises=raises), encoding="utf-8")
    steps = "\n".join(f'  emit call("{name}") compensate revert("{name}")'
                      for name in calls)
    source = (
        f'extern emission[{decl}] fn call(x: Str) -> Int '
        f'= @py ref call from "{host}.py"\n'
        f'extern emission fn revert(x: Str) -> Int '
        f'= @py ref revert from "{host}.py"\n'
        f"component C {{\n{steps}\n}}\n")
    main = directory / "main.rvl"
    main.write_text(source, encoding="utf-8")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return str(main), host


def _load_compensated(decl: str, sleep: float = 0.0, calls=("a",),
                      raises: bool = False):
    path, host = _compensated_program(decl, sleep, calls, raises)
    code = emit.emit(compile_files([path]))
    module = types.ModuleType(f"ceiling_module_{next(_counter)}")
    exec(compile(code, "ceiling_module.py", "exec"), module.__dict__)  # noqa: S102
    return module, importlib.import_module(host), code


def _async_compensated_program(decl: str, sleep: float = 0.0):
    """The reviewer probe's shape: an ASYNC host extern under a `time` ceiling,
    emitted with `await` and a site-spelled compensation. Returns
    `(path, host_module_name)`."""
    host = f"ceiling_async_host_{next(_counter)}"
    directory = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_async_"))
    (directory / f"{host}.py").write_text(
        _ASYNC_HOST_SOURCE.format(sleep=sleep), encoding="utf-8")
    source = (
        f'extern emission[{decl}] async fn call(x: Str) -> Int '
        f'= @py ref call from "{host}.py"\n'
        f'extern emission fn revert(x: Str) -> Int '
        f'= @py ref revert from "{host}.py"\n'
        "component C {\n"
        '  await emit call("a") compensate revert("a")\n'
        "}\n")
    main = directory / "main.rvl"
    main.write_text(source, encoding="utf-8")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return str(main), host


def _load_async_compensated(decl: str, sleep: float = 0.0):
    path, host = _async_compensated_program(decl, sleep)
    code = emit.emit(compile_files([path]))
    module = types.ModuleType(f"ceiling_module_{next(_counter)}")
    exec(compile(code, "ceiling_module.py", "exec"), module.__dict__)  # noqa: S102
    return module, importlib.import_module(host), code


def _async_bracketed_program(decl: str, sleep: float = 0.0):
    """The async probe plus a Phase-1 bracket acquired before the refused
    crossing, so the test can pin that the compensation runs in Phase 2, after
    the bracket inverse, not at the seam. Returns `(path, host_module_name)`."""
    host = f"ceiling_async_host_{next(_counter)}"
    directory = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_async_"))
    (directory / f"{host}.py").write_text(
        _ASYNC_HOST_SOURCE.format(sleep=sleep), encoding="utf-8")
    source = (
        f'extern emission[{decl}] async fn call(x: Str) -> Int '
        f'= @py ref call from "{host}.py"\n'
        f'extern emission fn revert(x: Str) -> Int '
        f'= @py ref revert from "{host}.py"\n'
        f'extern acquire fn hook(x: Str) -> Handle undo drop(result) '
        f'= @py ref hook from "{host}.py"\n'
        f'extern pure fn drop(x: Handle) -> Int '
        f'= @py ref drop from "{host}.py"\n'
        "component C {\n"
        '  let h = effect hook("h") undo drop(h)\n'
        '  await emit call("a") compensate revert("a")\n'
        "}\n")
    main = directory / "main.rvl"
    main.write_text(source, encoding="utf-8")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return str(main), host


def _load_async_bracketed(decl: str, sleep: float = 0.0):
    path, host = _async_bracketed_program(decl, sleep)
    code = emit.emit(compile_files([path]))
    module = types.ModuleType(f"ceiling_module_{next(_counter)}")
    exec(compile(code, "ceiling_module.py", "exec"), module.__dict__)  # noqa: S102
    return module, importlib.import_module(host), code


# ---------------------------------------------------------------------------
# calls - the host-extern case a static count cannot prove
# ---------------------------------------------------------------------------

def test_calls_over_budget_refuses_the_fourth_crossing_before_the_host_body():
    """`calls=3` admits three host-path crossings and refuses the fourth with
    the named fault, before that crossing's host body runs. The issue's
    acceptance case."""
    error, calls = _run("net(calls=3)", "\n".join(
        f'  emit call("{name}")' for name in "abcd"))
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert (error.kind, error.limit, error.observed) == ("calls", 3, 4)
    assert error.capability == "net(calls=3)"
    assert "declared calls ceiling 3 on `net(calls=3)` exceeded" in str(error)
    assert calls == ["a", "b", "c"], "the refused crossing must not fire"


def test_calls_within_budget_runs_every_crossing():
    """The control: three crossings against `calls=3` is exactly in budget."""
    error, calls = _run("net(calls=3)", "\n".join(
        f'  emit call("{name}")' for name in "abc"))
    assert error is None
    assert calls == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# size - the canonical payload bytes, accumulated per activation
# ---------------------------------------------------------------------------

def test_bytes_over_budget_refuses_before_the_payload_reaches_the_host():
    """`bytes="4B"` refuses a 5-byte payload (`["a"]` canonicalized) before the
    host body runs, naming the observed bytes."""
    error, calls = _run('net(bytes="4B")', '  emit call("a")')
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert (error.kind, error.limit, error.observed) == ("size", 4, 5)
    assert error.capability == 'net(size=4)'
    assert "declared size ceiling 4" in str(error)
    assert calls == [], "the over-budget payload must never be used"


def test_bytes_within_budget_runs_the_crossing():
    """The control: the same payload under a 1KB ceiling fires."""
    error, calls = _run('net(bytes="1KB")', '  emit call("a")')
    assert error is None
    assert calls == ["a"]


# ---------------------------------------------------------------------------
# time - a deadline on the capability, checked before every crossing
# ---------------------------------------------------------------------------

def test_a_single_crossing_that_overruns_the_deadline_faults_on_return():
    """A host body that spends 20ms under `time="10ms"` faults when it returns:
    a synchronous host call cannot be preempted, so the overrun is caught at
    the crossing's return rather than never. The second crossing never runs."""
    error, calls = _run('net(time="10ms")',
                        '  emit call("a")\n  emit call("b")', sleep=0.02)
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert error.kind == "time"
    assert error.limit == 10
    assert error.observed >= 20
    assert error.capability == "net(time=10)"
    assert calls == ["a"], "the crossing after the overrun must not fire"


def test_time_within_budget_runs_every_crossing():
    """The control: the same 20ms host body under a 5s deadline is in budget."""
    error, calls = _run('net(time="5s")',
                        '  emit call("a")\n  emit call("b")', sleep=0.02)
    assert error is None
    assert calls == ["a", "b"]


# ---------------------------------------------------------------------------
# the declaration surface and the accounting record
# ---------------------------------------------------------------------------

def test_the_crossing_trace_names_the_ceiling_and_the_observed_count():
    """The run trace records each metered crossing with the declared ceiling and
    the observed count, so a reviewer can tell 'never reached' from 'enforced'.
    The audit JSON already carries the ceiling spelling itself."""
    module, host, _code = _load("net(calls=3)", '  emit call("a")\n  emit call("b")')
    events: list[str] = []
    runtime_mod.set_trace(events.append)
    try:
        error = asyncio.run(_activate(module))
    finally:
        runtime_mod.set_trace(None)
    assert error is None
    assert events == [
        "ceiling net(calls=3) calls=1/3",
        "ceiling net(calls=3) calls=2/3",
    ]
    assert host.CALLS == ["a", "b"]


def test_a_budget_free_extern_emits_without_a_ceiling_argument():
    """No declaration, no change: the emitted crossing carries no `caps=`, the
    crossing still runs, and no trace line is added. This is the additivity
    guard for every existing host extern."""
    module, host, code = _load("net", '  emit call("a")')
    assert "caps=" not in code
    assert asyncio.run(_activate(module)) is None
    assert host.CALLS == ["a"]


def test_the_declaration_parser_reads_only_top_level_ceiling_params():
    """A value that merely LOOKS like a ceiling (inside a quoted path with a
    comma) is not one; only the canonical spelling's top-level parameters are
    declarations the meter reads."""
    assert runtime_mod._declared_ceilings(()) == ()
    assert runtime_mod._declared_ceilings(('fs.write(path="/a,calls=9")',)) == ()
    assert runtime_mod._declared_ceilings(
        ('fs.write(path="/a,calls=9",size=64)',)) \
        == (('fs.write(path="/a,calls=9",size=64)', {"size": 64}),)


def test_size_counts_the_activation_payload_total_not_one_payload():
    """Two 5-byte payloads under `bytes="8B"`: the first fits, the second is
    refused before the host body because the ACTIVATION total would be 10."""
    error, calls = _run('net(bytes="8B")',
                        '  emit call("a")\n  emit call("b")')
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert (error.kind, error.limit, error.observed) == ("size", 8, 10)
    assert calls == ["a"]


def test_the_time_deadline_is_anchored_at_the_first_crossing():
    """Unit-level anchor proof with a controlled monotonic clock: 0ms and 5ms
    are in budget under `time=10`, 11ms from the FIRST crossing is refused
    before the host body. This is the before-the-crossing half the real-clock
    test cannot pin."""
    root = Context()
    runtime_mod._bind_frame(root, runtime_mod.Frame(root, "C"))
    calls = []

    def host(x):
        calls.append(x)

    clock = [100.0]
    original = runtime_mod._ceiling_now
    runtime_mod._ceiling_now = lambda: clock[0]
    try:
        runtime_mod.extern_emit(root, "call", host, ("a",),
                                caps=("net(time=10)",))
        clock[0] = 100.005
        runtime_mod.extern_emit(root, "call", host, ("b",),
                                caps=("net(time=10)",))
        clock[0] = 100.011
        with pytest.raises(runtime_mod.CeilingExceeded) as exc:
            runtime_mod.extern_emit(root, "call", host, ("c",),
                                    caps=("net(time=10)",))
    finally:
        runtime_mod._ceiling_now = original
    assert (exc.value.kind, exc.value.limit) == ("time", 10)
    assert calls == ["a", "b"], "the crossing after the deadline must not fire"


def test_a_budgeted_crossing_without_an_activation_frame_is_refused():
    """The meter is per activation. A budgeted crossing outside one cannot be
    attributed, so it is refused rather than treated as unbounded."""
    with pytest.raises(runtime_mod.CeilingUnmetered):
        runtime_mod.extern_emit(Context(), "call", lambda x: None, ("a",),
                                caps=("net(calls=3)",))


def test_a_refused_crossing_is_not_recorded_in_the_wal():
    """The meter runs BEFORE the crossing's WAL record: a refused crossing
    leaves no `KIND_EMISSION` step for a host body that never ran."""
    ctx = types.SimpleNamespace()
    runtime_mod._bind_frame(ctx, runtime_mod.Frame(ctx, "C"))
    recorded = []
    ctx._revl_record_extern = lambda name, args: recorded.append(name)
    with pytest.raises(runtime_mod.CeilingExceeded):
        runtime_mod.extern_emit(ctx, "call", lambda x: None, ("a",),
                                caps=("net(calls=0)",))
    assert recorded == []


def test_the_meters_are_per_activation():
    """Two activations of the same component each get their own counters: a
    `calls=1` ceiling spent by the first activation does not refuse the second."""
    root = Context()
    runtime_mod._bind_frame(root, runtime_mod.Frame(root, "C"))
    calls = []

    def host(x):
        calls.append(x)

    runtime_mod.extern_emit(root, "call", host, ("a",), caps=("net(calls=1)",))
    with pytest.raises(runtime_mod.CeilingExceeded):
        runtime_mod.extern_emit(root, "call", host, ("b",),
                                caps=("net(calls=1)",))
    runtime_mod._bind_frame(root, runtime_mod.Frame(root, "C"))
    runtime_mod.extern_emit(root, "call", host, ("c",), caps=("net(calls=1)",))
    assert calls == ["a", "c"]


# ---------------------------------------------------------------------------
# the wrapper must not launder the declaration (security review, #2688)
# ---------------------------------------------------------------------------

#: `emit go(..)` names a LOCAL function, not an extern. The emitter used to look
#: `go` up in `extern_by_name`, find nothing, pass no `caps=`, and let both host
#: calls fire. These tests pin the resolved-declaration path.
_WRAPPER = "fn go(x: Str) -> Int { return call(x) }"


def test_a_wrapper_cannot_launder_a_calls_ceiling():
    """`go(x) = call(x)` emitted twice under `calls=1`: the second host call is
    refused with `CeilingExceeded` and a refusal record, and never fires."""
    module, host, code = _load(
        "net(calls=1)", '  emit go("a")\n  emit go("b")', prelude=_WRAPPER)
    # the wiring, asserted at the source: the wrapper emission carries the
    # declaration of the extern it reaches, not an empty caps=
    assert "caps=('net(calls=1)',)" in code
    assert module.go._revl_declared_caps == ("net(calls=1)",)
    events: list[str] = []
    runtime_mod.set_trace(events.append)
    try:
        error = asyncio.run(_activate(module))
    finally:
        runtime_mod.set_trace(None)
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert (error.kind, error.limit, error.observed) == ("calls", 1, 2)
    assert error.capability == "net(calls=1)"
    assert host.CALLS == ["a"], "the refused wrapper crossing must not fire"
    assert "ceiling net(calls=1) calls=2/1 refused" in events


def test_a_wrapper_cannot_launder_a_size_ceiling():
    """The same wrapper under `bytes="4B"`: a 5-byte payload is refused before
    the host body, and the refusal names the observed bytes."""
    error, calls = _run(
        'net(bytes="4B")', '  emit go("a")', prelude=_WRAPPER)
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert (error.kind, error.limit, error.observed) == ("size", 4, 5)
    assert calls == [], "the over-budget payload must never reach the host"


def test_a_wrapper_cannot_launder_a_time_ceiling():
    """A wrapper around a 20ms host body under `time="10ms"`: the overrun is
    caught when the wrapper returns, before the next crossing starts."""
    error, calls = _run(
        'net(time="10ms")', '  emit go("a")\n  emit go("b")',
        sleep=0.02, prelude=_WRAPPER)
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert error.kind == "time"
    assert error.limit == 10
    assert error.observed >= 20
    assert calls == ["a"], "the crossing after the overrun must not fire"


def test_an_empty_caps_crossing_with_declared_ceilings_fails_closed():
    """The runtime backstop: the emitted callee carries its declared ceilings,
    so reaching `extern_emit` with EMPTY caps is refused by name instead of
    firing unmetered. Both the extern and the wrapper are tagged."""
    module, host, _code = _load(
        "net(calls=1)", '  emit call("a")', prelude=_WRAPPER)
    assert module.call._revl_declared_caps == ("net(calls=1)",)
    root = Context()
    runtime_mod._bind_frame(root, runtime_mod.Frame(root, "C"))
    with pytest.raises(runtime_mod.CeilingCapsMissing) as caught:
        runtime_mod.extern_emit(root, "call", module.call, ("a",))
    assert "net(calls=1)" in str(caught.value)
    with pytest.raises(runtime_mod.CeilingCapsMissing):
        runtime_mod.extern_emit(root, "go", module.go, ("a",))
    assert host.CALLS == [], "a fail-closed refusal never reaches the host body"


# ---------------------------------------------------------------------------
# the shapes the meter cannot attribute are refused at emit time (#2688)
# ---------------------------------------------------------------------------

#: The identity wrapper above is the ONE shape the meter can attribute exactly.
#: These two are not: the first hands the extern a payload the wrapper did not
#: receive, the second crosses the extern more than once per invocation.
_TRANSFORMING = 'fn go() -> Int { return call("0123456789") }'
_MULTI_CALL = (
    "fn go(x: Str) -> Int { let a = call(x) let b = call(x) return a }")


def _provide_program(decl: str, method_body: str, prelude: str = ""):
    """Like `_program`, but the component PROVIDES one operation, so a
    value-position emission has a method body to sit in."""
    host = f"ceiling_host_{next(_counter)}"
    directory = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_"))
    (directory / f"{host}.py").write_text(
        _HOST_SOURCE.format(sleep=0.0, raises=False), encoding="utf-8")
    source = (
        f'extern emission[{decl}] fn call(x: Str) -> Int = @py ref call from "{host}.py"\n'
        f"{prelude + chr(10) if prelude else ''}"
        "service Ops { emission fn m() }\n"
        "component C provides ops: Ops {\n"
        "  provide ops {\n"
        f"    fn m() {{ {method_body} }}\n"
        "  }\n"
        "}\n")
    main = directory / "main.rvl"
    main.write_text(source, encoding="utf-8")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return str(main), host


def _refusal(decl: str, body: str, prelude: str = "", provide: bool = False):
    """Compile, then emit: the emission must be refused by name, so no Python
    module is produced and the host body never runs."""
    path, host = (_provide_program(decl, body, prelude) if provide
                  else _program(decl, body, prelude=prelude))
    compiled = compile_files([path])
    try:
        emit.emit(compiled)
    except emit.CeilingUnattributable as error:
        return error, importlib.import_module(host)
    pytest.fail("a budgeted wrapper shape was emitted instead of refused")


def test_a_value_position_wrapper_is_refused_at_emit_time():
    """`let a = emit go("a")` in a provide-method body names a local wrapper.
    That path never reaches `extern_emit`, so neither the caps argument nor the
    empty-caps backstop can meter or refuse it; the emitter refuses by name."""
    error, host = _refusal(
        "net(calls=1)", 'let a = emit go("a") return a',
        prelude=_WRAPPER, provide=True)
    assert isinstance(error, emit.CeilingUnattributable)
    assert error.capability == "net(calls=1)"
    assert error.shape == "a value-position emission"
    assert "net(calls=1)" in str(error)
    assert "value-position" in str(error)
    assert host.CALLS == [], "an emit-time refusal never reaches the host"


def test_a_payload_transforming_wrapper_is_refused_at_emit_time():
    """A wrapper that supplies its own payload (`call("0123456789")`) would be
    charged on the outer arguments, not the bytes the extern receives, so a
    `bytes=4` declaration would be silently exceeded. Refused by name."""
    error, host = _refusal(
        'net(bytes="4B")', '  emit go()', prelude=_TRANSFORMING)
    assert isinstance(error, emit.CeilingUnattributable)
    assert "size=4" in error.capability
    assert error.shape == "a payload-transforming call"
    assert "payload-transforming" in str(error)
    assert host.CALLS == [], "an emit-time refusal never reaches the host"


def test_a_multi_call_wrapper_is_refused_at_emit_time():
    """A wrapper that crosses the same budgeted extern twice per invocation is
    charged once at the wrapper seam, so `calls=1` would be silently exceeded.
    Refused by name instead of emitting the under-metered crossing."""
    error, host = _refusal(
        "net(calls=1)", '  emit go("a")', prelude=_MULTI_CALL)
    assert isinstance(error, emit.CeilingUnattributable)
    assert error.capability == "net(calls=1)"
    assert error.shape == "more than one call"
    assert "more than one call" in str(error)
    assert host.CALLS == [], "an emit-time refusal never reaches the host"


def test_a_recursive_wrapper_is_refused_at_emit_time():
    """A recursive wrapper crosses the extern once per level, so the meter
    cannot bound it. The classifier stops at the cycle and refuses by name
    instead of recursing until the interpreter gives up."""
    error, host = _refusal(
        "net(calls=1)", '  emit go("a")',
        prelude='fn go(x: Str) -> Int { let a = call(x) return go(x) }')
    assert error.shape == "a recursive call"
    assert host.CALLS == [], "an emit-time refusal never reaches the host"


def test_a_loop_crossing_is_refused_at_emit_time():
    """A crossing in a loop condition runs once per iteration while the meter
    charges the wrapper once, so it is refused by name."""
    error, host = _refusal(
        "net(calls=1)", '  emit go("a")',
        prelude='fn go(x: Str) -> Int { while (call(x) > 0) { return 1 } return 0 }')
    assert error.shape == "a call inside a loop"
    assert host.CALLS == [], "an emit-time refusal never reaches the host"


def test_a_conditional_expression_crossing_is_refused_at_emit_time():
    """A crossing in one branch of a conditional expression may not run at all,
    so the meter would over-charge. Refused by name."""
    error, host = _refusal(
        "net(calls=1)", '  emit go("a")',
        prelude='fn go(x: Str) -> Int '
                '{ return if (x == "") { call(x) } else { 0 } }')
    assert error.shape == "a conditional call"
    assert host.CALLS == [], "an emit-time refusal never reaches the host"


#: The three function-value shapes: the extern is not the callee of any named
#: call. The checker now refuses the value binding at admission (G4
#: `emission-value`, fixed by #2979), so the emitter never sees these shapes and
#: the host body cannot run. The emitter's own function-value refusal remains the
#: fail-closed backstop for any shape that does reach it.
_EXTERN_ALIAS = 'fn go(x: Str) -> Int { let f = call return f(x) }'
_WRAPPER_ALIAS = (
    "fn h(x: Str) -> Int = call(x)\n"
    "fn go(x: Str) -> Int { let g = h return g(x) }")
_EXTERN_VALUE = (
    "fn apply(f: (Str) -> Int) -> Int { return f(\"a\") }\n"
    "fn go(x: Str) -> Int = apply(call)")


def _admission_refusal(decl: str, body: str, prelude: str):
    """Compile only: the checker refuses the value binding, so no Python module
    is produced and the host body never runs."""
    path, host = _program(decl, body, prelude=prelude)
    with pytest.raises(RevlError) as caught:
        compile_files([path])
    return caught.value, importlib.import_module(host)


def _assert_function_value_admission_refusal(error, host) -> None:
    assert "cannot be used as a function value" in str(error)
    assert "(G4)" in str(error)
    assert host.CALLS == [], "an admission refusal never reaches the host"


def test_an_extern_alias_is_refused_before_emission():
    """`let f = call return f(x)` binds the budgeted extern to a local and
    calls through the binding. No named call names the extern, so the meter
    would never see it; the checker refuses the value binding instead (#2979)."""
    error, host = _admission_refusal(
        "net(calls=1)", '  emit go("a")', prelude=_EXTERN_ALIAS)
    _assert_function_value_admission_refusal(error, host)


def test_a_wrapper_alias_is_refused_before_emission():
    """`let g = h return g(x)` reaches the extern through a local function held
    as a value. `h` is an exact pass-through when called directly, but a value
    alias hides the callee from the meter, so the checker refuses it (#2979)."""
    error, host = _admission_refusal(
        "net(calls=1)", '  emit go("a")', prelude=_WRAPPER_ALIAS)
    _assert_function_value_admission_refusal(error, host)


def test_an_extern_passed_as_an_argument_is_refused_before_emission():
    """`apply(call)` hands the budgeted extern to another function, which calls
    it through the parameter. The extern itself never reaches the seam, so the
    checker refuses the value binding (#2979)."""
    error, host = _admission_refusal(
        "net(calls=1)", '  emit go("a")', prelude=_EXTERN_VALUE)
    _assert_function_value_admission_refusal(error, host)


def test_the_statement_form_identity_wrapper_is_still_metered():
    """The control for the three refusals: the exact pass-through wrapper is
    still emitted with the reached declaration and metered by the runtime."""
    module, host, code = _load(
        "net(calls=1)", '  emit go("a")', prelude=_WRAPPER)
    assert "caps=('net(calls=1)',)" in code
    error = asyncio.run(_activate(module))
    assert error is None
    assert host.CALLS == ["a"]


def _recorded_emit(module, host, crosses, caps):
    """Fire a sequence of budgeted crossings through the recording seam on ONE
    activation, returning `(errors, records)`: the exceptions raised, and the
    records the WAL wrote."""
    wal_path = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_wal_")) / "wal.jsonl"
    timeline = replay.Timeline("C")
    wal = replay.WriteAheadLog(str(wal_path), ir={}).open()
    timeline.attach_wal(wal)
    ctx = replay._RecordingContext(Context(), timeline, {}, {})
    runtime_mod._bind_frame(ctx, runtime_mod.Frame(ctx, "C"))
    errors = []
    try:
        for args in crosses:
            try:
                runtime_mod.extern_emit(ctx, "call", module.call, tuple(args),
                                        caps=caps)
            except BaseException as raised:  # noqa: BLE001 - returned
                errors.append(raised)
    finally:
        wal.close()
    records = [json.loads(line)
               for line in wal_path.read_text(encoding="utf-8").splitlines()]
    return errors, records


def _outcome_records(records):
    return [record for record in records
            if str(record.get("record", "")).startswith("emission-")
            or record.get("record") == "emitted-then-refused"]


def test_a_time_overrun_journals_emitted_then_refused_with_observed_counts():
    """A `time` overrun is only observable when the host call returns, so the
    crossing DID happen. Its outcome says so: `emitted-then-refused`, never
    `emission-complete` (the deadline was spent) and never `emission-refused`
    (which would claim the crossing never fired). The JSON record carries the
    observed counts beside the ceiling spelling."""
    module, host, _code = _load(
        'net(time="10ms")', '  emit call("a")', sleep=0.02)
    errors, records = _recorded_emit(
        module, host, [("a",)], ("net(time=10)",))
    assert len(errors) == 1
    assert isinstance(errors[0], runtime_mod.CeilingExceeded)
    outcomes = _outcome_records(records)
    assert [record["record"] for record in outcomes] == ["emitted-then-refused"]
    refusal = outcomes[0]
    observed = refusal["ceiling"]["net(time=10)"]
    assert observed["calls"] == 1
    assert observed["bytes"] >= 1
    assert observed["elapsed"] >= 20
    assert refusal["error"]["type"] == "CeilingExceeded"
    assert refusal["error"]["kind"] == "time"
    assert host.CALLS == ["a"], "the host body ran before the overrun was seen"


def test_a_pre_fire_refusal_leaves_no_emission_step():
    """The contrast that makes the outcome readable: a crossing refused BEFORE
    its host body runs is refused before the emission step is recorded, so the
    WAL carries the completed crossing and NO step for the refused one. Only a
    crossing that actually fired gets an `emitted-then-refused` outcome; the
    absence of a record is 'never fired'."""
    module, host, _code = _load(
        "net(calls=1)", '  emit call("a")\n  emit call("b")')
    errors, records = _recorded_emit(
        module, host, [("a",), ("b",)], ("net(calls=1)",))
    assert len(errors) == 1
    assert isinstance(errors[0], runtime_mod.CeilingExceeded)
    assert errors[0].kind == "calls"
    assert [record["record"] for record in _outcome_records(records)] == \
        ["emission-complete"]
    steps = [record for record in records
             if record.get("record") == "effect"
             and record.get("kind") == "emission"]
    assert len(steps) == 1, "the refused crossing has no emission step"
    assert host.CALLS == ["a"], "the refused crossing never fired"


def test_a_time_overrun_after_the_host_body_keeps_its_compensation():
    """A crossing that fires and is then refused on its return must stay
    compensable: the compensation is registered at the seam when the host body
    returns, before the `time` check decides the outcome, and the refusal
    enqueues it for Phase 2. Registering it after the fire lost it exactly when
    it was owed."""
    module, host, code = _load_compensated('net(time="10ms")', sleep=0.02)
    assert ".compensation" in code
    error = asyncio.run(_activate(module))
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert error.kind == "time"
    assert host.CALLS == ["a"], "the host body ran before the overrun was seen"
    assert host.UNDOS == ["a"], \
        "a fired crossing that is then refused must stay compensable"


def test_a_crossing_refused_before_it_fires_is_not_compensated():
    """The other half: the crossing that never fired has no registered
    compensation, so the abort compensates only the crossing that did fire."""
    module, host, _code = _load_compensated("net(calls=1)", calls=("a", "b"))
    error = asyncio.run(_activate(module))
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert error.kind == "calls"
    assert host.CALLS == ["a"], "the refused crossing never fired"
    assert host.UNDOS == ["a"], "only the crossing that fired is compensated"


def test_a_sync_host_body_that_raises_registers_no_compensation():
    """A host body that RAISES never confirmed that the crossing happened, so
    nothing is owed. The compensation registers when the body returns; a raise
    skips that registration, item 247's rule (`declared_crossing`), because
    compensating a crossing that never acted is as wrong as dropping one that
    did. Before this, the seam registered before the body and compensated even
    a body that raised before any effect."""
    module, host, _code = _load_compensated("net(calls=1)", raises=True)
    error = asyncio.run(_activate(module))
    assert isinstance(error, RuntimeError)
    assert host.CALLS == ["a"], "the host body did start"
    assert host.UNDOS == [], \
        "a raising host body must not register a compensation"


def _install_emitted_body(module, ctx):
    """Run the module's `apply`, intercepting `Frame.install` only to capture
    the activation frame and the cordis effect it registered. The effect drives
    the emitted body, so awaiting it also runs cordis's dispose pass on the
    body's failure, which is the path the async test must exercise."""
    captured = {}
    original = runtime_mod.Frame.install

    def capture(self, body):
        effect = original(self, body)
        captured["frame"] = self
        captured["effect"] = effect
        return effect

    runtime_mod.Frame.install = capture
    try:
        module.C["apply"](ctx, {})
    finally:
        runtime_mod.Frame.install = original
    return captured["frame"], captured["effect"]


async def _drive_until_time_refusal(effect):
    """Await the installed effect: the async body's time refusal fails the
    setup task, cordis then disposes the yielded entries (Phase 1), and `begin`
    (yielded first, disposed last) drains Phase 2."""
    with pytest.raises(runtime_mod.CeilingExceeded) as caught:
        await effect
    await asyncio.sleep(0)
    return caught.value


def _recording_context():
    wal_path = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_wal_")) / "wal.jsonl"
    timeline = replay.Timeline("C")
    wal = replay.WriteAheadLog(str(wal_path), ir={}).open()
    timeline.attach_wal(wal)
    ctx = replay._RecordingContext(Context(), timeline, {}, {})
    return ctx, wal, wal_path


def _wal_records(wal_path):
    return [json.loads(line)
            for line in wal_path.read_text(encoding="utf-8").splitlines()]


def test_an_async_time_overrun_runs_its_compensation_exactly_once():
    """The reviewer probe's shape: an async host extern under `time="10ms"`,
    `await emit call("a") compensate revert("a")`. The await IS the host body
    and it returns, so the crossing fired; the return check refuses it. The
    compensation must reach the seam (not the after-fire yield) and run exactly
    once, and the WAL outcome must stay `emitted-then-refused`."""
    module, host, code = _load_async_compensated('net(time="10ms")', sleep=0.02)
    fires = [line for line in code.splitlines() if "_revl_extern_emit(" in line]
    assert len(fires) == 1
    assert "compensate=" in fires[0], \
        "the async fire must hand its compensation to the seam"
    assert ".compensation" in code
    ctx, wal, wal_path = _recording_context()
    try:
        frame, effect = _install_emitted_body(module, ctx)
        error = asyncio.run(_drive_until_time_refusal(effect))
    finally:
        wal.close()
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert error.kind == "time"
    assert host.CALLS == ["a"], "the host body ran before the overrun was seen"
    assert host.UNDOS == ["a"], \
        "a fired async refusal must run its compensation exactly once"
    assert [entry.ran for entry in frame._compensations] == [True]
    assert frame._pending_compensations == []
    frame._drain_phase2()
    assert host.UNDOS == ["a"], "Phase 2 is single-flight"
    outcomes = _outcome_records(_wal_records(wal_path))
    assert [record["record"] for record in outcomes] == ["emitted-then-refused"]


def test_an_async_refusal_compensates_after_phase_one():
    """The compensation is Phase 2. An async body's unwind first disposes the
    yielded bracket inverse (Phase 1), then `begin` (yielded first, disposed
    last) drains the enqueued compensation. Draining at the seam instead would
    run the offset before the bracket inverse, breaking the two-phase contract
    (teardown-contract.md)."""
    module, host, _code = _load_async_bracketed('net(time="10ms")', sleep=0.02)
    ctx, wal, _wal_path = _recording_context()
    try:
        _frame, effect = _install_emitted_body(module, ctx)
        error = asyncio.run(_drive_until_time_refusal(effect))
    finally:
        wal.close()
    assert isinstance(error, runtime_mod.CeilingExceeded)
    assert error.kind == "time"
    assert host.CALLS == ["a"]
    assert host.UNDOS == ["drop", "a"], \
        "the bracket inverse is Phase 1; the compensation is Phase 2"


def _load_approval_emit():
    """A budgeted emit written with an approval edge in the activation body:
    `emit call("a") compensate revert("a") with a`. The extern does not declare
    `requires approval`, so the edge is admitted by the policy surface, which
    is enough to reach the emitter shape under test."""
    host = f"ceiling_host_{next(_counter)}"
    directory = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_approval_"))
    (directory / f"{host}.py").write_text(
        _HOST_SOURCE.format(sleep=0.0, raises=False), encoding="utf-8")
    source = (
        f'extern emission[net(calls=1)] fn call(x: Str) -> Int '
        f'= @py ref call from "{host}.py"\n'
        f'extern emission fn revert(x: Str) -> Int '
        f'= @py ref revert from "{host}.py"\n'
        "component C {\n"
        '  let a = await approval[net] { reason: "go" }\n'
        '  emit call("a") compensate revert("a") with a\n'
        "}\n")
    main = directory / "main.rvl"
    main.write_text(source, encoding="utf-8")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return emit.emit(compile_files([str(main)])), host


def test_an_approval_edge_registers_the_compensation_at_the_same_seam():
    """An `emit ... with a` edge is the same shape: the token is checked and
    consumed before the fire, and the compensation rides the same seam fire, so
    it registers when the host body returns. It must not stay on the
    after-the-fire yield, which a `time` refusal skips."""
    code, _host = _load_approval_emit()
    lines = code.splitlines()
    approval = [i for i, line in enumerate(lines) if "approval_crossing(" in line]
    assert len(approval) == 1
    assert "compensate=" in lines[approval[0]], \
        "the approval-wrapped fire must hand its compensation to the seam"
    assert "caps=('net(calls=1)',)" in lines[approval[0]]
    assert not any(line.strip().startswith("yield _revl_frame.compensation(")
                   for line in lines), \
        "the compensation must not stay on the after-fire yield"
    assert any("yield" in line and ".compensation" in line
               for line in lines[approval[0] + 1:]), \
        "the body must yield the entry the seam returned"


def test_the_budget_is_per_activation_and_that_is_intended():
    """`calls=1` is a per-ACTIVATION budget: one component activation may cross
    once, a fresh activation may cross once again. This is the design's stated
    unit (docs/design/260 §3) and is intentional; a `size`/`time` deadline is
    anchored the same way, at the token's first crossing in that activation."""
    root = Context()
    runtime_mod._bind_frame(root, runtime_mod.Frame(root, "C"))
    calls = []

    def host(x):
        calls.append(x)

    runtime_mod.extern_emit(root, "call", host, ("a",), caps=("net(calls=1)",))
    with pytest.raises(runtime_mod.CeilingExceeded):
        runtime_mod.extern_emit(root, "call", host, ("b",),
                                caps=("net(calls=1)",))
    runtime_mod._bind_frame(root, runtime_mod.Frame(root, "C"))
    runtime_mod.extern_emit(root, "call", host, ("c",), caps=("net(calls=1)",))
    assert calls == ["a", "c"]


def test_an_mcp_session_call_surfaces_the_typed_refusal(tmp_path):
    """End to end over the MCP session: a provided operation that emits the
    wrapper twice under `calls=1` raises the runtime's own `CeilingExceeded`
    out of `Session.call`, and the host saw only the in-budget crossing."""
    from revl.mcp.session import Session  # noqa: PLC0415

    host_name = f"ceiling_mcp_host_{next(_counter)}"
    (tmp_path / f"{host_name}.py").write_text(
        _HOST_SOURCE.format(sleep=0.0, raises=False), encoding="utf-8")
    (tmp_path / "main.rvl").write_text(
        f'extern emission[net(calls=1)] fn call(x: Str) -> Int '
        f'= @py ref call from "{host_name}.py"\n'
        f"{_WRAPPER}\n"
        "service Ops { emission fn twice() }\n"
        "component C provides ops: Ops {\n"
        "  provide ops {\n"
        '    fn twice() { emit go("a") emit go("b") }\n'
        "  }\n"
        "}\n", encoding="utf-8")
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    session = Session()
    session.load(compile_files([str(tmp_path / "main.rvl")]))
    with pytest.raises(runtime_mod.CeilingExceeded) as caught:
        session.call("ops", "twice", [])
    assert (caught.value.kind, caught.value.limit) == ("calls", 1)
    assert "declared calls ceiling 1" in str(caught.value)
    host = importlib.import_module(host_name)
    assert host.CALLS == ["a"], "the refused MCP crossing must not fire"

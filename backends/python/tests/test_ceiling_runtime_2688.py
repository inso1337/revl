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
              on the monotonic clock and checked before every crossing (a
              synchronous host call cannot be preempted mid-call on this tier).

Each over-budget case is refused with the named `CeilingExceeded`, the host body
does not run for the refused crossing, and a within-budget control completes.
A budget-free extern still emits byte-identically and traces nothing.

The budget is per ACTIVATION (one component activation, not one provide call):
`Frame` owns the counters, so a second activation starts fresh. That is the
design's stated unit (`docs/design/260-emission-cardinality-bounds.md` §3: a
per-activation max), and it is intended - a ceiling bounds what one activation
may spend, and a re-activation is a new spend.

A wrapper must not launder the declaration. `emit go(x)` where
`fn go(x) = call(x)` is still a `kind: "fn"` crossing, but the emit names `go`,
not the extern, so a direct declaration lookup found nothing and the crossing
fired unmetered. The emitter now resolves the callee through the document's
local functions (`_function_ceiling_caps`) and sends the reached extern's
ceiling declarations; the runtime refuses a budgeted callee that arrives with
empty caps (`CeilingCapsMissing`) as a second, fail-closed line of defence.
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

#: A trusted host module (reached through `@py ref`, the sanctioned way for a
#: fixture to have a side effect). It counts the crossings that actually reach
#: the host body, and optionally sleeps so a `time` deadline can be crossed.
_HOST_SOURCE = """\
import time

CALLS = []


def call(x):
    CALLS.append(x)
    if {sleep!r}:
        time.sleep({sleep!r})
    return 0
"""

_counter = itertools.count()


def _program(decl: str, body: str, sleep: float = 0.0, prelude: str = ""):
    """Write a program plus its trusted host module into a fresh directory and
    return `(path, host_module_name)`. The host module name is unique per
    program, so its embedded sleep value cannot collide across the tests in
    this file. `prelude` is module-level text between the extern and the
    component - the wrapper function the bypass tests need."""
    host = f"ceiling_host_{next(_counter)}"
    directory = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_"))
    (directory / f"{host}.py").write_text(
        _HOST_SOURCE.format(sleep=sleep), encoding="utf-8")
    source = (
        f'extern emission[{decl}] fn call(x: Str) -> Int = @py ref call from "{host}.py"\n'
        f"{prelude + chr(10) if prelude else ''}"
        f"component C {{\n{body}\n}}\n")
    main = directory / "main.rvl"
    main.write_text(source, encoding="utf-8")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return str(main), host


def _load(decl: str, body: str, sleep: float = 0.0, prelude: str = ""):
    path, host = _program(decl, body, sleep, prelude)
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


def test_a_time_overrun_journals_refusal_not_completion_with_observed_counts():
    """The WAL ordering fix: the `time` check runs BEFORE the completion record,
    so an overrun writes `emission-refused` (never `emission-complete`), and the
    JSON record carries the observed counts beside the ceiling spelling."""
    module, host, _code = _load(
        'net(time="10ms")', '  emit call("a")', sleep=0.02)
    wal_path = pathlib.Path(tempfile.mkdtemp(prefix="revl2688_wal_")) / "wal.jsonl"
    timeline = replay.Timeline("C")
    wal = replay.WriteAheadLog(str(wal_path), ir={}).open()
    timeline.attach_wal(wal)
    ctx = replay._RecordingContext(Context(), timeline, {}, {})
    runtime_mod._bind_frame(ctx, runtime_mod.Frame(ctx, "C"))
    try:
        with pytest.raises(runtime_mod.CeilingExceeded):
            runtime_mod.extern_emit(ctx, "call", module.call, ("a",),
                                    caps=("net(time=10)",))
    finally:
        wal.close()
    records = [json.loads(line)
               for line in wal_path.read_text(encoding="utf-8").splitlines()]
    outcomes = [record for record in records
                if str(record.get("record", "")).startswith("emission-")]
    assert [record["record"] for record in outcomes] == ["emission-refused"]
    refusal = outcomes[0]
    observed = refusal["ceiling"]["net(time=10)"]
    assert observed["calls"] == 1
    assert observed["bytes"] >= 1
    assert observed["elapsed"] >= 20
    assert refusal["error"]["type"] == "CeilingExceeded"
    assert refusal["error"]["kind"] == "time"
    assert host.CALLS == ["a"]


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
        _HOST_SOURCE.format(sleep=0.0), encoding="utf-8")
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

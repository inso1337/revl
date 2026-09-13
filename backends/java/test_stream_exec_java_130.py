"""item 130 Slices 3-5 — `Stream[T]` on the cordis4j (blocking) tier.

docs/design/130-stream-reactive-types.md §4.6 puts go, java and rust in their own
slice because those tiers ERASE the async color: `next` becomes a blocking park
that the CANCEL signal also ends, and `close` trips that signal. That park is the
whole point — it is what keeps the bracket inverse reachable off the teardown
thread (§9 Part A), so a `next` parked on a provider that never emits cannot make
teardown deadlock behind it.

The compile-time half here asserts the emitted shapes (runs everywhere). The
executable half emits scenarios/stream_130.rvl and scenarios/stream_event_130.rvl,
compiles each against the stub cordis4j API in ./stubs, and RUNS the scenario
harness on the JVM — the only thing that proves the park actually unparks, that a
provider fault reaches an outstanding `next` through a real activation, that
`merge`'s multi-source teardown leaves nothing behind, and that the typed-event
gate validates, collapses and faults where it says it does. It SKIPS honestly when
no JDK is present.

The tier-agnostic shape assertions (and the go/rust/ts mirrors of them) live in
tests/test_stream_reactive.py.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "src"))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from revl import compile_files  # noqa: E402

import javac_gate  # noqa: E402

JAVAC = javac_gate.JAVAC
JAVA = javac_gate.JAVA
NO_JDK = javac_gate.NO_JDK

FIXTURE = HERE / "scenarios" / "stream_130.rvl"
EVENT_FIXTURE = HERE / "scenarios" / "stream_event_130.rvl"
HARNESS = HERE / "scenarios" / "RunStream130.java"
EVENT_HARNESS = HERE / "scenarios" / "RunStreamEvent130.java"


def _emit_module():
    spec = importlib.util.spec_from_file_location(
        "revl_java_emit_130", HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emit = _emit_module()


def _emit_java(fixture: Path) -> str:
    return emit.emit(compile_files([str(fixture)]))


# ---------------------------------------------------------------------------
# the emitted shapes (design §4.6, the java row)
# ---------------------------------------------------------------------------

def test_subscribe_is_an_ordinary_bracket_whose_inverse_is_close():
    """The CORE GUARANTEE's mechanism on this tier: the subscription is an
    ordinary bracket on the activation's own `fx` accumulator, and its inverse is
    `close`. So unloading the owner closes the stream on the same LIFO disposer
    stack every other bracket rides — no stream-specific teardown path."""
    src = _emit_java(FIXTURE)
    assert 'var sub = Stream.subscribe(src, "error", 0);' in src
    assert "fx.track(Disposables.of(() -> sub.close()));" in src
    # LIFO: the provider's bracket is registered BEFORE the consumer's, so the
    # consumer's inverse runs first and no provider is torn down under a live
    # listener.
    assert src.index("() -> src.close()") < src.index("() -> sub.close()")


def test_next_is_the_blocking_cancellation_first_park():
    """`next` erases to a blocking park whose cancel flag teardown trips, and the
    flag is probed BEFORE the buffer — so a `close` racing a buffered item still
    wins (§9 Part A). `close` never waits for the park: signalling the condition
    is what resolves it."""
    src = _emit_java(FIXTURE)
    assert "public Object next() {" in src
    assert "if (cancelled) {" in src
    assert src.index("if (cancelled) {") < src.index("if (!items.isEmpty()) {")
    assert "ready.await();" in src
    assert "ready.signalAll();" in src


def test_a_faulted_terminal_is_an_uncaught_throw():
    """A `Faulted` terminal (a provider abort, or an `error`-policy overflow) is a
    thrown CordisException, not a value — so it cannot be mistaken for an item,
    and nothing in the lowering catches it. The activation's own A8 self-revert
    disposes the accumulated prefix (subscription bracket included) and rethrows,
    which is how a failed handler cannot leave a subscription active."""
    src = _emit_java(FIXTURE)
    assert 'throw new CordisException("stream faulted: " + why);' in src
    body = src[src.index("class ParkedPlugin"):src.index("class FaninPlugin")]
    assert "sub.next();" in body
    assert body.count("catch (") == 1, "only the A8 self-revert catches"
    assert "fx.dispose();" in body


def test_merge_opens_inside_the_subscriptions_acquisition():
    """`subscribe merge(a, b)` opens the fan-in INSIDE the subscription's
    acquisition, so the merged stream is DERIVED and owned by the subscription
    rather than being a bracket of its own. Multi-source teardown then rides the
    ONE bracket the subscribe registers."""
    src = _emit_java(FIXTURE)
    assert 'var sub = Stream.subscribe(Stream.merge(a, b), "error", 0);' in src
    # one bracket per subscribing component (Consumer, Parked, Fanin, Iterate) and
    # NO extra bracket for the derived merge — it is owned by the subscription.
    assert src.count("() -> sub.close()") == 4
    fanin = src[src.index("class FaninPlugin"):src.index("class IteratePlugin")]
    assert fanin.count("fx.track(") == 3, (
        "the fan-in grew a bracket of its own; multi-source teardown must ride "
        "the one bracket the subscribe registers")


def test_stream_free_program_carries_no_stream_runtime():
    """§10.9: the stream runtime is pulled in only by a document that holds a
    stream. A program without one must not grow a `Stream` class it never names."""
    src = emit.emit(compile_files([str(ROOT / "backends" / "rust" / "scenarios"
                                      / "probe.rvl")]))
    assert "class Stream" not in src
    assert "class Subscription" not in src


def test_iteration_lowers_as_a_blocking_next_loop():
    """item 130 Slice 4 on the java tier: `every o in sub { … }` is a plain
    `while (true)` loop over the Slice 1/3 `next` — no new runtime. A `Closed`
    ends the loop before the body; the item enters the body only after that."""
    src = _emit_java(FIXTURE)
    body = src[src.index("class IteratePlugin"):]
    assert "while (true) {" in body
    assert "Object _revlStreamItem1 = sub.next();" in body
    assert "if (Stream.isClosed(_revlStreamItem1)) {" in body
    assert body.index("break;") < body.index("sink.write(o)")
    assert "String o = (String) _revlStreamItem1;" in body


def test_typed_event_handler_lowers_with_the_contract_gate():
    """item 130 Slice 5 on the java tier: `on OrderCreated as e in sub { … }` is
    the Slice 4 loop plus ONE gate. The contract is built ONCE above the loop, the
    gate sits AFTER the terminal test, a duplicate `continue`s, and the validated
    item is constructed into the event's record class for the typed body."""
    src = _emit_java(EVENT_FIXTURE)
    assert 'new EventContract("OrderCreated", ' in src
    assert '"order_id", 64);' in src, "the derived key and default window"
    body = src[src.index("class HandlerPlugin"):]
    assert body.index("new EventContract(") < body.index("while (true) {")
    assert body.index("if (Stream.isClosed(") < body.index("_revlEvent1.admit(")
    assert "if (_revlEventObj1 == null) {" in body
    assert body.index("if (_revlEventObj1 == null) {") < body.index("continue;")
    assert "OrderCreated e = new OrderCreated(" in body
    assert body.index("OrderCreated e = new OrderCreated(") < body.index(
        "sink.write((e).order_id)")


def test_typed_event_handler_pulls_the_contract_runtime_only_when_used():
    """The `EventContract` half of the stream runtime — the derived-schema
    validator and the bounded dedup window, plus the hand-written JSON reader the
    JDK does not ship — is emitted only for a document with an `on … as` handler.
    A plain `every … in` program carries the stream runtime and nothing more."""
    handler = _emit_java(EVENT_FIXTURE)
    assert "class EventContract {" in handler
    assert "class RevlJson {" in handler
    plain = _emit_java(FIXTURE)
    assert "class Stream {" in plain
    assert "EventContract" not in plain
    assert "RevlJson" not in plain


# ---------------------------------------------------------------------------
# the executable half: the emitted units RUN on the JVM
# ---------------------------------------------------------------------------

def _run_scenario(tmp_path: Path, source: str, harness: Path, entry: str,
                  banner: str) -> None:
    out = javac_gate.compile_unit(tmp_path, source)
    compiled = subprocess.run(
        [JAVAC, "--release", "21", "-cp", str(out), "-d", str(out), str(harness)],
        capture_output=True, text=True, timeout=600,
    )
    assert compiled.returncode == 0, compiled.stderr
    run = subprocess.run(
        [JAVA, "-cp", str(out), entry],
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert banner in run.stdout


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason=NO_JDK)
def test_java_stream_scenario_builds_and_runs(tmp_path):
    """Definition of done for the java tier: the emitted components RUN on the JVM
    and prove, by running,

      * §10.2 the core guarantee — unloading the owner closes the stream, LIFO,
        with no residue;
      * §9 Part A — a parked `next` is terminated by the owner's own bracket
        inverse, which returns without waiting for the park to drain;
      * §9 Part B — a provider fault terminates an outstanding `next` through a
        real activation, and the failed activation closes the subscription;
      * `merge` — an item from either source reaches the one consumer, the fan-in
        tears down as one LIFO stack, and one source's close does not strand the
        consumer on the other;
      * backpressure `error` — a full bounded buffer faults, no silent loss;
      * Slice 4 `every … in` — the body runs once per item and a `Closed` ends the
        loop without entering it.
    """
    _run_scenario(tmp_path, _emit_java(FIXTURE), HARNESS,
                  "RunStream130", "STREAM_130_OK")


@pytest.mark.skipif(JAVAC is None or JAVA is None, reason=NO_JDK)
def test_java_typed_event_scenario_builds_and_runs(tmp_path):
    """Definition of done for the typed-event handler on the java tier: the
    emitted `EventContract` gate RUNS and proves, by running,

      * a conforming item is validated against the derived schema and reaches the
        typed body (§6);
      * an in-window redelivery of the same key is COLLAPSED — the handler runs
        once per identity, and the collapse is traced, not silent;
      * a schema violation FAULTS the activation — the same terminal a provider
        abort delivers — so no malformed item reaches the body and the failed
        activation closes the subscription with no residue (§6, A8).
    """
    _run_scenario(tmp_path, _emit_java(EVENT_FIXTURE), EVENT_HARNESS,
                  "RunStreamEvent130", "STREAM_EVENT_130_OK")

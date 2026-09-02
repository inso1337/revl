"""item 130 Slice 3 — `Stream[T]` on the cordis-go (blocking) tier.

docs/design/130-stream-reactive-types.md §4.6 puts go, java and rust in their own
slice because those tiers ERASE the async color: `next` becomes a blocking
`select` on the item channel and the CANCEL channel, and `close` closes the
cancel channel. That select is the whole point — it is what keeps the bracket
inverse reachable off the teardown goroutine (§9 Part A), so a `next` parked on a
provider that never emits cannot make teardown deadlock behind it.

The compile-time half here asserts the emitted shapes (runs everywhere). The
executable half emits testdata/stream_130.rvl into a real stc-go module and runs
`go test` — the only thing that proves the select actually unparks, that a
provider fault reaches an outstanding `next` through a real activation, and that
`merge`'s multi-source teardown leaves nothing behind. It SKIPS honestly when no
go toolchain (or no stc-go module cache) is present.
"""

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIXTURE = HERE / "testdata" / "stream_130.rvl"
HARNESS = HERE / "testdata" / "stream_130_test.go.fixture"
GO_SUM = HERE / "scenarios" / "go.sum"
STC = "github.com/0xdenny218/stc-go v0.6.1-0.20260818143352-b3d6788a428e"

sys.path.insert(0, str(ROOT / "src"))
from revl import compile_source  # noqa: E402


def _emit_module():
    spec = importlib.util.spec_from_file_location("revl_go_emit_130", HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emit = _emit_module()


def _emit_go() -> str:
    return emit.emit(compile_source(FIXTURE.read_text(encoding="utf-8")),
                     package="stream130")


# ---------------------------------------------------------------------------
# the emitted shapes (design §4.6, the go row)
# ---------------------------------------------------------------------------

def test_subscribe_is_an_ordinary_bracket_whose_inverse_is_close():
    """The CORE GUARANTEE's mechanism on this tier: the subscription is an
    `ctx.Effect` bracket like any other, so unloading the owner runs `Close`
    through the same LIFO disposer stack a Pool rides."""
    src = _emit_go()
    assert 'sub = StreamSubscribe(src, "error", 0)' in src
    assert "return func() error { sub.Close(); return nil }" in src
    # the subscription binds a live pointer resource, not a value
    assert "var sub *Subscription" in src


def test_next_is_the_cancel_channel_select():
    """`next` erases to a blocking select whose cancel case teardown closes, and
    the priority is spelled explicitly: Go's `select` picks among ready cases at
    random, so cancellation-first cannot be left to it."""
    src = _emit_go()
    assert "func (sub *Subscription) Next() (any, error) {" in src
    assert "case <-sub.cancel:" in src
    assert "case item := <-sub.items:" in src
    assert "case <-sub.term:" in src
    # `Close` trips the cancel channel synchronously and never waits for the park
    assert "close(sub.cancel)" in src
    assert "func (sub *Subscription) Close() bool {" in src


def test_a_faulted_terminal_fails_the_activation():
    """A `Faulted` terminal (a provider abort, or an `error`-policy overflow) is
    an error at the await site, so the accumulated prefix — the subscription
    bracket included — reverts LIFO. A `Closed` terminal is an ordinary value."""
    src = _emit_go()
    assert "if _, err := sub.Next(); err != nil {" in src


def test_merge_rides_the_subscriptions_single_bracket():
    """`subscribe merge(a, b)` opens the fan-in INSIDE the subscription's
    acquisition, so multi-source teardown stays ONE LIFO stack: the single
    bracket inverse closes the subscription, closing the subscription closes the
    merge it owns, and closing the merge detaches it from both sources — which
    keep their own brackets (design §1)."""
    src = _emit_go()
    assert 'sub = StreamSubscribe(StreamMerge(a, b), "error", 0)' in src
    assert "func StreamMerge(a *Stream, b *Stream) *Stream {" in src
    fanin = src.split("func Fanin() stc.Component {", 1)[1].split("\nfunc ", 1)[0]
    # three brackets — one per source, one for the subscription. The fan-in adds
    # NO fourth: it rides the subscription's.
    assert fanin.count("ctx.Effect(func() stc.Inverse {") == 3
    assert fanin.count("sub.Close()") == 1
    # and the subscription's Close cascades into a derived upstream
    assert 'if sub.src.kind != "source" {' in src


def test_stream_free_program_is_byte_identical():
    """§10.9: the stream host runtime is pulled in only by a document that
    reaches a stream, so every stream-free program on this tier emits exactly as
    before."""
    plain = emit.emit(compile_source(
        "component P {\n"
        "  let pool = effect Pool.open(\"u\", 2) undo pool.close()\n"
        "}\n"))
    assert "Subscription" not in plain
    assert "StreamSubscribe" not in plain
    assert "_revlStreams" not in plain


def test_stream_host_gets_no_no_op_stub():
    """`Stream` has a REAL runtime here, so it must not also collect the generic
    `func StreamSource(_args ...any) any` host stub — that would redeclare the
    constructor and silently drop the semantics."""
    src = _emit_go()
    assert "func StreamSource(_args ...any) any {" not in src
    assert "func StreamSource() *Stream {" in src


# ---------------------------------------------------------------------------
# the executable proof (the only thing that proves the select unparks)
# ---------------------------------------------------------------------------

def _go_or_skip() -> str:
    go = shutil.which("go")
    if go is None:
        pytest.skip("go not on PATH")
    if not GO_SUM.exists():  # pragma: no cover — checked in alongside scenarios
        pytest.skip("no pinned stc-go go.sum to build against")
    return go


def test_go_stream_scenario_builds_and_runs():
    """Definition of done for the go tier: the emitted components RUN on real
    stc-go and prove, by running,

      * §10.2 the core guarantee — unloading the owner closes the stream, LIFO,
        with no residue;
      * §9 Part A — a parked `next` is terminated by the owner's own bracket
        inverse, which returns without waiting for the park to drain;
      * §9 Part B — a provider fault terminates an outstanding `next` through a
        real activation, and the failed activation closes the subscription;
      * `merge` — an item from either source reaches the one consumer, the
        fan-in tears down as one LIFO stack, one source's close does not strand
        the consumer on the other, and a source's fault propagates at once;
      * backpressure `error` — a full bounded buffer faults, no silent loss.
    """
    go = _go_or_skip()
    src = _emit_go()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "gen.go").write_text(src, encoding="utf-8")
        shutil.copy(HARNESS, root / "stream_130_test.go")
        (root / "go.mod").write_text(
            "module stream130\n\ngo 1.25.0\n\nrequire %s\n" % STC, encoding="utf-8")
        shutil.copy(GO_SUM, root / "go.sum")
        run = subprocess.run(
            [go, "test", "-count=1", "-race", "./..."],
            cwd=root, capture_output=True, text=True)
    if run.returncode != 0 and "no required module provides" in (
            run.stdout + run.stderr):  # pragma: no cover — offline module cache
        pytest.skip("stc-go is not in the local module cache")
    assert run.returncode == 0, (run.stdout + "\n" + run.stderr)

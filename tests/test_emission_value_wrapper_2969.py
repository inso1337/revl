"""A marked `emit` of a wrapper `fn` records the host extern it crosses (#2969).

`fn helper(msg) { return ext(msg) }` moves the emission to the fn, so the checker
requires `emit` at the wrapper's call and admits it. The py emitter then had two
gaps in that shape:

  * `let r = emit helper(msg)` (VALUE position) rendered as a bare
    `r = helper(msg)`. The host body ran, the `emit` marker was in the source,
    and the write-ahead log held no emission step for the crossing, so a crash
    around it recovered as if nothing had been sent. `_is_value_emission` read
    only the callee's extern class, and a plain `fn` is not in that table.
  * `emit helper(msg)` (STATEMENT position) did record, but under the wrapper's
    name (`helper`), not under the extern that crossed (`ext`), so the log did
    not say which host body ran.

Both forms now record the step keyed by the extern the checker's emission reach
names (`ext`), in the reference emitter and in `selfhost/emit_py.rvl`. A wrapper
that reaches more than one host extern keeps its own name: one `emit` step
cannot name several boundaries. The direct control (`emit ext(msg)`) is
unchanged, and a call without the marker is still refused (G4).

The recording half needs the cordis-py runtime the WAL is written by; the
emitted-bytes half runs anywhere.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402
from revl.lower import RevlError  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WAL is written by a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")


SOURCE = """
service Ops {
  emission fn value(msg: Str) -> Str
  emission fn stmt(msg: Str) -> Str
  emission fn direct(msg: Str) -> Str
  emission fn deep(msg: Str) -> Str
}

extern emission fn ext(msg: Str) -> Str
  = @py { return "ext:" + msg }

extern pure fn upper(msg: Str) -> Str
  = @py { return msg.upper() }

fn helper(msg: Str) -> Str { return ext(msg) }
fn relay(msg: Str) -> Str { return helper(msg) }

component Agent provides ops: Ops {
  provide ops {
    fn value(msg) {
      let r = emit helper(msg)
      return r
    }
    fn stmt(msg) {
      emit helper(msg)
      return "done"
    }
    fn direct(msg) {
      let r = emit ext(msg)
      return r
    }
    fn deep(msg) {
      let r = emit relay(msg)
      return r
    }
  }
}
"""

UNMARKED = """
service Ops { emission fn value(msg: Str) -> Str }
extern emission fn ext(msg: Str) -> Str
  = @py { return "ext:" + msg }
fn helper(msg: Str) -> Str { return ext(msg) }
component Agent provides ops: Ops {
  provide ops {
    fn value(msg) {
      let r = helper(msg)
      return r
    }
  }
}
"""


ASYNC = """
service Ops {
  emission async fn value(msg: Str) -> Str
  emission async fn stmt(msg: Str) -> Str
  emission async fn direct(msg: Str) -> Str
}

extern emission async fn aext(msg: Str) -> Str
  = @py { return "aext:" + msg }

fn helper(msg: Str) -> Str { return aext(msg) }

component Agent provides ops: Ops {
  provide ops {
    async fn value(msg) {
      let r = emit helper(msg)
      return r
    }
    async fn stmt(msg) {
      emit helper(msg)
      return "done"
    }
    async fn direct(msg) {
      let r = emit aext(msg)
      return r
    }
  }
}
"""


ASYNC_ADMITTED = ASYNC.replace(
    "  emission async fn value(msg: Str) -> Str\n", "").replace(
    """    async fn value(msg) {
      let r = emit helper(msg)
      return r
    }
""", "")


def _emit(tmp_path: Path, source: str) -> str:
    sys.path.insert(0, str(ROOT / "backends" / "python"))
    from revl._paths import python_backend_emitter  # noqa: PLC0415
    (tmp_path / "wrapper.rvl").write_text(source, encoding="utf-8")
    return python_backend_emitter().emit(compile_files(
        [str(tmp_path / "wrapper.rvl")]))


def _method(src: str, name: str) -> str:
    start = src.index(f"def {name}(self")
    end = src.find("\n\n", start)
    return src[start:end]


def _steps(source: str, method: str, arg: str) -> list:
    """`[(kind, label)]` the recorder saw for one call, in order."""
    from revl import compile_source  # noqa: PLC0415
    from revl.mcp.session import Session  # noqa: PLC0415
    out = compile_source(source, "wrapper.rvl")
    session = Session()
    session.load(out, record=True)
    session.call("ops", method, [arg])
    return [(step.kind, step.label)
            for step in session.recorder.timeline("Agent").steps]


@needs_cordis
@pytest.mark.parametrize("method", ["value", "stmt", "direct", "deep"])
def test_marked_emit_records_the_host_crossing(method):
    """The value form (the issue's shape), the statement form, its transitive
    wrapper, and the direct control all record the crossing under `ext`. On
    main the value form recorded nothing and the statement form recorded
    `helper`."""
    steps = _steps(SOURCE, method, "hi")
    emissions = [label for kind, label in steps if kind == "emission"]
    assert emissions == ["ext"]


@needs_cordis
def test_the_wrapper_value_form_still_returns_the_host_result():
    """Recording the step must not change what the method returns: the host
    body still runs once and its value reaches the caller."""
    from revl import compile_source  # noqa: PLC0415
    from revl.mcp.session import Session  # noqa: PLC0415
    session = Session()
    session.load(compile_source(SOURCE, "wrapper.rvl"), record=True)
    assert session.call("ops", "value", ["hi"])["result"] == "ext:hi"
    assert session.call("ops", "deep", ["ho"])["result"] == "ext:ho"


@pytest.mark.parametrize("method, callee", [
    ("value", "helper"),
    ("stmt", "helper"),
    ("deep", "relay"),
])
def test_the_emitted_fire_names_the_crossed_extern(tmp_path, method, callee):
    """The emitted method fires through the recording seam, keyed by `ext`,
    with the wrapper as the callee object. On main the value form was a bare
    call and the statement form was keyed by the wrapper."""
    body = _method(_emit(tmp_path, SOURCE), method)
    assert (f"_revl_extern_emit(_revl_ctx, 'ext', {callee}, (msg,))" in body), body
    assert f"{callee}(msg)" not in body.replace(
        f"_revl_extern_emit(_revl_ctx, 'ext', {callee}, (msg,))", ""), body


def test_the_direct_control_is_unchanged(tmp_path):
    """`emit ext(msg)` keeps its own name as the recorded key and its own
    extern as the callee, byte for byte."""
    body = _method(_emit(tmp_path, SOURCE), "direct")
    assert "_revl_extern_emit(_revl_ctx, 'ext', ext, (msg,))" in body


def test_a_pure_value_call_is_not_routed(tmp_path):
    """A plain pure call in value position is still a plain call: the reach
    empty, no seam, no import churn."""
    src = _emit(tmp_path, SOURCE.replace(
        "      let r = emit helper(msg)\n", "      let r = upper(msg)\n"))
    body = _method(src, "value")
    assert "r = upper(msg)" in body
    assert "_revl_extern_emit" not in body


def test_an_unmarked_wrapper_call_is_still_refused(tmp_path):
    """The marker rule is what keeps the routing sound: the unmarked call is
    still refused, and the refusal names the wrapper."""
    path = tmp_path / "unmarked.rvl"
    path.write_text(UNMARKED, encoding="utf-8")
    with pytest.raises(RevlError, match="call to emission `helper` must be marked `emit`"):
        compile_files([str(path)])


def test_an_async_wrapper_value_emit_is_refused(tmp_path):
    """An async-colored wrapper in VALUE position is refused (issue #2969).

    Its suspension cannot be settled through the recording seam, so `let r =
    emit helper(msg)` would run the host body and record no emission step. The
    refusal is loud rather than an unrecorded crossing, and the direct async
    extern (`emit aext(msg)`) is still admitted and recorded."""
    path = tmp_path / "async_wrapper.rvl"
    path.write_text(ASYNC, encoding="utf-8")
    with pytest.raises(RevlError) as err:
        compile_files([str(path)])
    assert err.value.code == "G4"
    assert err.value.category == "emission-value"
    assert "`emit` of async wrapper `helper` in value position" in err.value.message


AWAITED_STATEMENT = """
service Ops {
  emission async fn stmt(msg: Str) -> Str
}

extern emission async fn aext(msg: Str) -> Str
  = @py { return "aext:" + msg }

extern pure fn plain(msg: Str) -> Str
  = @py { return msg }

fn helper(msg: Str) -> Str { return aext(msg) }

component Agent provides ops: Ops {
  provide ops {
    async fn stmt(msg) {
      await emit helper(msg)
      return "done"
    }
  }
}
"""


def test_an_awaited_wrapper_emit_stays_the_statement_form(tmp_path):
    """`await emit helper(msg)` in an `async fn` method is the AWAITED statement
    form, not a value (issue #2969).

    Its crossing IS awaited, so an async wrapper reaches it through the
    recording seam: the step is a statement emit step keyed by the wrapper, the
    value-position refusal does not fire, and the py method awaits the step.
    Reading it as a bare `await <emit expression>` refused it."""
    path = tmp_path / "awaited_statement.rvl"
    path.write_text(AWAITED_STATEMENT, encoding="utf-8")
    emitted = _emit(tmp_path, AWAITED_STATEMENT)
    method = _method(emitted, "stmt")
    assert "await _revl_extern_emit(_revl_ctx, 'helper', helper, (msg,))" in method


ASYNC_ACQUISITION = """
service Database {
  fn query(q: Str) -> Str
}

extern emission async fn aext(msg: Str) -> Str
  = @py { return "aext:" + msg }

fn helper(msg: Str) -> Str { return aext(msg) }

component Consumer requires db: Database {
  let h = effect await emit helper("ACQ") undo db.query("UNDO")
}
"""


def test_an_async_acquisition_is_not_value_position(tmp_path):
    """An ASYNC ACQUISITION of an async wrapper compiles (item 131, #2969).

    `effect await emit helper(...) undo ...` puts the emitted call in the
    bracket's own acquire position: the landed result is bound to the handle and
    the `undo` is its inverse, so it is not a value the caller reads. The
    value-position refusal is keyed to the emit node not being the bracket root,
    or it would refuse the acquisition form too."""
    path = tmp_path / "async_acquisition.rvl"
    path.write_text(ASYNC_ACQUISITION, encoding="utf-8")
    program = compile_files([str(path)])
    assert program is not None


@needs_cordis
def test_an_async_direct_value_emit_still_records_the_extern():
    """The refusal is narrow: a direct async extern in value position was
    already recorded, and still is."""
    steps = _steps(ASYNC_ADMITTED, "direct", "hi")
    emissions = [label for kind, label in steps if kind == "emission"]
    assert emissions == ["aext"]


@needs_cordis
def test_an_async_wrapper_statement_form_still_records_the_wrapper():
    """PIN: the statement form of an async wrapper is admitted and records the
    wrapper's own name (`helper`), not the extern it reaches. The reach map
    skips async fns in both emitters, so the statement form names the wrapper;
    the WAL still holds an emission step, so no crossing runs unrecorded."""
    steps = _steps(ASYNC_ADMITTED, "stmt", "hi")
    emissions = [label for kind, label in steps if kind == "emission"]
    assert emissions == ["helper"]


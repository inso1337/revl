"""The go backend's two entry points answer the same about admissibility.

Issue #1379. `backends/go/emit.py` is reached two ways: `emit`, and
`emit_placement` for a placement runner's `emitted` package. `emit` ran three
document-level refusals -- a typed hole, a called `deferred` emission, a fault
test -- and `emit_placement`'s v3 combined branch, which does not go through
`emit`, ran none of them.

What that cost, measured before the fix on the documents below:

  * a fault test: `emit` refused it by name; `emit_placement` emitted 11561
    bytes, byte-for-byte the same as the identical document with no fault test
    in it. The test was not lowered and not refused, it was dropped.
  * a called `deferred` emission with a `@go` body: `emit` refused it;
    `emit_placement` emitted 11968 bytes, byte-for-byte the same as the same
    document with the `deferred` modifier removed. Deferral is the whole
    semantics of the construct (docs/design/245-session-commit.md Decision 2),
    and the emitted program fires the emission at the call site instead.
  * a typed hole: refused both ways, but through placement only because the
    expression renderer happened to meet a kind it had no arm for, reported as
    `unsupported expr kind: 'hole'`. The guarantee was incidental and the
    diagnostic was the emitter's internal vocabulary rather than docs/holes.md.

The fix is one shared entry (`_refuse_inadmissible_document`) called from both,
not three calls copied into the second. The last test here is the part that
keeps it fixed: it fails if a document-level refusal is added to the module and
not to the shared list.

`_refuse_stream_document_top_level` is NOT one of these, and the test named for
it says why: it is a fact about a rendering path, not about the tier.
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "revl_go_emit_refusals", ROOT / "backends" / "go" / "emit.py")
go = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(go)


# The document every case below is a variation on: a v3 composition with
# components AND top-level declarations, which is what routes `emit_placement`
# down the combined branch that skipped the refusals. It also has a service, so
# there is something to bridge, and a component, so placement has a boot.
_BASE = """\
type Row = { id: Int, name: Str }
type ToolCall = { name: Str, args: List[Str] }
type Step = Final(Str) | NeedTool(ToolCall)

service Scheduler {
  fn describe(now: Step) -> Str
  fn report() -> Row
%(extra_service)s}

%(extern)s
component Sched provides sched: Scheduler {
  provide sched {
    fn describe(now) = match now {
      Final(msg) => msg,
      NeedTool(tool) => tool.name,
    }
    fn report() = %(report_body)s
%(extra_provide)s  }
}
%(trailer)s"""


def _doc(extra_service="", extern="", report_body='{ id: 1, name: "ada" }',
         extra_provide="", trailer="") -> dict:
    src = _BASE % {"extra_service": extra_service, "extern": extern,
                   "report_body": report_body,
                   "extra_provide": extra_provide, "trailer": trailer}
    return compile_source(src, "placement_refusals.rvl")


def _emits(fn, ir) -> int:
    """Bytes emitted, or the test fails with the refusal it got instead."""
    return len(fn(copy.deepcopy(ir), package="emitted"))


def _refusal(fn, ir) -> str:
    with pytest.raises(go.EmitError) as exc:
        fn(copy.deepcopy(ir), package="emitted")
    return str(exc.value)


# --------------------------------------------------------------------------
# the control: the same shape, admissible, and it still places


def test_the_control_document_places():
    ir = _doc()
    assert ir.get("ir_version") == 3
    assert _emits(go.emit, ir) > 0
    assert _emits(go.emit_placement, ir) > 0


# --------------------------------------------------------------------------
# the three refusals, each through BOTH entry points


def test_a_fault_test_is_refused_through_placement():
    """Before the shared entry this document placed, and the emitted Go was
    identical to the control's -- the fault test was silently dropped."""
    ir = _doc(trailer='''
fault test "the scheduler reverts its acquisition" for Sched {
  fail at step 1
  assert no residue
}
''')
    assert len(ir.get("fault_tests") or []) == 1
    through_emit = _refusal(go.emit, ir)
    through_placement = _refusal(go.emit_placement, ir)
    assert "fault tests do not lower to the cordis-go tier" in through_emit
    assert through_placement == through_emit


def test_a_called_deferred_emission_is_refused_through_placement():
    """The extern carries a `@go` body on purpose. Without one the placement
    path refused it for being unportable, which looked like the gate working
    and was not: remove the `deferred` modifier and that refusal stays, while
    this one goes away."""
    ir = _doc(
        extra_service="  emission fn announce(msg: Str)\n",
        extern='extern emission deferred fn publish(msg: Str) -> Unit = @go {\n'
               '\t_ = msg\n'
               '\treturn\n'
               '}\n',
        extra_provide="    fn announce(msg) { emit publish(msg) }\n")
    through_emit = _refusal(go.emit, ir)
    through_placement = _refusal(go.emit_placement, ir)
    assert "`deferred` emission `publish` needs a session owner" in through_emit
    assert through_placement == through_emit


def test_the_deferred_control_without_the_modifier_still_places():
    """The other half of the case above. The same document, `deferred`
    removed, is admissible through both entry points -- so the refusal keys on
    the modifier rather than on anything else about the document."""
    ir = _doc(
        extra_service="  emission fn announce(msg: Str)\n",
        extern='extern emission fn publish(msg: Str) -> Unit = @go {\n'
               '\t_ = msg\n'
               '\treturn\n'
               '}\n',
        extra_provide="    fn announce(msg) { emit publish(msg) }\n")
    assert _emits(go.emit, ir) > 0
    assert _emits(go.emit_placement, ir) > 0


def test_a_typed_hole_is_refused_through_placement_by_name():
    """A hole was already refused through placement, but by the expression
    renderer running out of arms (`unsupported expr kind: 'hole'`). It is the
    document-level refusal that must answer, in docs/holes.md's words."""
    ir = _doc(report_body='hole[Row] "the row is not written yet"')
    through_emit = _refusal(go.emit, ir)
    through_placement = _refusal(go.emit_placement, ir)
    assert "typed hole(s)" in through_emit
    assert "docs/holes.md" in through_emit
    assert through_placement == through_emit
    assert "unsupported expr kind" not in through_placement


# --------------------------------------------------------------------------
# and the part that keeps it fixed


def test_every_document_level_refusal_is_on_the_shared_entry():
    """The hand-kept mirror, removed. A refusal that reads the whole document
    is identified by its signature -- one parameter named `ir` -- and every one
    of them must be on the shared list, so adding a fourth cannot reach one
    entry point and not the other.

    An exclusion has to be written here, with a reason, rather than simply not
    being added anywhere."""
    document_level = set()
    for name, obj in vars(go).items():
        if not name.startswith("_refuse_") or not inspect.isfunction(obj):
            continue
        params = list(inspect.signature(obj).parameters)
        if params and params[0] == "ir":
            document_level.add(name)
    # the runner itself matches the shape it runs
    document_level.discard("_refuse_inadmissible_document")

    registered = {fn.__name__ for fn in go._DOCUMENT_REFUSALS}
    # Path-specific, not tier-wide: it refuses a stream document that also
    # declares a top-level `fn` because the live stc-go path renders no
    # top-level `fn` and the pure path drops the stream's component. The
    # combined renderer behind `emit_placement` drops neither, so that document
    # places there and refusing it would reject Go this tier does emit.
    path_specific = {"_refuse_stream_document_top_level"}

    assert document_level - path_specific == registered, (
        "a document-level refusal is not on the shared entry: "
        f"{sorted(document_level - path_specific - registered)}")
    assert registered.isdisjoint(path_specific)


def test_the_path_specific_refusal_is_not_applied_to_placement():
    """The measured reason the exclusion above is right rather than an excuse.
    A stream document with a top-level `fn` is refused through `emit`, and
    places through `emit_placement` WITH both the function and the stream in
    the output. Refusing it there would be a regression, not a fix."""
    ir = compile_source("""\
type Row = { id: Int, name: Str }

service Scheduler {
  fn report() -> Row
}

fn double(n: Int) -> Int = n + n

component Sched provides sched: Scheduler {
  let src = effect Stream.source() undo src.close()

  provide sched {
    fn report() = { id: 1, name: "ada" }
  }
}
""", "stream_placement.rvl")
    assert go._document_holds_stream(ir)
    assert "holds a stream" in _refusal(go.emit, ir)
    placed = go.emit_placement(copy.deepcopy(ir), package="emitted")
    assert "func double(n int64) int64 {" in placed
    assert "Stream" in placed

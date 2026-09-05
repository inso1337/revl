"""A raising method-body inverse must be observable — issue #321 (b).

An activation-body effect's inverse is a bare ``yield lambda: <undo>`` that
cordis reaches through ``Frame._tracked``, the guard chokepoint that catches a
raising inverse and records it into ``Frame.compensation_residue`` (the
contract's Phase-1 "continue-and-record", docs/design/teardown-contract.md).

A PROVIDE-METHOD body effect (``backends/python/emit.py``'s ``_method_step``
``effect`` arm) does NOT pass through ``_tracked``: its generator is adopted
directly onto the frame. Before this fix its ``yield lambda: <undo>`` was
unguarded, so a raising undo escaped to the cordis unwind — logged, never
recorded, and therefore invisible to both ``revl test`` (the lifecycle test
still PASSed its ``assert no_residue``) and ``revl run``'s teardown proof
("no residue"). The method-body ``let-effect`` inverse was already guarded
inside ``Frame.acquire``; only the unbound ``effect`` arm leaked.

The fix routes that yield through ``_revl_frame._guard`` at the emit site, so a
raising method-body inverse lands as a ``bracket-fault`` residue exactly like an
activation-body one, and the part-(a) surfacing (item: "Fix inverse residue
reporting") then makes it observable from ``revl test`` and ``revl run``.
"""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the raising-inverse observability proof drives a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under its venv",
)

# A provide-method whose single effect step is an UNBOUND `effect ... undo ...`
# (the `_method_step` `effect` arm, not the `let-effect` arm) whose undo RAISES.
# The effect is registered per tool call, so a lifecycle/REPL `call` is what
# arms the fault; teardown replays it.
_SOURCE = (
    "type H = { id: Int }\n"
    "extern pure fn boom() -> Unit = @py { raise RuntimeError('undo exploded') }\n"
    "extern pure fn rel(h: H) -> Unit = @py { return }\n"
    "extern acquire fn acq(tag: Str) -> H undo rel(result)"
    " = @py { return {'id': 1} }\n"
    "service Ops { fn run(tag: Str) -> Int }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn run(tag) {\n"
    "      effect acq(tag) undo boom()\n"
    "      return 1\n"
    "    }\n"
    "  }\n"
    "}\n"
)

# The same component driven by a lifecycle test: load -> call the method (arms
# the method-body effect) -> unload -> `assert no_residue`. This is the issue's
# `revl test` reproducer, which used to report PASS.
_LIFECYCLE = _SOURCE + (
    'lifecycle test "method undo raises" {\n'
    '  load Agent\n'
    '  let x = call ops.run("a")\n'
    '  assert x == 1\n'
    '  unload Agent\n'
    '  assert no_residue\n'
    "}\n"
)


def _emitter():
    spec = importlib.util.spec_from_file_location(
        "revl_321_emit_py", _BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sole_frame(session):
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    frame = driver.runtime._frame_for_ctx(fiber.ctx)
    assert frame is not None, "component frame not found via ctx"
    return frame


# ---------------------------------------------------------------------------
# 1. emit-level: the method-body effect inverse is routed through the guard.
#    No cordis needed — this is the regression fence that survives an emitter
#    refactor even where the runtime is not installed.
# ---------------------------------------------------------------------------

def test_method_body_effect_inverse_is_routed_through_guard():
    ir = compile_source(_SOURCE, "issue_321.rvl")
    source = _emitter().emit(ir)
    # the adopted method-body effect generator is present ...
    assert "_revl_frame.adopt(_revl_ctx.effect(_effect_0" in source
    # ... and its yielded inverse is guarded, not a bare lambda.
    assert "yield _revl_frame._guard(lambda: boom())" in source
    assert "yield lambda: boom()" not in source


# ---------------------------------------------------------------------------
# 2. runtime: a raising method-body inverse lands as `bracket-fault` residue on
#    the frame (was silently dropped to the cordis logger before the fix), and
#    the unwind still completes (continue-and-record, teardown-contract.md).
# ---------------------------------------------------------------------------

@needs_cordis
def test_raising_method_body_inverse_records_bracket_fault_residue():
    from revl.mcp.session import Session

    ir = compile_source(_SOURCE, "issue_321.rvl")
    session = Session()
    session.load(copy.deepcopy(ir))
    frame = _sole_frame(session)

    assert frame.compensation_residue == []      # nothing until the call
    assert session.call("ops", "run", ["a"])["result"] == 1
    assert frame.compensation_residue == []       # armed, not yet replayed

    session.unload()                               # must not raise

    residue = frame.compensation_residue
    assert len(residue) == 1
    record = residue[0]
    assert record["kind"] == "bracket-fault"
    assert record["outcome"] == "failed"
    assert record["attempted"] == {"phase": 1}
    assert record["error"]["type"] == "RuntimeError"
    assert record["error"]["message"] == "undo exploded"


# ---------------------------------------------------------------------------
# 3. observability: the issue's `revl test` reproducer. The lifecycle test
#    calls the method then asserts `no_residue`; the raising method-body inverse
#    must flip it to FAIL rather than the pre-fix silent PASS.
# ---------------------------------------------------------------------------

@needs_cordis
def test_revl_test_reports_the_raising_method_body_inverse():
    from revl import test as revl_test

    ir = compile_source(_LIFECYCLE, "issue_321_lifecycle.rvl")
    status, summary = revl_test.run_py(ir)
    assert status == "fail", (status, summary)
    text = summary if isinstance(summary, str) else " ".join(map(str, summary))
    assert "0 of 1 test(s) passed" in text

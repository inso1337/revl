"""Issue #1406: every entry point that loads a backend emitter, and what each
one does with a refusal.

`EmitError` is defined INSIDE each dynamically loaded backend `emit.py`. It is
not importable from `src/`, loading `emit.py` twice yields two unrelated
classes, and it inherits from `ValueError`. So every caller that loads an
emitter has to decide, by hand, between three outcomes:

1. the backend module is ABSENT here -- a legitimate quiet skip;
2. the emitter REFUSED this document -- an answer, which the caller must report;
3. the emitter FAULTED -- a bug, which must stay loud.

Three PRs had each fixed one entry point that got this wrong, and each was found
by accident while someone was looking at something else (#1393 `revl run` handed
the refusal over as a 26-line traceback, #1400 `revl bundle` swallowed refusals
AND real crashes and exited 0, #1403 `truc reproduce` caught only `ImportError`
so a refusal escaped the attestation path). The remaining entry points had never
been looked at. This file is the audit.

WHAT WAS MEASURED, on `main` at fc0d84ce9, python 3.12, with the pinned cordis-py
fork installed, over documents the real emitters really refuse:

    entry point                        before                      after
    ---------------------------------- --------------------------- --------------
    run.py           `revl run`        32-line traceback           `error: <d>` + stage, exit 1
    test.py          `revl test`       26-line traceback           `[py] fail: emitter refused: <d>`
    run_java.py      `revl run -b java`raw EmitError out of run_java`error: could not build ...`, exit 1
    placement.py     plan gate         refusal OK, FAULT laundered  fault propagates
    placement.py     `_build_java`     raw EmitError (traceback)   `RuntimeError: java emit failed`
    placement.py     `_build_rust`     emitter traceback relayed   the diagnostic, relayed
    placement.py     `_build_go`       refusal OK, FAULT laundered  fault propagates
    run_wasm.py      `revl run -b wasm`refusal OK, FAULT laundered  fault propagates
    mcp/session.py   `revl_load`       16-frame traceback in proc,  `SessionError`,
                                       `category: "internal"` over  `category: "session"`
                                       the transport
    gate.py          `compile_to`      `code: "COMPILER_FAULT"`    `code: "TIER_REFUSED"`
    fault.py         prop/fault runner "the prop-test driver raised "the py emitter refused
                                       EmitError: ..." under "1     this document: ..."
                                       broke"
    _process_runner  placement child   one `FATAL` line + exit 1   unchanged, see the census
    mcp/quarantine   wasm canonical    already correct             unchanged

THE CONTROL MATTERS AS MUCH AS THE FIX, and here it matters twice.

`EmitError` inherits from `ValueError`, so the lazy version of every fix below --
catching `ValueError` -- passes every "no traceback" assertion in this file while
quietly swallowing half of each emitter's real internal faults. Every assertion
that a refusal is reported is therefore paired with a control in which the same
call FAULTS, and several of the fixes below are narrowings of an `except
Exception` that was laundering a compiler bug into a sentence about the author's
program.

And a census gate is worthless if it cannot fire, which this repository has been
bitten by. `test_the_census_scanner_detects_a_new_entry_point` runs the scanner
over synthetic source and asserts it flags an unrecorded emitter load. That
control is a pure function over a string, so unlike a guard that proves itself
from a live defect it keeps working after every defect above is fixed.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402
from revl import gate as gate_mod  # noqa: E402
from revl import placement as placement_mod  # noqa: E402
from revl import refusal as refusal_mod  # noqa: E402
from revl import run_java as run_java_mod  # noqa: E402
from revl import run_wasm as run_wasm_mod  # noqa: E402

HAVE_CORDIS = importlib.util.find_spec("cordis") is not None


# ---------------------------------------------------------------------------
# documents the real emitters really refuse
# ---------------------------------------------------------------------------

#: A `validated` extern. Refused by NAME on the py tier (issue #1382), and on ts,
#: rust, java and go for the simpler reason that it carries no body they can
#: spell. One document reaches five of the six tiers.
PROGRAM = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])

extern emission[model] validated fn complete(h: Str) -> AgentTurn = @py {
  return {"kind": "Final", "value": "x"}
}

service Counter {
  fn next() -> Int
}

component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 42
  }
}
"""

#: The wasm tier lowers `PROGRAM` happily (it never reaches the extern), so its
#: refusal needs its own document: a required `Stream[T]` coeffect, which that
#: tier refuses by name because it resolves a requirement against a SERVICE.
WASM_PROGRAM = """
service Counter { fn next() -> Int }
component CounterSvc requires src: Stream[Int] provides counter: Counter {
  provide counter { fn next() = 1 }
}
"""

TEST_PROGRAM = """
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])

extern emission[model] validated fn complete(h: Str) -> AgentTurn = @py {
  return {"kind": "Final", "value": "x"}
}

fn double(n: Int) -> Int = n * 2

test "double doubles" {
  assert double(2) == 4
}
"""

#: A document every tier lowers, for the controls that need an ADMITTED tier.
#: Deliberately free of any `validated` crossing: `PROGRAM` is refused by all six
#: tiers since #1413, so it can no longer serve as its own green control.
PLAIN_PROGRAM = """
service Counter { fn next() -> Int }
component CounterSvc provides counter: Counter {
  provide counter { fn next() = 42 }
}
"""

#: A fragment of the py tier's own refusal. Every rendering below has to carry it.
PY_REFUSAL = "`validated` extern `complete`"

_TIER_DIRS = {"py": "python", "ts": "typescript", "rust": "rust",
              "java": "java", "go": "go", "wasm": "wasm"}


def tier_refusal_text(tier: str, ir: dict, call: str = "emit", **kwargs) -> str:
    """What THIS tier's emitter itself says when it refuses `ir`, run live.

    Every "the entry point reported the refusal" assertion below compares against
    this rather than against a fragment written down in this file, and that is a
    deliberate correction rather than a convenience.

    The fragment version pinned a WORDING, and a wording belongs to the emitter.
    When PR #1413 landed (five tiers refusing a `validated` emission by name),
    the ts/rust/java/go/wasm refusal for this file's own document changed from
    "not portable to this backend" to "needs the response-validation seam", and
    seven assertions here went red although every entry point was still doing
    exactly the right thing. Re-pinning a different fragment would only re-arm
    the same trap for the next lane that improves a diagnostic.

    Reading the text off the emitter is also STRICTLY STRONGER than a fragment.
    The property this file exists to hold is that each entry point RELAYS the
    emitter's own diagnostic instead of inventing one of its own; a substring
    both sides happen to contain never tested that, and this does.

    It raises rather than returning "" when the emitter does NOT refuse. An empty
    expected string would make every `assert expected in reported` below pass
    against anything at all, which is the vacuous-guard shape this file is partly
    about.
    """
    path = ROOT / "backends" / _TIER_DIRS[tier] / "emit.py"
    spec = importlib.util.spec_from_file_location(f"live_refusal_{tier}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        getattr(module, call)(json.loads(json.dumps(ir)), **kwargs)
    except refusal_mod.refusals(module) as refusal:
        return str(refusal).strip()
    raise AssertionError(
        f"the {tier} emitter did NOT refuse this document, so there is no "
        f"refusal text to compare against and every assertion using it would "
        f"be vacuous. Pick a document this tier refuses.")


def _ir(source: str) -> dict:
    return compile_source(source, "agent.rvl")


@pytest.fixture(scope="module")
def ir() -> dict:
    return compile_source(PROGRAM, "agent.rvl")


@pytest.fixture(scope="module")
def wasm_ir() -> dict:
    return compile_source(WASM_PROGRAM, "stream.rvl")


# ---------------------------------------------------------------------------
# fake emitter modules: the two controls every assertion below is paired with
# ---------------------------------------------------------------------------

def _fake_emitter(exc_factory, *, with_refusal_class: bool = True):
    """An emitter module object whose `emit` raises what `exc_factory` builds.

    `with_refusal_class` decides whether the module carries an `EmitError` at
    all, which is the "module declares no refusal vocabulary" arm: a caller must
    then treat everything it raises as a fault.
    """
    module = types.ModuleType("fake_emit")
    if with_refusal_class:
        class EmitError(ValueError):
            pass
        module.EmitError = EmitError

    def emit(ir, *args, **kwargs):
        raise exc_factory(module)

    module.emit = emit
    module.emit_placement = lambda ir, package: emit(ir)
    return module


def _refusing(message="this tier cannot lower that"):
    return _fake_emitter(lambda m: m.EmitError(message))


def _faulting():
    """The FAULT control: an internal emitter bug, the shape a real one takes."""
    return _fake_emitter(
        lambda m: AttributeError("'NoneType' object has no attribute 'get'"))


def _bare_value_error():
    """THE control for this issue. `EmitError` inherits from `ValueError`, so a
    fix that catches `ValueError` passes every green assertion in this file. This
    module carries its own `EmitError` and raises a bare `ValueError` instead: a
    caller that discriminates correctly must treat it as a FAULT."""
    return _fake_emitter(lambda m: ValueError("an emitter bug that is a ValueError"))


# ---------------------------------------------------------------------------
# the shared discrimination
# ---------------------------------------------------------------------------

def test_the_refusal_class_is_read_off_the_module_object():
    """Why the helper exists. Two loads of the same `emit.py` produce two
    unrelated `EmitError` classes, so the class has to come off the module that
    will raise and not off any other one."""
    path = ROOT / "backends" / "python" / "emit.py"

    def load(name):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    first, second = load("emit_probe_one"), load("emit_probe_two")
    assert refusal_mod.refusal_class(first) is first.EmitError
    assert refusal_mod.refusal_class(second) is second.EmitError
    assert first.EmitError is not second.EmitError
    assert not isinstance(second.EmitError("x"), first.EmitError)


def test_a_module_with_no_refusal_class_catches_nothing():
    """`except ()` catches nothing, which is the right answer for a module that
    declares no refusal vocabulary: everything it raises is a fault."""
    bare = types.ModuleType("no_emit_error")
    assert refusal_mod.refusal_class(bare) is None
    assert refusal_mod.refusals(bare) == ()
    assert refusal_mod.is_refusal(bare, ValueError("x")) is False


def test_a_non_exception_named_emit_error_is_not_a_refusal_vocabulary():
    """A module that binds the name to something that is not an exception class
    cannot widen anyone's catch."""
    weird = types.ModuleType("weird")
    weird.EmitError = "not a class"
    assert refusal_mod.refusal_class(weird) is None
    weird.EmitError = dict
    assert refusal_mod.refusal_class(weird) is None


def test_a_subclass_of_the_refusal_class_is_still_a_refusal():
    module = _refusing()

    class Narrower(module.EmitError):
        pass

    assert refusal_mod.is_refusal(module, Narrower("still a refusal"))


def test_a_bare_value_error_is_not_a_refusal():
    """The control the whole issue turns on. `EmitError` IS a `ValueError`."""
    module = _refusing()
    assert issubclass(module.EmitError, ValueError)
    assert refusal_mod.is_refusal(module, ValueError("a bug")) is False


def test_the_two_landed_copies_are_now_the_shared_one():
    """PRs #1402 and #1405 each grew a private `_refusal_class`, which is the
    duplication that argued for a shared one. Both names now point at it."""
    from revl import bundle
    from revl.truc import reproduce

    assert bundle._refusal_class is refusal_mod.refusal_class
    assert reproduce._refusal_class is refusal_mod.refusal_class


# ---------------------------------------------------------------------------
# the census: who loads an emitter, and what each one does about a refusal
# ---------------------------------------------------------------------------

#: The text patterns that mean "this module loads a backend emitter". Kept as
#: source patterns rather than as imports because that is the only way to see a
#: module that has not been written yet.
_LOAD_MARKERS = (
    re.compile(r'"emit\.py"'),                       # spec_from_file_location(...)
    re.compile(r"^\s*import emit\b", re.M),          # backends/python on sys.path
    re.compile(r'import_module\(\s*f?"backends\.'),  # backends.<tier>.emit
)

#: How each entry point answers a refusal. `reports` means it turns the
#: emitter's own sentence into an answer of its own and lets a FAULT through,
#: and every such module must reference the shared discrimination below.
#:
#: The two non-`reports` rows are measured, not assumed:
#:
#: * `_process_runner.py` is a placement CHILD process. Its `main()` catch-all
#:   prints one redacted `[<name>] FATAL <type>: <sentence>` line on stderr and
#:   exits 1, so the emitter's own diagnostic reaches the conductor's merged
#:   trace and the run fails loudly. That funnel is issue #814's contract --
#:   EVERY failure no other funnel saw takes that one shape -- so the refusal is
#:   reported under a `FATAL` label rather than a refusal-shaped one. Recorded
#:   here rather than changed, because relabelling it means changing #814's rule
#:   for every failure and not just this one.
#: * `mcp/quarantine.py` was measured CORRECT before this issue and is left
#:   alone. It reads `EmitError` off `backends/wasm/canonical.py`, which
#:   re-exports the wasm emitter's own class, catches that and nothing wider,
#:   and answers a refusal with a `status: "deferred"` record carrying the
#:   emitter's sentence (measured: a `Float`-only boundary gives `{"kind":
#:   "confined", "status": "deferred", "ran": False}` plus "presents no
#:   canonical-ABI-emittable method"). It is `by-hand` rather than `reports`
#:   only because the class it needs is the one `canonical` already re-exports,
#:   and rewriting the wasm quarantine path to reach the same class a second way
#:   would be change without a defect behind it.
ENTRY_POINTS = {
    "revl/_process_runner.py": "funnelled",
    "revl/bundle.py": "reports",
    "revl/fault.py": "reports",
    "revl/gate.py": "reports",
    "revl/mcp/quarantine.py": "by-hand",
    "revl/mcp/session.py": "reports",
    "revl/placement.py": "reports",
    "revl/run.py": "reports",
    "revl/run_java.py": "reports",
    "revl/run_wasm.py": "reports",
    "revl/test.py": "reports",
    "revl/truc/reproduce.py": "reports",
}


def _scan(source: str) -> bool:
    """Whether `source` loads a backend emitter."""
    return any(marker.search(source) for marker in _LOAD_MARKERS)


def _entry_points_on_disk() -> dict[str, str]:
    src = ROOT / "src"
    return {str(path.relative_to(src)): path.read_text(encoding="utf-8")
            for path in sorted((src / "revl").rglob("*.py"))
            if _scan(path.read_text(encoding="utf-8"))}


def test_the_census_scanner_detects_a_new_entry_point():
    """THE non-vacuity control for the census gate below.

    A gate that cannot fire is worse than no gate, and this repository has shipped
    that defect more than once. This runs the scanner over synthetic source rather
    than over a live defect, so it keeps proving the gate works after every real
    entry point is correct.
    """
    for source in (
        'spec = importlib.util.spec_from_file_location("x", D / "emit.py")',
        "def load():\n    import emit\n    return emit",
        'module = importlib.import_module(f"backends.{backend}.emit")',
    ):
        assert _scan(source), source
    assert not _scan("# this module mentions an emitter but loads none\nimport json")


def test_the_live_refusal_text_helper_cannot_go_vacuous():
    """The non-vacuity control for `tier_refusal_text` itself.

    Every relay assertion in this file is `assert tier_refusal_text(...) in
    reported`. If the helper ever answered "" for a document a tier stopped
    refusing, all of them would pass against literally any output. It raises
    instead, and this proves it: `PLAIN_PROGRAM` is a document every tier
    lowers.
    """
    with pytest.raises(AssertionError, match="did NOT refuse"):
        tier_refusal_text("rust", _ir(PLAIN_PROGRAM))


def test_the_live_refusal_text_is_the_emitters_own_sentence():
    """And the paired green half: for a document the tier does refuse, the
    helper returns that tier's text and not another tier's."""
    ir = _ir(PROGRAM)
    rust = tier_refusal_text("rust", ir)
    java = tier_refusal_text("java", ir)
    assert rust and java
    assert "rust" in rust and "java" in java
    assert rust != java


def test_the_set_of_emitter_entry_points_is_the_recorded_one():
    """A NEW entry point fails here until it is classified.

    This is the whole of issue #1406 as a gate: three separate PRs each fixed one
    entry point that mishandled a refusal, and every one of the three was found
    by accident. The next one gets found by this test instead.
    """
    on_disk = set(_entry_points_on_disk())
    recorded = set(ENTRY_POINTS)
    assert on_disk == recorded, (
        "the set of modules under src/revl that load a backend emitter has "
        "changed.\n"
        f"  new, unclassified: {sorted(on_disk - recorded)}\n"
        f"  recorded but gone: {sorted(recorded - on_disk)}\n"
        "An emitter can REFUSE a document (an answer, which you must report), "
        "be ABSENT (a quiet skip) or FAULT (a bug, which must stay loud). "
        "Decide which of the three your entry point does, use "
        "`revl.refusal.refusals` to tell them apart, and add it to "
        "ENTRY_POINTS with a test beside the others in this file."
    )


def test_every_reporting_entry_point_uses_the_shared_discrimination():
    """Each `reports` module must reach for `revl.refusal` rather than remember
    the three-way distinction on its own. Six callers each keeping their own copy
    is the hand-kept-mirror shape; two of them had already drifted into private
    copies of the same helper by the time this issue was filed."""
    sources = _entry_points_on_disk()
    missing = [name for name, discipline in ENTRY_POINTS.items()
               if discipline == "reports"
               and "refusal import" not in sources.get(name, "")]
    assert missing == [], (
        f"{missing} load an emitter and are recorded as reporting its refusals, "
        "but do not import the shared discrimination from `revl.refusal`."
    )


def test_the_census_disciplines_are_the_three_the_audit_found():
    assert set(ENTRY_POINTS.values()) == {"reports", "funnelled", "by-hand"}


def test_the_by_hand_entry_point_still_discriminates_correctly():
    """The one entry point exempted from the shared helper has to keep earning
    it, or the exemption is a hole. `canonical.EmitError` must stay the wasm
    emitter's own refusal class, which is precisely what the shared helper would
    have answered."""
    from revl.mcp import quarantine

    canonical = quarantine._load_canonical()
    assert canonical is not None
    assert refusal_mod.refusal_class(canonical) is canonical.EmitError


# ---------------------------------------------------------------------------
# gate.py -- a tier limit is not a compiler fault
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["py", "ts", "rust", "java", "go"])
def test_compile_to_answers_a_tier_refusal_with_its_own_code(tier):
    """Before: every one of these came back `COMPILER_FAULT`, whose own contract
    says it carries "an exception the reference compiler should never have
    raised, which is a bug report and not a repair signal for the author". A
    named tier limit is exactly a repair signal."""
    emitted = gate_mod.compile_to(PROGRAM, tier)
    assert emitted.verdict.admitted is False
    assert emitted.output is None              # still fail closed
    assert emitted.verdict.code == gate_mod.TIER_REFUSED
    assert emitted.verdict.code != gate_mod.FAULT
    # the emitter's own sentence, verbatim, not a fragment written down here
    assert tier_refusal_text(tier, _ir(PROGRAM)) in emitted.verdict.message


def test_compile_to_still_calls_an_emitter_fault_a_compiler_fault(monkeypatch):
    """The control. A bug inside an emitter must keep saying it is one."""
    def boom(ir, tier):
        raise AttributeError("'NoneType' object has no attribute 'get'")

    monkeypatch.setattr(gate_mod, "_emit_for_tier", boom)
    emitted = gate_mod.compile_to("fn f() -> Int = 1", "py")
    assert emitted.verdict.admitted is False
    assert emitted.verdict.code == gate_mod.FAULT
    assert emitted.output is None


def test_compile_to_calls_a_bare_value_error_a_compiler_fault(monkeypatch):
    """The second control, and the one that fails against the lazy fix. An
    emitter raising a bare `ValueError` is faulting, not refusing, even though
    `EmitError` is a `ValueError` too."""
    def boom(ir, tier):
        raise ValueError("an emitter bug that happens to be a ValueError")

    monkeypatch.setattr(gate_mod, "_emit_for_tier", boom)
    emitted = gate_mod.compile_to("fn f() -> Int = 1", "py")
    assert emitted.verdict.code == gate_mod.FAULT


def test_compile_to_still_admits_a_tier_that_lowers_the_document():
    emitted = gate_mod.compile_to(PLAIN_PROGRAM, "wasm")
    assert emitted.verdict.admitted is True
    assert emitted.output


# ---------------------------------------------------------------------------
# placement.py -- the plan-time capability gate and the per-tier builds
# ---------------------------------------------------------------------------

_PLACED = {"CounterSvc": "p1"}
_BACKENDS = {"p1": "rust"}


def test_the_plan_gate_reports_a_tier_refusal(ir):
    problem = placement_mod.tier_capability_gate(ir, _PLACED, _BACKENDS)
    assert problem is not None
    assert "cannot be placed on the `rust` tier" in problem
    assert tier_refusal_text("rust", ir) in problem


def test_the_plan_gate_lets_an_emitter_fault_through(ir, monkeypatch):
    """The control, and the half that was wrong. `except Exception` reported
    `'NoneType' object has no attribute 'get'` as "component 'CounterSvc' cannot
    be placed on the `rust` tier", which sends an author to rewrite a program
    that was never the problem."""
    monkeypatch.setattr(placement_mod, "_emit_gate_module",
                        lambda backend: _faulting())
    with pytest.raises(AttributeError):
        placement_mod.tier_capability_gate(ir, _PLACED, _BACKENDS)


def test_the_plan_gate_lets_a_bare_value_error_through(ir, monkeypatch):
    monkeypatch.setattr(placement_mod, "_emit_gate_module",
                        lambda backend: _bare_value_error())
    with pytest.raises(ValueError, match="an emitter bug"):
        placement_mod.tier_capability_gate(ir, _PLACED, _BACKENDS)


def test_the_plan_gate_still_reports_a_module_refusal(ir, monkeypatch):
    """The paired green half: the same fake, refusing instead of faulting, is
    still turned into a plan diagnostic."""
    monkeypatch.setattr(placement_mod, "_emit_gate_module",
                        lambda backend: _refusing("this tier cannot spell it"))
    problem = placement_mod.tier_capability_gate(ir, _PLACED, _BACKENDS)
    assert problem is not None
    assert "this tier cannot spell it" in problem


def test_the_node_arm_refusal_is_a_plan_refusal():
    """The gate's own ts refusal (a `@py`-only extern a node-placed component
    reaches) is a `PlanRefusal`, so narrowing the gate's catch did not drop it.
    It stays a `RuntimeError` subclass, which is what every caller of this
    module's build path already catches."""
    assert issubclass(placement_mod.PlanRefusal, RuntimeError)


def test_build_java_reports_a_refusal_as_a_runtime_error(ir):
    """Before: a raw `EmitError`, which is a `ValueError` and so matched neither
    the conductor's `except (RevlError, RuntimeError, OSError)` nor `revl run`'s,
    and left as a traceback."""
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(RuntimeError) as caught:
            placement_mod._build_java(ir, Path(tmp))
    assert "java emit failed" in str(caught.value)
    assert tier_refusal_text("java", ir) in str(caught.value)


def test_build_go_reports_a_refusal_as_a_runtime_error(ir):
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(RuntimeError) as caught:
            placement_mod._build_go(ir, Path(tmp))
    assert "go emit failed" in str(caught.value)
    assert tier_refusal_text("go", ir, call="emit_placement",
                             package="emitted") in str(caught.value)


def test_build_go_lets_an_emitter_fault_through(ir, monkeypatch):
    """The control for the narrowing. `_build_go` caught `Exception`, so an
    emitter bug arrived as `RuntimeError: go emit failed: <the bug>` and read as
    a property of the document."""
    monkeypatch.setattr(placement_mod, "_tier_emitter",
                        lambda name, path: _faulting())
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(AttributeError):
            placement_mod._build_go(ir, Path(tmp))


def test_build_go_lets_a_bare_value_error_through(ir, monkeypatch):
    monkeypatch.setattr(placement_mod, "_tier_emitter",
                        lambda name, path: _bare_value_error())
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="an emitter bug"):
            placement_mod._build_go(ir, Path(tmp))


def test_build_java_lets_an_emitter_fault_through(ir, monkeypatch):
    monkeypatch.setattr(placement_mod, "_tier_emitter",
                        lambda name, path: _faulting())
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(AttributeError):
            placement_mod._build_java(ir, Path(tmp))


def test_build_java_lets_a_bare_value_error_through(ir, monkeypatch):
    monkeypatch.setattr(placement_mod, "_tier_emitter",
                        lambda name, path: _bare_value_error())
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError, match="an emitter bug"):
            placement_mod._build_java(ir, Path(tmp))


def test_the_rust_emitter_cli_renders_its_own_refusal(ir):
    """`placement._build_rust` and `run_rust` run the rust emitter as a
    SUBPROCESS and relay its stderr verbatim, so an uncaught `EmitError` in that
    CLI arrived as a Python traceback two processes away from the code that
    raised it."""
    with tempfile.TemporaryDirectory() as tmp:
        ir_json = Path(tmp) / "ir.json"
        ir_json.write_text(json.dumps(ir), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "backends" / "rust" / "emit.py"),
             str(ir_json)],
            capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.startswith("error: ")
    assert tier_refusal_text("rust", ir) in result.stderr
    assert "Traceback" not in result.stderr


def test_the_typescript_emitter_cli_renders_its_own_refusal(ir):
    with tempfile.TemporaryDirectory() as tmp:
        ir_json = Path(tmp) / "ir.json"
        ir_json.write_text(json.dumps(ir), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "backends" / "typescript" / "emit.py"),
             str(ir_json)],
            capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.startswith("error: ")
    assert tier_refusal_text("ts", ir) in result.stderr
    assert "Traceback" not in result.stderr


def test_both_emitter_clis_still_emit_a_document_they_can_lower():
    """The control for the two CLI guards: the happy path is byte-unchanged and
    still exits 0 with the emission on stdout."""
    ok_ir = compile_source("fn double(n: Int) -> Int = n * 2", "ok.rvl")
    for backend in ("rust", "typescript"):
        with tempfile.TemporaryDirectory() as tmp:
            ir_json = Path(tmp) / "ir.json"
            ir_json.write_text(json.dumps(ok_ir), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "backends" / backend / "emit.py"),
                 str(ir_json)],
                capture_output=True, text=True)
        assert result.returncode == 0, (backend, result.stderr[-400:])
        assert result.stdout.strip()


# ---------------------------------------------------------------------------
# run_wasm.py
# ---------------------------------------------------------------------------

def test_run_wasm_reports_a_refusal(wasm_ir):
    err = io.StringIO()
    with redirect_stderr(err):
        code = run_wasm_mod.run_wasm(wasm_ir, {}, None, once=True)
    assert code == 1
    assert err.getvalue().startswith("error: could not emit the wasm composition")
    assert "not lowered on the wasm tier" in err.getvalue()


def test_run_wasm_lets_an_emitter_fault_through(wasm_ir, monkeypatch):
    """The control, and the half that was wrong: `except Exception` reported an
    `AttributeError` from a buggy emitter under "the substrate tier is the
    strictest emitter", which reads as a fact about the author's document."""
    monkeypatch.setattr(run_wasm_mod, "_wasm_emitter", _faulting)
    with pytest.raises(AttributeError):
        run_wasm_mod.run_wasm(wasm_ir, {}, None, once=True)


def test_run_wasm_lets_a_bare_value_error_through(wasm_ir, monkeypatch):
    monkeypatch.setattr(run_wasm_mod, "_wasm_emitter", _bare_value_error)
    with pytest.raises(ValueError, match="an emitter bug"):
        run_wasm_mod.run_wasm(wasm_ir, {}, None, once=True)


def test_run_wasm_still_reports_a_module_refusal(wasm_ir, monkeypatch):
    monkeypatch.setattr(run_wasm_mod, "_wasm_emitter",
                        lambda: _refusing("wasm cannot lower this"))
    err = io.StringIO()
    with redirect_stderr(err):
        code = run_wasm_mod.run_wasm(wasm_ir, {}, None, once=True)
    assert code == 1
    assert "wasm cannot lower this" in err.getvalue()


# ---------------------------------------------------------------------------
# run_java.py
# ---------------------------------------------------------------------------

def test_run_java_reports_a_refusal(ir):
    """Before: the raw `EmitError` escaped `run_java` entirely. It is a
    `ValueError`, so it matched none of `(RevlError, RuntimeError, OSError)`."""
    err, out = io.StringIO(), io.StringIO()
    with redirect_stderr(err), redirect_stdout(out):
        code = run_java_mod.run_java(ir, {}, None, once=True)
    assert code == 1
    assert "could not build the java composition" in err.getvalue()
    assert tier_refusal_text("java", ir) in err.getvalue()


def test_run_java_lets_an_emitter_fault_through(ir, monkeypatch):
    monkeypatch.setattr(run_java_mod, "_java_emitter", _faulting)
    with pytest.raises(AttributeError):
        run_java_mod.run_java(ir, {}, None, once=True)


def test_run_java_lets_a_bare_value_error_through(ir, monkeypatch):
    monkeypatch.setattr(run_java_mod, "_java_emitter", _bare_value_error)
    with pytest.raises(ValueError, match="an emitter bug"):
        run_java_mod.run_java(ir, {}, None, once=True)


def test_the_java_refusal_class_comes_off_the_module_that_raises(monkeypatch):
    """Why `run_java` loads the emitter in `run_java` and hands it down to
    `_build`. A second `exec_module` produces a DIFFERENT `EmitError` class, and
    an `except` against it would not catch the first module's instance."""
    first = run_java_mod._java_emitter()
    second = run_java_mod._java_emitter()
    assert first.EmitError is not second.EmitError
    assert not isinstance(second.EmitError("x"), first.EmitError)


# ---------------------------------------------------------------------------
# fault.py
# ---------------------------------------------------------------------------

def test_a_driver_names_an_emitter_refusal_as_one():
    """Before: "the prop-test driver raised EmitError: ..." under a "1 broke"
    summary, which claims the property was checked and found false when it was
    never evaluated."""
    module = _refusing("the py tier cannot lower this")
    sentence = fault_mod._driver_failure(
        module, module.EmitError("the py tier cannot lower this"), "prop-test")
    assert sentence == ("the py emitter refused this document: "
                        "the py tier cannot lower this")


def test_a_driver_still_names_a_crash_a_crash():
    module = _refusing()
    sentence = fault_mod._driver_failure(
        module, AttributeError("'NoneType' object has no attribute 'get'"),
        "prop-test")
    assert sentence.startswith("the prop-test driver raised AttributeError:")


def test_a_driver_calls_a_bare_value_error_a_crash():
    """The control that fails against the lazy fix."""
    module = _refusing()
    sentence = fault_mod._driver_failure(
        module, ValueError("an emitter bug"), "round-trip")
    assert sentence.startswith("the round-trip driver raised ValueError:")


@pytest.mark.skipif(not HAVE_CORDIS, reason="the cordis-py runtime is not installed")
def test_the_prop_runner_names_the_refusal_end_to_end():
    ir = compile_source(
        PROGRAM + '\nfn double(n: Int) -> Int = n * 2\n'
                  'prop test "double doubles" (n: Int) { assert double(n) == n * 2 }\n',
        "prop.rvl")
    lines: list[str] = []
    failures, _ = fault_mod.run_prop_units(ir, ir.get("prop_tests") or [],
                                           out=lines.append)
    assert failures == 1
    failed = [line for line in lines if line.startswith("FAIL")]
    assert len(failed) == 1
    assert "the py emitter refused this document" in failed[0]
    assert PY_REFUSAL in failed[0]
    assert "driver raised" not in failed[0]


# ---------------------------------------------------------------------------
# mcp/session.py
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_CORDIS, reason="the cordis-py runtime is not installed")
def test_session_load_answers_a_refusal_as_a_session_error(ir):
    """Before: a raw 16-frame `EmitError` in process, and `category: "internal"`
    over the transport, because the server's generic handler is what caught it."""
    from revl.mcp.session import Session, SessionError

    with pytest.raises(SessionError) as caught:
        Session().load(ir, {})
    assert "the py emitter refused this composition" in str(caught.value)
    assert PY_REFUSAL in str(caught.value)


@pytest.mark.skipif(not HAVE_CORDIS, reason="the cordis-py runtime is not installed")
def test_the_transport_calls_the_refusal_a_session_answer_not_an_internal_fault():
    """The end-to-end half. An agent branching on `category` was told a named
    tier limit was an internal revl fault, so the useful reaction (drop
    `validated`, or move the crossing to a service) was the one it could not
    reach without parsing prose."""
    import revl.mcp.server as server

    saved = server.AUTHORING
    try:
        # the untrusted-author profile refuses a host-block `extern` at G8, before
        # emission, so the trusted-author arm is the one that reaches the emitter
        server.set_authoring_trust(host_code=True)
        response = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "revl_load",
                       "arguments": {"source": PROGRAM, "filename": "agent.rvl"}},
        })
    finally:
        server.AUTHORING = saved
    payload = response["result"]["structuredContent"]
    assert payload["ok"] is False
    diagnostic = payload["diagnostics"][0]
    assert diagnostic["category"] == "session"
    assert diagnostic["category"] != "internal"
    assert PY_REFUSAL in diagnostic["message"]


@pytest.mark.skipif(not HAVE_CORDIS, reason="the cordis-py runtime is not installed")
def test_session_lets_an_emitter_fault_through(ir, monkeypatch):
    """The control. A bug inside the emitter must still reach the transport's
    generic handler, which is where a compiler bug belongs."""
    from revl.mcp.session import Session, SessionError

    session = Session()

    class FaultingDriver:
        emit = _refusing()

        def _emit_module(self, ir):
            raise AttributeError("'NoneType' object has no attribute 'get'")

    with pytest.raises(AttributeError):
        session._emit_or_refuse(FaultingDriver(), ir)

    class BareValueErrorDriver:
        emit = _refusing()

        def _emit_module(self, ir):
            raise ValueError("an emitter bug that is a ValueError")

    with pytest.raises(ValueError, match="an emitter bug"):
        session._emit_or_refuse(BareValueErrorDriver(), ir)

    class RefusingDriver:
        emit = _refusing("the py tier cannot lower this")

        def _emit_module(self, ir):
            raise self.emit.EmitError("the py tier cannot lower this")

    with pytest.raises(SessionError, match="the py emitter refused this composition"):
        session._emit_or_refuse(RefusingDriver(), ir)


# ---------------------------------------------------------------------------
# the two CLI surfaces, end to end
# ---------------------------------------------------------------------------

def _cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return subprocess.run([sys.executable, "-m", "revl", *args],
                          capture_output=True, text=True, env=env, cwd=str(ROOT))


@pytest.mark.skipif(not HAVE_CORDIS, reason="the cordis-py runtime is not installed")
def test_revl_run_answers_a_refusal_in_two_lines(tmp_path):
    """Measured at 32 lines of traceback before, of which the diagnostic was the
    last one."""
    source = tmp_path / "agent.rvl"
    source.write_text(PROGRAM, encoding="utf-8")
    result = _cli("run", str(source), "--once")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0].startswith("error: " + PY_REFUSAL)
    assert "lifecycle stage: boot" in lines[1]


@pytest.mark.skipif(not HAVE_CORDIS, reason="the cordis-py runtime is not installed")
def test_revl_test_answers_a_refusal_the_way_its_five_sibling_tiers_do(tmp_path):
    """The five non-py runners have reported `emitter refused: ...` all along;
    the py one handed over a 26-line traceback. One rendering, not two."""
    source = tmp_path / "tprog.rvl"
    source.write_text(TEST_PROGRAM, encoding="utf-8")
    result = _cli("test", str(source))
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined
    assert "[py] fail: emitter refused:" in combined
    assert PY_REFUSAL in combined

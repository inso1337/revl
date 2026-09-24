"""Issue #1393: an emit-time refusal is an ANSWER, so no entry point may hand it
to the user as a Python traceback.

Background. A week of work landed on one premise: a tier that cannot lower a
construct refuses it BY NAME rather than emitting something plausible. PR #1381
made five tiers refuse a `validated` emission they were silently dropping,
#1391/#1382 did the same for a `validated` extern on python, #1355 and #1317
made go refuse a document shape instead of dropping components, and #1384 closed
three refusals `emit_placement` skipped. Each one turns a silent wrong answer
into a named one.

That improvement stops at the emitter boundary if the named answer then arrives
as a stack trace. `EmitError` is a `ValueError` defined inside each backend's
dynamically loaded `emit.py`, so it matched none of the `except (RevlError,
RuntimeError, OSError)` tuples the drivers were written with, and it reached the
terminal raw. It is also the load-bearing half of `revl.diagnostics`, which
exists so an agent can react to a refusal without parsing prose; a traceback is
the one shape that forces prose parsing.

Measured on `main` at 1a44b34c5, with the cordis-py runtime installed:

  * `revl run` (py tier)            -> 26 lines of traceback
  * `revl test` (py tier)           -> 26 lines of traceback
  * `revl run --backend java`       -> 29 lines of traceback
  * `revl run --backend rust`       -> the emitter subprocess's traceback,
                                       relayed verbatim inside "rust emit failed"

and, for contrast, already correct before this file existed:

  * `revl test` on ts/rust/java/go/wasm -> "[<tier>] fail: emitter refused: ..."
  * `revl run --backend go`             -> "error: could not build the go composition: ..."
  * `revl run --backend wasm`           -> one diagnostic
  * `revl compile` / `revl audit`       -> neither one emits, so neither can hit it
  * the MCP verbs                       -> the transport already answers with a
                                           diagnostic record, never a traceback

THE CONTROL MATTERS AS MUCH AS THE FIX. Swallowing a genuine crash into a tidy
sentence would be a worse defect than the one being closed, so every catch added
for this issue names `EmitError` and nothing wider, and each half below is
paired with a test that an internal fault from the same call still escapes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import compile_source  # noqa: E402
from revl import run_java as run_java_mod  # noqa: E402
from revl import test as revl_test  # noqa: E402

import emit as py_emit  # noqa: E402  — the python backend's emitter


HAVE_CORDIS = importlib.util.find_spec("cordis") is not None


# A `validated` extern: refused by name on the py tier (issue #1382), and
# refused on every other tier for the simpler reason that it carries no body
# they can spell. One document exercises both halves.
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

#: The sentence the py tier refuses with. Every rendering below has to carry it.
REFUSAL = "`validated` extern `complete`"


def _ir(source: str = PROGRAM) -> dict:
    return compile_source(source, "agent.rvl")


def _faulting_emitter(exc: BaseException):
    """A stand-in emitter module whose `emit` raises *exc*.

    `EmitError` is carried on the module OBJECT, so a fake that wants to be
    treated as a refusal has to expose the class the caller will catch. These
    fakes deliberately raise something else: they are the control.
    """
    fake = types.ModuleType("emit")
    fake.EmitError = py_emit.EmitError

    def emit(ir, *args, **kwargs):
        raise exc

    fake.emit = emit
    return fake


# --------------------------------------------------------- the refusal itself

def test_the_py_emitter_still_refuses_this_document_by_name():
    """The anchor. Everything below is about how this sentence is DELIVERED, so
    it is worth nothing if the sentence stops being raised."""
    with pytest.raises(py_emit.EmitError, match=REFUSAL):
        py_emit.emit(_ir())


# ------------------------------------------------------------- `revl test`

def test_revl_test_reports_a_py_emit_refusal_as_a_tier_failure():
    """The py runner now answers the way its five siblings already did.

    `revl.test.run_py` is reached through the module rather than through
    `RUNNERS[...]` on purpose: the issue-#266 audit in
    tests/test_env_gated_skips_run_somewhere.py exists so a runner that can
    answer "skip, toolchain absent" is required in some CI job. This call cannot
    skip — the emitter refuses before the cordis preflight is reached — so there
    is no green-by-omission for that audit to catch here, and the file does not
    need a provisioned tier to be meaningful.
    """
    outcome, message = revl_test.run_py(_ir(TEST_PROGRAM))
    assert outcome == "fail"
    assert message.startswith("emitter refused: ")
    assert REFUSAL in message
    assert "Traceback" not in message


def test_revl_test_still_lets_an_internal_emitter_fault_escape(monkeypatch):
    """THE CONTROL. The catch names `EmitError`; a bug inside the emitter is not
    a refusal and must not be dressed as one.

    A bare `ValueError` is the sharp case, not an arbitrary exception:
    `EmitError` IS a `ValueError`, so the lazy version of this fix — catching
    `ValueError` — would pass every other test in this file and quietly swallow
    half the emitter's real faults. This one fails against that version.
    """
    monkeypatch.setattr(
        revl_test, "_emitter",
        lambda backend: _faulting_emitter(ValueError("internal emitter fault")))
    with pytest.raises(ValueError, match="internal emitter fault"):
        revl_test.run_py(_ir(TEST_PROGRAM))


# ------------------------------------------------- `revl run --backend java`

def _java_without_a_jdk(monkeypatch):
    """Reach the java driver's emit step without a JDK on the machine.

    The emitter runs before javac is ever invoked, so the refusal path is fully
    exercised with a bin directory that does not exist. A test that needed a
    real JDK would skip on most machines, which for a diagnostic-shape test is
    the same as not having written it.
    """
    monkeypatch.setattr(run_java_mod, "java_runtime_reason", lambda: None)
    monkeypatch.setattr(run_java_mod, "_working_jdk_bin", lambda: "/nonexistent/bin")


def test_run_java_reports_an_emit_refusal_instead_of_a_traceback(monkeypatch, capsys):
    _java_without_a_jdk(monkeypatch)
    code = run_java_mod.run_java(_ir(), {}, ["agent.rvl"], once=True)
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("error: could not build the java composition:")
    assert "no @java body" in err
    assert "Traceback" not in err


def test_run_java_still_lets_an_internal_emitter_fault_escape(monkeypatch):
    """THE CONTROL, java half — again a bare `ValueError`, for the same reason.

    `RuntimeError` would NOT do here: this driver's guard has caught
    `(RevlError, RuntimeError, OSError)` since long before this issue, so a
    `RuntimeError` proves nothing about what was added. A `ValueError` is caught
    only if somebody widened the tuple to the class `EmitError` inherits from.
    """
    _java_without_a_jdk(monkeypatch)
    monkeypatch.setattr(
        run_java_mod, "_java_emitter",
        lambda: _faulting_emitter(ValueError("internal emitter fault")))
    with pytest.raises(ValueError, match="internal emitter fault"):
        run_java_mod.run_java(_ir(), {}, ["agent.rvl"], once=True)


# ------------------------------------------------------- the emitter CLIs
#
# `revl run --backend rust`, `revl run --backend ts` and `revl run --placement`
# reach those two emitters by running `backends/<tier>/emit.py` as a SUBPROCESS
# and relaying its stderr verbatim (src/revl/placement.py). An uncaught
# `EmitError` there is a traceback in the operator's terminal two processes
# away from the code that raised it.

def _emit_cli(tier: str, ir_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "backends" / tier / "emit.py"), str(ir_file)],
        capture_output=True, text=True, check=False)


@pytest.mark.parametrize("tier,body", [("rust", "@rs"), ("typescript", "@ts")])
def test_the_emitter_cli_prints_a_refusal_not_a_traceback(tmp_path, tier, body):
    ir_file = tmp_path / "ir.json"
    ir_file.write_text(json.dumps(_ir()), encoding="utf-8")
    done = _emit_cli(tier, ir_file)
    assert done.returncode == 1
    assert done.stdout == "", "a refused emission must write no source"
    assert "Traceback" not in done.stderr
    assert done.stderr.startswith("error: ")
    assert f"no {body} body" in done.stderr


@pytest.mark.parametrize("tier", ["rust", "typescript"])
def test_the_emitter_cli_still_tracebacks_on_an_internal_fault(tmp_path, tier):
    """THE CONTROL, CLI half. A malformed IR is not a refusal: nothing in the
    emitter decided anything about it, so it stays a crash with a stack."""
    ir_file = tmp_path / "ir.json"
    ir_file.write_text(json.dumps({
        "ir_version": 3,
        "components": [{"name": "X", "provides": {}}],
        "services": {"S": {"methods": 5}},
    }), encoding="utf-8")
    done = _emit_cli(tier, ir_file)
    assert done.returncode != 0
    assert "Traceback" in done.stderr


# ------------------------------------------------------------- `revl run`
#
# The headline of the issue. These two need the cordis-py runtime, because the
# py driver imports it before it emits; CI's `frontend-cordis` job is where they
# execute. Everything above runs on a bare interpreter.

def _run_cli(probe: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, env=env, check=False)


@pytest.mark.skipif(not HAVE_CORDIS, reason="the py driver imports cordis before it emits")
def test_revl_run_renders_a_py_emit_refusal_with_its_lifecycle_stage(tmp_path):
    """Green after. The diagnostic's own first line is byte-identical to what the
    emitter raised, and the item-461 stage rides underneath it, which is exactly
    how `revl run` renders every other refusal it already caught."""
    source = tmp_path / "agent.rvl"
    source.write_text(PROGRAM, encoding="utf-8")
    done = _run_cli(
        f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        f"from revl.__main__ import main\n"
        f"raise SystemExit(main(['run', {str(source)!r}, '--once']))\n")
    assert done.returncode == 1
    assert "Traceback" not in done.stderr
    assert done.stderr.startswith(f"error: {REFUSAL}")
    assert "(lifecycle stage: boot " in done.stderr


@pytest.mark.skipif(not HAVE_CORDIS, reason="the py driver imports cordis before it emits")
def test_revl_run_still_surfaces_an_unexpected_emitter_fault_as_a_traceback(tmp_path):
    """THE CONTROL, and the one this issue is most at risk of getting wrong.

    The boundary catch is `except emit.EmitError`. The stand-in emitter raises a
    bare `ValueError`, which `EmitError` inherits from: catching `ValueError`
    instead of the class would look identical on every green test here and would
    silently eat half the emitter's real faults. It must still take the run down
    loudly, with a stack that points at the fault — an internal failure dressed
    as a named refusal would be a worse defect than the traceback this removed.
    """
    source = tmp_path / "agent.rvl"
    source.write_text(PROGRAM, encoding="utf-8")
    done = _run_cli(
        f"import sys, types; sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        f"fake = types.ModuleType('emit')\n"
        f"class EmitError(ValueError): pass\n"
        f"fake.EmitError = EmitError\n"
        f"def emit(ir, *a, **k): raise ValueError('internal emitter fault')\n"
        f"fake.emit = emit\n"
        f"sys.modules['emit'] = fake\n"
        f"from revl.__main__ import main\n"
        f"raise SystemExit(main(['run', {str(source)!r}, '--once']))\n")
    assert done.returncode != 0
    assert "Traceback" in done.stderr
    assert "ValueError: internal emitter fault" in done.stderr

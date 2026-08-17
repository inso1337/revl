"""A composition that rewrites itself (demo/self_evolve.py, as a test).

The claim under test is not that the model is good — it is that the model
cannot decide. A machine-written component enters the running system only if
the compiler admits it against what is already loaded, and a refused
candidate leaves the composition serving its last good generation.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
for path in (str(ROOT / "src"), str(DEMO)):
    if path not in sys.path:
        sys.path.insert(0, path)

pytest.importorskip("cordis", reason="the self-evolution loop needs the cordis-py runtime")

import evolve_bridge  # noqa: E402
from revl import compile_files  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

SOURCE = DEMO / "components" / "evolve.rvl"


@pytest.fixture
def system():
    session = Session()
    evolve_bridge.SESSION = session
    evolve_bridge.reset()
    session.load(compile_files([str(SOURCE)]))
    yield session
    if session.loaded:
        session.unload()
    evolve_bridge.reset()


def _greet(session: Session) -> str:
    return session.call("greeter", "greet", ["ada"])["result"]


def _apply_pending(session: Session) -> bool:
    if not evolve_bridge.PENDING:
        return False
    candidate = evolve_bridge.PENDING.pop()
    session.swap(compile_files(
        ["<candidate>.rvl"],
        sources={evolve_bridge._abs("<candidate>.rvl"): candidate}))
    return True


def test_the_composition_boots_with_its_own_compiler_and_model(system):
    state = system.state()
    assert state["loadOrder"] == ["HostRuntime", "ModelAssistant", "GreeterV1", "Evolver"]
    assert state["providedKeys"] == ["assistant", "compiler", "evolve", "greeter"]
    assert _greet(system) == "hello, ada"


def test_a_running_system_rewrites_one_of_its_own_components(system):
    verdict = system.call("evolve", "once", ["improve the greeter"])["result"]
    assert verdict == "admitted"
    assert _apply_pending(system) is True
    assert _greet(system) == "hello, ada! (v2)"


def test_a_candidate_that_does_not_compile_is_refused_and_never_queued(system):
    system.call("evolve", "once", ["improve the greeter"])
    _apply_pending(system)

    verdict = system.call("evolve", "once", ["broken idea"])["result"]
    assert verdict.startswith("refused:")
    assert "expects `Str`, got `Int`" in verdict
    assert evolve_bridge.PENDING == []          # never reached the queue
    assert _greet(system) == "hello, ada! (v2)"  # last good generation still serves


def test_the_evolver_must_declare_that_it_changes_the_world():
    """G4's upper bound applied to the evolver itself: `once` reaches two
    emissions, so the interface cannot describe it as harmless."""
    ir = compile_files([str(SOURCE)])
    assert ir["services"]["Evolve"]["methods"]["once"]["emission"] is True


def test_the_audit_names_the_model_and_the_compiler_as_host_code():
    """What the evolver can reach is enumerable before it runs (G8)."""
    ir = compile_files([str(SOURCE)])
    externs = {e["name"]: e["class"] for e in ir["externs"]}
    assert externs["host_complete"] == "emission"   # the model
    assert externs["host_propose"] == "emission"    # the admission gate
    assert externs["host_check"] == "pure"          # asking does not change anything


def test_the_whole_self_modifying_system_unloads_without_residue(system):
    system.call("evolve", "once", ["improve the greeter"])
    _apply_pending(system)
    report = system.unload()
    assert report["noResidue"] is True

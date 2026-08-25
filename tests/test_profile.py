"""Capability/emission profiling (roadmap item 124, docs/revl-profile.md).

`revl profile` diffs a component's *declared* emission surface (the static G8
walk `revl audit` runs) against what a recorded run *actually* emitted (the v2
`emit` events), and flags over-declaration — a declared emission the run never
exercised.

`compute_profile` is pure over two inputs, so it is pinned here without a
runtime: the declared side is a composition document (an ``audit --json``-shaped
`boundary` doc for the unit tests, and a real compiled IR / ``.rvl`` source for
the reuse test), and the used side is a hand-built trace whose `emit` events are
constructed with `why_runtime.make_emit_event` — the same builder the run driver
uses, so the fixtures cannot drift from the real record shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import profile as p  # noqa: E402
from revl import why_runtime as wr  # noqa: E402


# --------------------------------------------------------------------------
# synthetic inputs
# --------------------------------------------------------------------------


def _boundary_doc(surface: dict) -> dict:
    """A minimal composition document in the shape `revl audit --json` emits:
    only a `boundary` map. `compute_profile` reads declarations straight from
    it, so a unit test needs no compiler.

    `surface` is ``{component: {label: [scope...]}}``; each label becomes both
    an `emissions` entry and a `capabilities` entry, exactly as the audit walk
    produces them."""
    boundary = {}
    for comp, labels in surface.items():
        boundary[comp] = {
            "emissions": sorted(labels),
            "capabilities": {lbl: list(scopes) for lbl, scopes in labels.items()},
        }
    return {"boundary": boundary}


def _emit(seq: int, component: str, service: str, key: str) -> dict:
    return wr.make_emit_event(seq, 1, component, service, key,
                              wr.cause_trigger("crossed by step-back"))


def _write_trace(tmp_path: Path, events: list[dict]) -> str:
    path = tmp_path / "run.jsonl"
    wr.write_trace(events, str(path))
    return str(path)


# --------------------------------------------------------------------------
# over-declaration: declared two, used one
# --------------------------------------------------------------------------


def test_over_declared_capability_is_flagged():
    """Writer's code can emit through both `bus` and `db`; the run only crossed
    `db`. `bus.publish` (capability `bus`) is over-declared — declared minus
    used, at both grains."""
    ir = _boundary_doc({
        "Writer": {"bus.publish": ["bus"], "db.execute": ["db"]},
    })
    events = [_emit(1, "Writer", "Db", "db.execute")]

    profile = p.compute_profile(ir, events)
    writer = profile["components"]["Writer"]

    assert writer["declared"]["keys"] == ["bus.publish", "db.execute"]
    assert writer["used"]["keys"] == ["db.execute"]
    assert writer["overDeclared"]["keys"] == ["bus.publish"]
    assert writer["overDeclared"]["capabilities"] == ["bus"]
    assert writer["underDeclared"]["keys"] == []

    summary = profile["summary"]
    assert summary["overDeclaredKeys"] == 1
    assert summary["overDeclaredCapabilities"] == 1
    assert summary["overDeclaredComponents"] == 1
    assert summary["clean"] is False


def test_fully_used_composition_has_empty_over_declared_set():
    """A run that crosses every declared emission leaves nothing over-declared —
    the profile is clean."""
    ir = _boundary_doc({
        "Writer": {"bus.publish": ["bus"], "db.execute": ["db"]},
    })
    events = [
        _emit(1, "Writer", "Db", "db.execute"),
        _emit(2, "Writer", "Bus", "bus.publish"),
    ]

    profile = p.compute_profile(ir, events)
    writer = profile["components"]["Writer"]

    assert writer["overDeclared"]["keys"] == []
    assert writer["overDeclared"]["capabilities"] == []
    assert profile["summary"]["overDeclaredKeys"] == 0
    assert profile["summary"]["clean"] is True


def test_empty_trace_makes_the_whole_surface_over_declared():
    """A trace with no `emit` events used nothing — every declared emission
    reads as over-declared. The honest answer for a run that crossed no
    boundary, not an error."""
    ir = _boundary_doc({"Writer": {"bus.publish": ["bus"], "db.execute": ["db"]}})

    profile = p.compute_profile(ir, [])
    writer = profile["components"]["Writer"]

    assert writer["used"]["keys"] == []
    assert writer["overDeclared"]["keys"] == ["bus.publish", "db.execute"]
    assert profile["summary"]["overDeclaredKeys"] == 2


# --------------------------------------------------------------------------
# capability vs key grain: two methods through one required key
# --------------------------------------------------------------------------


def test_capability_grain_collapses_labels_to_required_key():
    """`db.read` and `db.write` are two labels on one required key `db`. Using
    only `db.read` leaves `db.write` over-declared *at the key grain*, but the
    capability `db` was exercised, so it is NOT over-declared at the capability
    grain — the least-privilege unit is coarser than the emission label."""
    ir = _boundary_doc({
        "Store": {"db.read": ["db"], "db.write": ["db"]},
    })
    events = [_emit(1, "Store", "Db", "db.read")]

    profile = p.compute_profile(ir, events)
    store = profile["components"]["Store"]

    assert store["overDeclared"]["keys"] == ["db.write"]
    assert store["overDeclared"]["capabilities"] == []  # `db` was used
    assert store["used"]["capabilities"] == ["db"]


# --------------------------------------------------------------------------
# anomalies: used-but-not-declared, and an emitter with no surface
# --------------------------------------------------------------------------


def test_under_declaration_is_surfaced_not_merged():
    """An emission the trace records that the surface does not name is an
    anomaly (the checker forbids emitting through an undeclared boundary): it is
    reported under `underDeclared`, never folded into the over-declared count."""
    ir = _boundary_doc({"Writer": {"db.execute": ["db"]}})
    events = [
        _emit(1, "Writer", "Db", "db.execute"),
        _emit(2, "Writer", "Bus", "bus.publish"),  # not in the declared surface
    ]

    profile = p.compute_profile(ir, events)
    writer = profile["components"]["Writer"]

    assert writer["overDeclared"]["keys"] == []
    assert writer["underDeclared"]["keys"] == ["bus.publish"]
    assert profile["summary"]["underDeclaredKeys"] == 1
    assert profile["summary"]["clean"] is False


def test_emitter_with_no_declared_surface_is_an_unknown_component():
    """A component that emits in the trace but declares no emission surface at
    all is flagged as unknown — the composition and the trace are probably not
    the same system. It is never silently dropped."""
    ir = _boundary_doc({"Writer": {"db.execute": ["db"]}})
    events = [
        _emit(1, "Writer", "Db", "db.execute"),
        _emit(2, "Ghost", "Db", "db.execute"),  # Ghost has no declared surface
    ]

    profile = p.compute_profile(ir, events)

    assert profile["unknownComponents"] == ["Ghost"]
    assert "Ghost" in profile["components"]
    assert profile["summary"]["clean"] is False


# --------------------------------------------------------------------------
# reuse of the real audit walk (full compiled IR / .rvl input)
# --------------------------------------------------------------------------


_DEMO = """
service Bus {
  emission[bus] fn publish(topic: Str)
}
service Db {
  emission[db] fn execute(sql: Str)
}

component BusImpl provides bus: Bus {
  let cells = effect Map.new() undo cells.drop()
  provide bus {
    fn publish(topic) {
      effect cells.insert("last", topic)
      undo   cells.remove("last")
    }
  }
}
component DbImpl provides db: Db {
  let cells = effect Map.new() undo cells.drop()
  provide db {
    fn execute(sql) {
      effect cells.insert("last", sql)
      undo   cells.remove("last")
    }
  }
}

component Writer requires bus: Bus, db: Db {
  emit db.execute("insert")
  emit bus.publish("topic")
}
"""


def _compile_demo(tmp_path: Path) -> dict:
    from revl.compiler import compile_files

    src = tmp_path / "demo.rvl"
    src.write_text(_DEMO, encoding="utf-8")
    return compile_files([str(src)])


def test_declared_surface_reuses_the_audit_walk_on_a_full_ir(tmp_path):
    """The declared side comes from the same G8 walk `revl audit` runs — a full
    compiled IR (no precomputed `boundary`) is walked on the spot, and the
    required-key -> service wiring is joined for context."""
    ir = _compile_demo(tmp_path)
    events = [_emit(1, "Writer", "Db", "db.execute")]

    profile = p.compute_profile(ir, events)
    writer = profile["components"]["Writer"]

    assert writer["declared"]["keys"] == ["bus.publish", "db.execute"]
    assert writer["overDeclared"]["keys"] == ["bus.publish"]
    assert writer["overDeclared"]["capabilities"] == ["bus"]
    # the service wiring is available from a full IR (absent from an audit doc)
    assert writer["services"] == {"bus": "Bus", "db": "Db"}
    # the non-emitting provider components never enter the profile
    assert set(profile["components"]) == {"Writer"}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "revl", "profile", *argv],
        cwd=ROOT, capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""})


def test_cli_human_and_json(tmp_path):
    src = tmp_path / "demo.rvl"
    src.write_text(_DEMO, encoding="utf-8")
    trace = _write_trace(tmp_path, [_emit(1, "Writer", "Db", "db.execute")])

    human = _run_cli(str(src), trace)
    assert human.returncode == 0  # descriptive by default — not a gate
    assert "OVER-DECLARED" in human.stdout
    assert "bus.publish" in human.stdout

    machine = _run_cli(str(src), trace, "--json")
    assert machine.returncode == 0
    payload = json.loads(machine.stdout)
    assert payload["components"]["Writer"]["overDeclared"]["keys"] == ["bus.publish"]
    assert payload["summary"]["overDeclaredKeys"] == 1


def test_cli_strict_exits_nonzero_only_on_over_declaration(tmp_path):
    src = tmp_path / "demo.rvl"
    src.write_text(_DEMO, encoding="utf-8")

    partial = _write_trace(tmp_path, [_emit(1, "Writer", "Db", "db.execute")])
    strict_over = _run_cli(str(src), partial, "--strict")
    assert strict_over.returncode == 1

    full = _write_trace(tmp_path, [
        _emit(1, "Writer", "Db", "db.execute"),
        _emit(2, "Writer", "Bus", "bus.publish"),
    ])
    strict_clean = _run_cli(str(src), full, "--strict")
    assert strict_clean.returncode == 0
    # without --strict the same partial run still exits 0
    assert _run_cli(str(src), partial).returncode == 0


def test_cli_missing_trace_reports_cleanly(tmp_path):
    src = tmp_path / "demo.rvl"
    src.write_text(_DEMO, encoding="utf-8")
    result = _run_cli(str(src), str(tmp_path / "nope.jsonl"))
    assert result.returncode == 1
    assert "error" in result.stderr.lower()

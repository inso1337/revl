"""Minimal-capability repair patch on `revl profile` (roadmap item 307).

`revl profile` (item 124) NAMES a component's over-declared emission reach.
`--patch` turns that into an actionable, PROPOSED (never applied) least-authority
suggestion: the narrowed `emission[...]` the author should declare, sound in
three ways: it narrows to exactly the observed reach, it never drops a `*`
(unnameable / first-class-dispatch) reach the trace cannot observe, and it never
touches a component whose composition and trace disagree. The suggestion is
re-run through the admission gate, which is the backstop for observation being
one run (an under-approximation of reach).

`compute_repair_patch` is pure over the item-124 profile document, so it is
pinned here without a runtime, exactly as `test_profile.py` pins `compute_profile`.
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
# synthetic inputs (same shapes as test_profile.py)
# --------------------------------------------------------------------------


def _boundary_doc(surface: dict) -> dict:
    """A minimal `revl audit --json`-shaped composition: only a `boundary` map.
    `surface` is ``{component: {label: [scope...]}}``."""
    boundary = {}
    for comp, labels in surface.items():
        boundary[comp] = {
            "emissions": sorted(labels),
            "capabilities": {lbl: list(scopes) for lbl, scopes in labels.items()},
        }
    return {"boundary": boundary}


def _emit(seq: int, component: str, capability: str, key: str) -> dict:
    return wr.make_emit_event(seq, 1, component, capability, key,
                              wr.cause_trigger("crossed by step-back"))


def _patch(surface: dict, events: list[dict]) -> dict:
    return p.compute_repair_patch(p.compute_profile(_boundary_doc(surface), events))


# --------------------------------------------------------------------------
# 1. an over-declaring component gets the least-authority suggestion, and the
#    narrowed declaration re-admits (the whole point: apply, re-run the gate)
# --------------------------------------------------------------------------


def test_over_declaration_yields_the_minimal_emission_and_readmits():
    surface = {"Agent": {
        "filesystem.read": ["fs"], "filesystem.write": ["fs"],
        "network": ["net"], "shell": ["sh"]}}
    trace = [_emit(1, "Agent", "fs", "filesystem.read")]

    patch = _patch(surface, trace)
    entry = patch["patch"]["Agent"]

    assert entry["minimizable"] is True
    # the exact declaration the author should write, narrowed to the one reach
    assert entry["emission"] == "emission[filesystem.read]"
    assert entry["suggested"]["keys"] == ["filesystem.read"]
    assert entry["removed"]["keys"] == ["filesystem.write", "network", "shell"]
    # capabilities are the required keys (a label's first segment): `filesystem`
    # is still needed (read keeps it), so only network/shell drop
    assert entry["removed"]["capabilities"] == ["network", "shell"]
    # PROPOSED, never applied
    assert patch["applied"] is False
    assert "never applied" in patch["advisory"]
    assert patch["summary"]["minimizable"] == 1

    # STILL ADMITS: adopt the suggestion as the declared surface and re-run the
    # profile against the same trace: clean, no over- or under-declaration
    narrowed = _boundary_doc({"Agent": {"filesystem.read": ["fs"]}})
    reprofiled = p.compute_profile(narrowed, trace)
    assert reprofiled["summary"]["clean"] is True
    assert reprofiled["summary"]["overDeclaredKeys"] == 0
    assert reprofiled["summary"]["underDeclaredKeys"] == 0


# --------------------------------------------------------------------------
# 2. a component that reaches everything it declares gets NO patch
# --------------------------------------------------------------------------


def test_fully_used_component_is_not_minimizable():
    surface = {"Full": {"a.x": ["c"], "b.y": ["d"]}}
    trace = [_emit(1, "Full", "c", "a.x"), _emit(2, "Full", "d", "b.y")]

    patch = _patch(surface, trace)
    entry = patch["patch"]["Full"]

    assert entry["minimizable"] is False
    assert "nothing to narrow" in entry["reason"]
    # the pass-through keeps the declared surface, drops nothing
    assert entry["removed"]["keys"] == []
    assert entry["suggested"]["keys"] == ["a.x", "b.y"]
    assert patch["summary"]["minimizable"] == 0
    assert patch["summary"]["capabilitiesRemoved"] == 0


# --------------------------------------------------------------------------
# 3. a `*` (first-class / unknown-dispatch) reach is NEVER narrowed past
# --------------------------------------------------------------------------


def test_wildcard_reach_survives_every_narrowing():
    # `*` is unnameable, so the trace can never observe it; it must stay declared
    surface = {"Dyn": {"*": ["*"], "db.put": ["db"], "net.get": ["net"]}}
    trace = [_emit(1, "Dyn", "db", "db.put")]  # only db.put observed

    patch = _patch(surface, trace)
    entry = patch["patch"]["Dyn"]

    assert entry["minimizable"] is True
    assert entry["keepsWildcard"] is True
    # `*` is kept, net.get (unused, nameable) is dropped, db.put (used) is kept
    assert "*" in entry["suggested"]["keys"]
    assert entry["suggested"]["keys"] == ["*", "db.put"]
    assert entry["removed"]["keys"] == ["net.get"]
    assert "kept `*`" in entry["note"]


def test_wildcard_kept_even_when_nothing_else_is_observed():
    surface = {"Dyn2": {"*": ["*"], "net.get": ["net"]}}
    patch = _patch(surface, [])  # empty trace: nothing observed at all

    entry = patch["patch"]["Dyn2"]
    # never narrows to less than the unnameable `*` reach
    assert entry["suggested"]["keys"] == ["*"]
    assert entry["emission"] == "emission[*]"
    assert "net.get" in entry["removed"]["keys"]


# --------------------------------------------------------------------------
# conservatism: a composition/trace disagreement is not narrowed
# --------------------------------------------------------------------------


def test_under_declaration_mismatch_is_not_narrowed():
    # Agent emits a label it never declared, so composition and trace disagree,
    # so the surface is the wrong system to narrow
    surface = {"Agent": {"a.x": ["c"]}}
    trace = [_emit(1, "Agent", "z", "z.rogue")]

    patch = _patch(surface, trace)
    entry = patch["patch"]["Agent"]

    assert entry["minimizable"] is False
    assert "disagree" in entry["reason"]
    assert entry["removed"]["keys"] == []


def test_unknown_emitter_is_not_narrowed():
    # Ghost has no declared surface at all but emits (an unknownComponents case)
    surface = {"Known": {"a.x": ["c"], "b.y": ["d"]}}
    trace = [_emit(1, "Ghost", "c", "a.x"), _emit(2, "Known", "c", "a.x")]

    profile = p.compute_profile(_boundary_doc(surface), trace)
    assert "Ghost" in profile["unknownComponents"]
    patch = p.compute_repair_patch(profile)
    # Known over-declares b.y and is narrowable; Ghost is a mismatch, passed through
    assert patch["patch"]["Known"]["minimizable"] is True
    assert patch["patch"]["Known"]["emission"] == "emission[a.x]"
    assert patch["patch"]["Ghost"]["minimizable"] is False


# --------------------------------------------------------------------------
# 4. the machine-readable (JSON) document is well-formed and self-describing
# --------------------------------------------------------------------------


def test_patch_document_shape_is_well_formed():
    surface = {"Agent": {"filesystem.read": ["fs"], "shell": ["sh"]}}
    trace = [_emit(1, "Agent", "fs", "filesystem.read")]
    patch = _patch(surface, trace)

    # round-trips through JSON (an agent harness consumes it verbatim)
    reloaded = json.loads(json.dumps(patch))
    assert reloaded == patch
    assert set(patch) == {"patch", "summary", "applied", "advisory"}
    assert set(patch["summary"]) == {
        "components", "minimizable", "keysRemoved", "capabilitiesRemoved"}
    entry = patch["patch"]["Agent"]
    for field in ("minimizable", "declared", "suggested", "removed", "emission",
                  "keepsWildcard"):
        assert field in entry


# --------------------------------------------------------------------------
# reuse on a full compiled IR (the same demo test_profile.py compiles)
# --------------------------------------------------------------------------


_DEMO = """
service Bus { emission[bus] fn publish(topic: Str) }
service Db { emission[db] fn execute(sql: Str) }
component BusImpl provides bus: Bus {
  let cells = effect Map.new() undo cells.drop()
  provide bus { fn publish(topic) { effect cells.insert("last", topic) undo cells.remove("last") } }
}
component DbImpl provides db: Db {
  let cells = effect Map.new() undo cells.drop()
  provide db { fn execute(sql) { effect cells.insert("last", sql) undo cells.remove("last") } }
}
component Writer requires bus: Bus, db: Db {
  emit db.execute("insert")
  emit bus.publish("topic")
}
"""


def test_patch_on_a_full_compiled_ir(tmp_path):
    from revl.compiler import compile_files

    src = tmp_path / "demo.rvl"
    src.write_text(_DEMO, encoding="utf-8")
    ir = compile_files([str(src)])
    trace = [_emit(1, "Writer", "Db", "db.execute")]  # bus.publish unused

    patch = p.compute_repair_patch(p.compute_profile(ir, trace))
    entry = patch["patch"]["Writer"]
    assert entry["minimizable"] is True
    assert entry["emission"] == "emission[db.execute]"
    assert entry["removed"]["keys"] == ["bus.publish"]
    assert entry["removed"]["capabilities"] == ["bus"]


# --------------------------------------------------------------------------
# 5. CLI: --patch human + JSON, and it stays exit 0 (a suggestion, not a gate)
# --------------------------------------------------------------------------


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "revl", "profile", *argv],
        cwd=ROOT, capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""})


def _write_trace(tmp_path: Path, events: list[dict]) -> str:
    path = tmp_path / "run.jsonl"
    wr.write_trace(events, str(path))
    return str(path)


def test_cli_patch_human_and_json(tmp_path):
    src = tmp_path / "demo.rvl"
    src.write_text(_DEMO, encoding="utf-8")
    trace = _write_trace(tmp_path, [_emit(1, "Writer", "Db", "db.execute")])

    human = _run_cli(str(src), trace, "--patch")
    assert human.returncode == 0
    assert "SUGGEST" in human.stdout
    assert "emission[db.execute]" in human.stdout
    assert "never" in human.stdout.lower()  # proposed-only framing

    machine = _run_cli(str(src), trace, "--patch", "--json")
    assert machine.returncode == 0
    payload = json.loads(machine.stdout)
    assert payload["applied"] is False
    assert payload["patch"]["Writer"]["emission"] == "emission[db.execute]"
    # --patch never gates, even with --strict (it is what clears a strict fail)
    strict = _run_cli(str(src), trace, "--patch", "--strict")
    assert strict.returncode == 0

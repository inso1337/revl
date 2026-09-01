"""`revl scaffold` — a typed, holed skeleton from a spec (docs/scaffold.md).

The generator invents structure, never finished code, so the things that must
stay true are the scaffold-then-fill contract (docs/holes.md §8) and the
conservative-authority thesis applied to codegen:

  1. the skeleton for a representative spec compiles as a draft;
  2. its holes are obligations with the right expected types, each carrying the
     fill spec `revl_check` adds (the reused fillspec path);
  3. it is never admissible while a hole remains;
  4. a hole filled with a correct expression checks, with one fewer obligation;
  5. a capability whose boundary the spec did not inject is NOT turned into an
     `emission[...]` — the operation stays a hole, so a missing authority is a
     tracked obligation, never a silently widened permission.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.scaffold import (  # noqa: E402
    ScaffoldError, build_skeleton, build_spec, scaffold_document)


# The representative spec is the proposal's own flagship command:
#   revl scaffold --service Analysis --requires filesystem \
#     --provides analysis --capabilities filesystem.read
def _flagship():
    return build_spec(service="Analysis", requires=["filesystem"],
                      provides="analysis", capabilities=["filesystem.read"])


# ---- 1. the skeleton compiles as a draft ----------------------------------

def test_the_skeleton_compiles_as_a_draft():
    ir = compile_source(build_skeleton(_flagship()), "csv_analyzer.rvl")
    # a real, linked composition — not just a parse
    assert ir["manifest"]["loadOrder"] == ["AnalysisProvider"]
    assert ir.get("holes")  # and it is a draft


def test_it_provides_the_service_and_requires_the_dependency():
    ir = compile_source(build_skeleton(_flagship()), "csv_analyzer.rvl")
    component = ir["components"][0]
    assert component["name"] == "AnalysisProvider"
    assert component["requires"] == {"filesystem": "Filesystem"}
    assert "Analysis" in ir["services"]


# ---- 2. holes are obligations with the right types + fill specs -------------

def test_every_method_body_and_the_effect_value_are_holes():
    doc = scaffold_document(_flagship(), "csv_analyzer.rvl")
    expected = sorted(o["expected"] for o in doc["obligations"])
    # the effect resource and the provide-method return
    assert expected == ["AnalysisResource", "Str"]


def test_each_obligation_carries_its_fill_spec():
    doc = scaffold_document(_flagship(), "csv_analyzer.rvl")
    by_type = {o["expected"]: o for o in doc["obligations"]}
    # the emission method's fill may cross exactly the injected boundary
    run = by_type["Str"]["fillSpec"]
    assert run["capability"] == {
        "mayEmit": True, "bound": ["filesystem"],
        "reason": "an emission-declared provide-method scoped to filesystem"}
    assert {"name": "input", "type": "Str"} in run["bindings"]
    # the effect-setup hole is a pure position
    setup = by_type["AnalysisResource"]["fillSpec"]
    assert setup["capability"]["mayEmit"] is False


# ---- 3. not admissible while a hole remains --------------------------------

def test_the_scaffold_is_not_admissible_while_holes_remain():
    doc = scaffold_document(_flagship(), "csv_analyzer.rvl")
    assert doc["admissible"] is False
    # and the gate agrees: booting is admission, which refuses a hole
    source = doc["source"]
    running = compile_source("")  # a cold-start composition
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "cand.rvl", manifest=running)
    assert "admission refused" in str(excinfo.value)
    assert "may never enter a running composition" in str(excinfo.value)


# ---- 4. a correct fill checks ----------------------------------------------

def test_a_correctly_filled_hole_checks_with_one_fewer_obligation():
    source = build_skeleton(_flagship())
    # fill the provide-method's Str hole with an in-scope Str binding
    filled = source.replace(
        'fn run(input) = hole[Str] "produce run\'s Str result '
        "(a fill here may emit through the declared boundary)\"",
        "fn run(input) = input")
    assert "hole[Str]" not in filled  # the substitution landed
    ir = compile_source(filled, "csv_analyzer.rvl")
    # the fill type-checks; only the effect-resource obligation is left
    assert [h["type"] for h in ir.get("holes", [])] == ["AnalysisResource"]


# ---- 5. conservative capability: an un-injected boundary stays a hole -------

def test_an_uninjected_capability_is_not_emitted_and_stays_a_hole():
    # network.send is requested, but no `--requires network` injects that
    # boundary. The generator must not widen: no emission bound for it, and the
    # operation that would need it is left as a hole obligation.
    spec = build_spec(service="Analysis", requires=["filesystem"],
                      provides="analysis", capabilities=["network.send"])
    source = build_skeleton(spec)
    assert "emission[" not in source          # nothing was granted at all
    assert "network" not in _emission_bounds(source)
    ir = compile_source(source, "conservative.rvl")
    # the run method stayed a hole rather than emitting an invented boundary
    assert any(h["type"] == "Str" for h in ir.get("holes", []))
    # and the gap is recorded in the obligation prose, not silently dropped
    assert any("network.send" in (h.get("message") or "")
               for h in ir.get("holes", []))


def test_omitting_a_capability_omits_its_emission_bound():
    # the same shape, but with the boundary NOT even requested as a capability:
    # the method scaffolds pure, no emission anywhere.
    spec = build_spec(service="Store", requires=["db"], provides="store")
    source = build_skeleton(spec)
    assert "emission[" not in source
    ir = compile_source(source, "pure.rvl")
    assert ir.get("holes")  # still a draft, just a pure one


def test_an_emission_method_without_a_wired_capability_is_refused():
    # asking for an emitting method with no injected boundary would force a
    # bare `emission` ("any boundary") — the exact widening scaffold avoids.
    with pytest.raises(ScaffoldError) as excinfo:
        build_spec(service="Analysis", provides="analysis",
                   emits=["run(input: Str) -> Str"])
    assert "boundary" in str(excinfo.value)


def test_a_declared_capability_becomes_exactly_that_emission_bound():
    # the positive direction: when the boundary IS injected, the emission bound
    # is exactly it and nothing wider.
    spec = build_spec(service="Analysis", requires=["filesystem"],
                      provides="analysis", capabilities=["filesystem.read"])
    ir = compile_source(build_skeleton(spec), "csv_analyzer.rvl")
    run = ir["services"]["Analysis"]["methods"]["run"]
    assert run["emission"] is True
    assert run["capabilities"] == ["filesystem"]  # the root, never widened


# ---- helpers ---------------------------------------------------------------

def _emission_bounds(source: str) -> str:
    """The text inside every `emission[...]` in the source, for asserting a
    capability is absent from every bound rather than merely absent overall."""
    out = []
    idx = source.find("emission[")
    while idx != -1:
        end = source.find("]", idx)
        out.append(source[idx:end])
        idx = source.find("emission[", end)
    return " ".join(out)


# ---- 6. the CLI verb -------------------------------------------------------

def _cli(*args, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "revl", "scaffold", *args],
        capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
        **kwargs)


def test_cli_writes_the_file_and_reports_obligations(tmp_path):
    out = tmp_path / "csv_analyzer.rvl"
    result = _cli("--service", "Analysis", "--requires", "filesystem",
                  "--provides", "analysis", "--capabilities", "filesystem.read",
                  "--out", str(out))
    assert result.returncode == 0
    text = out.read_text()
    assert "emission[filesystem]" in text
    assert "hole[Str]" in text
    # the open holes are reported on stderr, like a `revl compile` draft
    assert "open hole" in result.stderr
    # and the written file itself compiles as a draft
    ir = compile_source(text, "csv_analyzer.rvl")
    assert ir.get("holes")


def test_cli_json_carries_skeleton_obligations_and_fill_specs():
    result = _cli("--service", "Analysis", "--requires", "filesystem",
                  "--provides", "analysis", "--capabilities", "filesystem.read",
                  "--json")
    assert result.returncode == 0
    doc = json.loads(result.stdout)
    assert doc["admissible"] is False
    assert doc["holeCount"] == 2
    assert "component AnalysisProvider" in doc["source"]
    assert all("fillSpec" in o for o in doc["obligations"])

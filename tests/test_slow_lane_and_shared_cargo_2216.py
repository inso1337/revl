"""Issue #2216: the marked slow lane, and one cargo target directory per session.

The audit (#2190, TEST-10) named two items beyond the caches that #2307 and
#2326 landed:

  * the perf probes that are slow by NATURE move to a marked slow lane the full
    gate runs, instead of riding in every affected run;
  * the cargo-building fixtures share one `CARGO_TARGET_DIR` instead of each
    recompiling the same dependency graph in its own temp directory.

Both are mechanisms, and a mechanism nobody holds can rot silently: a marker
nothing deselects is decoration, and a "shared" fixture the builders stop
passing is a second temp directory. These tests read the artifacts the lanes
actually consult (the pytest config, the workflow, the hook, the gate script,
the fixtures' own source) rather than restating the intent in prose.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import yaml

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"

# The two tests the audit's slow table and fix line name. Pinned by name here
# because "move the perf probes" is only true if THESE are the ones marked.
PROBES = {
    "tests/test_selfhost_lower.py":
        "test_no_nesting_under_the_size_bound_exhausts_the_descent",
    "tests/test_retention_summary_worklist_539.py":
        "test_worklist_not_slower_than_reference_on_lower_rvl",
}


def _slow_lane():
    return load_by_path("revl_tests_slow_lane", ROOT / "tests" / "_slow_lane.py")


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class _Item:
    """What the lane helper reads from a collected pytest item."""

    def __init__(self, nodeid: str, slow: bool):
        self.nodeid = nodeid
        self._slow = slow

    def get_closest_marker(self, name: str):
        return object() if self._slow and name == "slow" else None


# ------------------------------------------------------------- the marker


def test_the_slow_marker_is_registered():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    markers = config["tool"]["pytest"]["ini_options"]["markers"]
    assert any(marker.startswith("slow:") for marker in markers), markers


def test_the_perf_probes_carry_the_marker():
    for rel, name in PROBES.items():
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        names = [_decorator_name(d) for d in node.decorator_list]
        assert "pytest.mark.slow" in names, (rel, name, names)


def test_the_lane_switch_is_what_the_lanes_can_set():
    lane = _slow_lane()
    assert lane.ENV == "REVL_SKIP_SLOW"
    assert lane.MARKER == "slow"
    assert lane.enabled({}) is False
    assert lane.enabled({lane.ENV: "0"}) is False
    assert lane.enabled({lane.ENV: "1"}) is True


def test_the_lane_partitions_only_the_marked_tests():
    lane = _slow_lane()
    fast, slow = (_Item("tests/test_a.py::test_fast", False),
                  _Item("tests/test_a.py::test_slow", True))
    kept, dropped = lane.split([fast, slow])
    assert kept == [fast]
    assert dropped == [slow]


# --------------------------------------------------------- the lane consumers


def _ci_jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def test_the_pull_request_lane_deselects_the_slow_marker():
    """`root-suite-affected` is the ONLY root-suite runner on a pull request,
    so it is the fast lane that has to skip the marked tests there."""
    steps = _ci_jobs()["root-suite-affected"]["steps"]
    step = next(s for s in steps if s.get("name") == "root suite (affected nodes)")
    assert step["env"]["REVL_SKIP_SLOW"] == "1"


def test_the_full_suite_keeps_the_slow_lane():
    """The merge-queue `frontend` matrix runs the whole suite before land. If it
    inherited the switch, the slow lane would run nowhere."""
    frontend = _ci_jobs()["frontend"]
    assert "REVL_SKIP_SLOW" not in str(frontend.get("env", {}))
    step = next(s for s in frontend["steps"] if s.get("run") == "pytest tests/ -q")
    assert "REVL_SKIP_SLOW" not in str(step)
    lanes = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    assert "REVL_SKIP_SLOW" not in str(lanes["frontend-cordis"])


def test_the_local_fast_lane_is_gated_on_affected_mode():
    text = (ROOT / "tools" / "pre_merge.sh").read_text(encoding="utf-8")
    assert ('[ "$AFF" -eq 1 ] && SLOW_ENV="REVL_SKIP_SLOW=1"'
            in text), text
    assert '$SLOW_ENV "$PYTEST"' in text, text


def test_the_pre_commit_hook_is_a_fast_lane():
    text = (ROOT / "tools" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert re.search(r"^export REVL_SKIP_SLOW=1$", text, re.MULTILINE), text


# ------------------------------------------------- the shared cargo directory


def test_the_session_offers_one_cargo_target_directory():
    text = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert re.search(
        r'@pytest\.fixture\(scope="session"\)\n'
        r'def cargo_target_dir\(tmp_path_factory\)', text), text


def test_every_cargo_building_fixture_uses_it():
    admit = (ROOT / "tests" / "test_gate_crate_admit.py").read_text(encoding="utf-8")
    assert 'cargo_target_dir / "debug" / "revl_gate_consumer"' in admit, admit
    assert admit.count('"--target-dir"') >= 2, admit
    example = (ROOT / "tests" / "test_gate_consumer_example_rs.py").read_text(
        encoding="utf-8")
    assert 'cargo_target_dir / "debug" / "agent-prefilter"' in example, example
    assert '"--target-dir"' in example, example
    inprocess = (ROOT / "tests" / "test_inprocess_gate_rust.py").read_text(
        encoding="utf-8")
    assert "target = cargo_target_dir" in inprocess, inprocess
    assert 'target / "release" / "inprocess_gate_rust"' in inprocess, inprocess

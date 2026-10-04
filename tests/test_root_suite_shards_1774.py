"""`REVL_TEST_SHARD` splits a run without dropping or repeating a test (issue #1774).

`root-suite-affected` runs as four shards in CI. A FULL selection (or one of 40
or more files) is split by test file with `REVL_TEST_SHARD=k/4`; a smaller one
runs whole in shard 1. The safety claim is that the shards PARTITION the
collection: every selected test runs in exactly one shard. These tests pin the
assignment itself, the conftest wiring (in pytest subprocesses, as CI runs it)
and the job's shape in ci.yml.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _shard  # noqa: E402

SHARDS = 4


def _root_files():
    return sorted(f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.py"))


# ------------------------------------------------------------ the assignment

def test_every_file_lands_in_exactly_one_shard():
    files = _root_files()
    owner = _shard.assign(files, _shard.load_weights(), SHARDS)
    assert set(owner) == set(files)
    assert set(owner.values()) <= set(range(SHARDS))
    assert len(set(owner.values())) == SHARDS, "a shard was left empty"


def test_the_assignment_does_not_depend_on_collection_order():
    files = _root_files()
    weights = _shard.load_weights()
    shuffled = list(files)
    random.Random(1774).shuffle(shuffled)
    assert _shard.assign(files, weights, SHARDS) == _shard.assign(shuffled, weights, SHARDS)


def test_a_file_without_a_weight_still_gets_a_shard():
    owner = _shard.assign(["tests/test_new_a.py", "tests/test_new_b.py"], {}, SHARDS)
    assert sorted(owner) == ["tests/test_new_a.py", "tests/test_new_b.py"]


def test_the_recorded_weights_balance_the_full_suite():
    """Balance is a cost claim, not a safety one: a stale weight can only make
    the shards uneven. Measured weights keep the heaviest shard within 20% of
    the mean, or, when one unit alone outweighs that (a family is never split;
    before issue #1861 one test of
    tests/test_selfhost_lower.py::test_no_nesting_under_the_size_bound_exhausts_the_descent
    measured about 1120s), within 5% of that unit, with the other shards
    sharing the rest evenly."""
    weights = _shard.load_weights()
    families = _shard.load_families()
    assert len(weights) > 500, "shard_weights.json is missing or nearly empty"
    files = _root_files()
    loads = _shard.loads(files, weights, SHARDS, families)
    mean = sum(loads) / SHARDS
    split = _shard.split_files(files, weights, SHARDS)
    heaviest = max([weights.get(f, 0.0) for f in files if f not in split]
                   + [v for f in split for v in families.get(f, {}).values()])
    assert max(loads) <= max(1.2 * mean, 1.05 * heaviest), [round(x) for x in loads]
    rest = sorted(loads)[:-1]
    assert max(rest) <= 1.2 * (sum(rest) / len(rest)), [round(x) for x in loads]


def test_a_split_file_never_raises_the_heaviest_shard():
    """When the recorded weights split a file, the split must not make the
    heaviest shard worse than packing that file whole. Since issue #1861 no file
    is heavy enough to split (test_selfhost_lower.py measured 114s in CI run
    37231772801, down from 1520s), so on today's weights the two predictions are
    the same; the splitting itself is pinned on synthetic weights below."""
    weights, families = _shard.load_weights(), _shard.load_families()
    files = _root_files()
    whole = max(_shard.loads(files, weights, SHARDS))
    split = max(_shard.loads(files, weights, SHARDS, families))
    assert split <= whole + 1e-6, (round(whole), round(split))


# ------------------------------------------- splitting a heavy file by family

_NODES = (
    [f"tests/test_heavy.py::test_fam_a[{i}]" for i in range(30)]
    + [f"tests/test_heavy.py::test_fam_b[{i}]" for i in range(30)]
    + ["tests/test_heavy.py::test_single"]
    + [f"tests/test_heavy.py::TestKlass::test_m{i}[{j}]" for i in range(3) for j in range(4)]
    + [f"tests/test_light_{i}.py::test_x[{j}]" for i in range(12) for j in range(3)]
)
_HEAVY_WEIGHTS = {"tests/test_heavy.py": 1000.0,
                  **{f"tests/test_light_{i}.py": 20.0 for i in range(12)}}


def test_a_plan_runs_every_test_exactly_once():
    owner = _shard.plan(_NODES, _HEAVY_WEIGHTS, {}, SHARDS)
    assert set(owner) == set(_NODES)
    assert set(owner.values()) <= set(range(SHARDS))
    shards = [{t for t, i in owner.items() if i == k} for k in range(SHARDS)]
    assert set().union(*shards) == set(_NODES)
    assert sum(len(s) for s in shards) == len(_NODES)


def test_a_heavy_file_is_split_but_never_inside_a_family():
    owner = _shard.plan(_NODES, _HEAVY_WEIGHTS, {}, SHARDS)
    heavy = {i for t, i in owner.items() if t.startswith("tests/test_heavy.py")}
    assert len(heavy) > 1, "the heavy file was not split"
    by_family: dict = {}
    for t, i in owner.items():
        by_family.setdefault(_shard.family(t), set()).add(i)
    assert all(len(v) == 1 for v in by_family.values()), "a family was split"
    assert _shard.family("tests/test_heavy.py::TestKlass::test_m1[2]") == \
        "tests/test_heavy.py::TestKlass", "a class is one family"
    light = {}
    for t, i in owner.items():
        if not t.startswith("tests/test_heavy.py"):
            light.setdefault(_shard.file_of(t), set()).add(i)
    assert all(len(v) == 1 for v in light.values()), "a light file was split"


def test_a_plan_does_not_depend_on_collection_order():
    shuffled = list(_NODES)
    random.Random(1774).shuffle(shuffled)
    assert _shard.plan(_NODES, _HEAVY_WEIGHTS, {}, SHARDS) == \
        _shard.plan(shuffled, _HEAVY_WEIGHTS, {}, SHARDS)


def test_recorded_family_seconds_steer_the_split():
    """One family that carries most of the file's time gets a shard of its own."""
    fams = {"tests/test_heavy.py": {"tests/test_heavy.py::test_single": 900.0}}
    _, seconds = _shard.units(_NODES, _HEAVY_WEIGHTS, fams, SHARDS)
    assert seconds["tests/test_heavy.py::test_single"] == 900.0
    # an unrecorded family: the file's weight by its share of collected tests
    assert seconds["tests/test_heavy.py::test_fam_a"] == pytest.approx(1000.0 * 30 / 73)
    owner = _shard.plan(_NODES, _HEAVY_WEIGHTS, fams, SHARDS)
    alone = owner["tests/test_heavy.py::test_single"]
    assert sum(_HEAVY_WEIGHTS.get(_shard.file_of(t), 0) for t, i in owner.items()
               if i == alone and not t.startswith("tests/test_heavy.py")) == 0


def test_a_small_or_unweighted_collection_is_never_split():
    small = {f"tests/test_light_{i}.py": 20.0 for i in range(12)}
    assert _shard.split_files(small, small, SHARDS) == set()
    assert _shard.split_files({"tests/a.py", "tests/b.py"}, {}, SHARDS) == set()
    assert _shard.split_files(set(_HEAVY_WEIGHTS), _HEAVY_WEIGHTS, 1) == set()



def test_a_malformed_spec_is_refused():
    for spec in ("4", "0/4", "5/4", "a/b"):
        with pytest.raises(ValueError):
            _shard.parse(spec)
    assert _shard.parse("2/4") == (2, 4)


# ------------------------------------------- the conftest wiring, end to end

_CASE = '''
import pytest

@pytest.mark.parametrize("n", range({count}))
def test_case(n):
    assert n >= 0
'''


def _collect(tree: Path, shard: str | None, weights: Path | None = None) -> set:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(ROOT / "tests"), str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
    env.pop(_shard.ENV, None)
    env.pop("REVL_SHARD_WEIGHTS", None)
    if shard is not None:
        env[_shard.ENV] = shard
    if weights is not None:
        env["REVL_SHARD_WEIGHTS"] = str(weights)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(tree), "-q", "--collect-only",
         "-p", "no:cacheprovider", "-p", "conftest", "--rootdir", str(tree)],
        cwd=tree, env=env, capture_output=True, text=True, timeout=300,
        check=False)
    assert proc.returncode in (0, 5), proc.stdout + proc.stderr
    return {line.strip() for line in proc.stdout.splitlines() if "::" in line}


def test_the_shards_partition_a_real_collection(tmp_path):
    for i in range(7):
        (tmp_path / f"test_case_{i}.py").write_text(
            _CASE.format(count=i + 1), encoding="utf-8")
    whole = _collect(tmp_path, None)
    assert len(whole) == sum(range(1, 8))
    parts = [_collect(tmp_path, f"{k}/{SHARDS}") for k in range(1, SHARDS + 1)]
    assert set().union(*parts) == whole, "a test ran in no shard"
    assert sum(len(p) for p in parts) == len(whole), "a test ran in two shards"
    for part in parts:
        files = {node.split("::", 1)[0] for node in part}
        for other in parts:
            if other is not part:
                assert not files & {n.split("::", 1)[0] for n in other}, "a file was split"


def test_the_shards_partition_a_collection_with_a_split_file(tmp_path):
    """End to end through conftest: a file the weights mark heavy is split
    across shards by family, and the shards still run each test once."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "test_heavy.py").write_text(
        "import pytest\n" + "".join(
            f"@pytest.mark.parametrize('n', range(3))\ndef test_f{i}(n):\n    pass\n"
            for i in range(8)), encoding="utf-8")
    for i in range(3):
        (tree / f"test_case_{i}.py").write_text(_CASE.format(count=2), encoding="utf-8")
    weights = tmp_path / "weights.json"
    weights.write_text(json.dumps({"seconds": {
        "test_heavy.py": 800.0, **{f"test_case_{i}.py": 10.0 for i in range(3)}}}),
        encoding="utf-8")
    whole = _collect(tree, None)
    parts = [_collect(tree, f"{k}/{SHARDS}", weights) for k in range(1, SHARDS + 1)]
    assert set().union(*parts) == whole, "a test ran in no shard"
    assert sum(len(p) for p in parts) == len(whole), "a test ran in two shards"
    heavy = [p for p in parts if any(t.startswith("test_heavy.py") for t in p)]
    assert len(heavy) > 1, "the heavy file was not split"
    for p in parts:
        for t in p:
            for other in parts:
                if other is not p:
                    assert not {_shard.family(t)} & {_shard.family(o) for o in other}


def test_a_pytest_started_by_a_test_runs_its_whole_collection():
    """conftest takes the setting out of the environment once read, so the
    pytest subprocesses some tests start are never sharded themselves."""
    assert _shard.ENV not in os.environ


# ------------------------------------------ the per-file seconds, for weights

def test_a_sharded_run_prints_the_seconds_of_every_file_it_ran(tmp_path):
    for i in range(3):
        (tmp_path / f"test_case_{i}.py").write_text(
            _CASE.format(count=2), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(ROOT / "tests"), str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
    out = {}
    for spec in (None, "1/1"):
        env.pop(_shard.ENV, None)
        if spec:
            env[_shard.ENV] = spec
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(tmp_path), "-q",
             "-p", "no:cacheprovider", "-p", "conftest", "--rootdir", str(tmp_path)],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
            check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        out[spec] = _shard.parse_seconds(proc.stdout)
    assert out[None] == {}, "an unsharded run printed shard seconds"
    assert sorted(out["1/1"]) == [f"test_case_{i}.py" for i in range(3)]
    assert all(v >= 0 for v in out["1/1"].values())


def test_seconds_read_back_through_a_ci_timestamp_prefix():
    lines = _shard.seconds_lines({"tests/test_b.py": 1.234, "tests/test_a.py": 60.0})
    log = "\n".join(f"2026-10-03T14:05:16.8019169Z {line}" for line in lines)
    assert _shard.parse_seconds(log + "\nunrelated line\n") == {
        "tests/test_a.py": 60.0, "tests/test_b.py": 1.23}


def test_a_refresh_files_family_seconds_under_their_file():
    doc = {"//": "old", "seconds": {"tests/h.py": 5.0},
           "families": {"tests/gone.py": {"tests/gone.py::t": 1.0}}}
    new = _shard.refreshed(doc, {"tests/h.py": 100.0, "tests/h.py::test_a": 60.004,
                                 "tests/h.py::TestK": 40.0}, "note",
                           keep=lambda f: f != "tests/gone.py")
    assert new["seconds"] == {"tests/h.py": 100.0}
    assert new["families"] == {"tests/h.py": {"tests/h.py::TestK": 40.0,
                                              "tests/h.py::test_a": 60.0}}


def test_a_split_file_sums_its_seconds_over_the_shards_it_ran_in():
    sys.path.insert(0, str(ROOT / "tools"))
    import refresh_shard_weights  # noqa: PLC0415
    logs = {"root-suite-affected (1)": "Z REVL_SHARD_SECONDS 30.00 tests/h.py\n"
                                       "Z REVL_SHARD_SECONDS 30.00 tests/h.py::test_a\n",
            "root-suite-affected (2)": "Z REVL_SHARD_SECONDS 12.50 tests/h.py\n"
                                       "Z REVL_SHARD_SECONDS 12.50 tests/h.py::test_b\n"
                                       "Z REVL_SHARD_SECONDS 4.00 tests/x.py\n"}
    assert refresh_shard_weights.measure(logs) == {
        "tests/h.py": 42.5, "tests/h.py::test_a": 30.0, "tests/h.py::test_b": 12.5,
        "tests/x.py": 4.0}


def test_a_refresh_overwrites_measured_files_and_drops_removed_ones():
    doc = {"//": "old", "seconds": {"tests/a.py": 1.0, "tests/b.py": 2.0, "tests/gone.py": 3.0}}
    new = _shard.refreshed(doc, {"tests/b.py": 9.876, "tests/c.py": 0.5}, "note",
                           keep=lambda f: f != "tests/gone.py")
    assert new == {"//": "note", "seconds": {
        "tests/a.py": 1.0, "tests/b.py": 9.88, "tests/c.py": 0.5}}


def test_a_weights_refresh_is_not_a_full_run():
    """A refreshed weights file changes shard balance only, so the affected
    selection for it (and for the tool that writes it) is this file alone."""
    sys.path.insert(0, str(ROOT / "tools"))
    import affected_tests  # noqa: PLC0415
    for changed in ("tests/shard_weights.json", "tools/refresh_shard_weights.py"):
        sel = affected_tests.select([changed], ROOT)
        assert not sel["full"], sel["reason"]
        assert sel["pytest"] == ["tests/test_root_suite_shards_1774.py"], sel
    assert affected_tests.select(["tests/_shard.py"], ROOT)["full"]


# --------------------------------------------------------------- the CI job

def _job():
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml")
                          .read_text(encoding="utf-8"))["jobs"]
    return jobs["root-suite-affected"]


def test_the_ci_job_runs_four_shards_and_shards_a_full_selection():
    job = _job()
    assert job["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    assert job["strategy"]["fail-fast"] is False
    script = "\n".join(step.get("run", "") for step in job["steps"])
    env = {k: v for step in job["steps"] for k, v in (step.get("env") or {}).items()}
    assert env.get("SHARD_TOTAL") == SHARDS
    assert 'REVL_TEST_SHARD="${SHARD_INDEX}/${SHARD_TOTAL}"' in script
    assert re.search(r'SEL_FULL" = "1"', script), "a FULL selection is not sharded"
    assert "pytest $SEL_PYTEST -q" in script

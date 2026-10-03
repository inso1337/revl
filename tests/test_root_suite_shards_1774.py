"""`REVL_TEST_SHARD` splits a run without dropping or repeating a test (issue #1774).

`root-suite-affected` runs as four shards in CI. A FULL selection (or one of 40
or more files) is split by test file with `REVL_TEST_SHARD=k/4`; a smaller one
runs whole in shard 1. The safety claim is that the shards PARTITION the
collection: every selected test runs in exactly one shard. These tests pin the
assignment itself, the conftest wiring (in pytest subprocesses, as CI runs it)
and the job's shape in ci.yml.
"""

from __future__ import annotations

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
    the mean."""
    weights = _shard.load_weights()
    assert len(weights) > 500, "shard_weights.json is missing or nearly empty"
    loads = _shard.loads(_root_files(), weights, SHARDS)
    mean = sum(loads) / SHARDS
    assert max(loads) <= 1.2 * mean, [round(x) for x in loads]


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


def _collect(tree: Path, shard: str | None) -> set:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(ROOT / "tests"), str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
    env.pop(_shard.ENV, None)
    if shard is not None:
        env[_shard.ENV] = shard
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


def test_a_pytest_started_by_a_test_runs_its_whole_collection():
    """conftest takes the setting out of the environment once read, so the
    pytest subprocesses some tests start are never sharded themselves."""
    assert _shard.ENV not in os.environ


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

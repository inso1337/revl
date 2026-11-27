"""Every self-host source has a pinned census bucket and a known size class (#3098).

`tools/build_gate_crate.py` sets `MAX_SOURCE_BYTES`, the fail-closed stack-safety
bound on a source the gate will look at: a larger source is declined outright
(`frontier-declined`), whatever it contains. A self-host file that grows past it
therefore leaves what the gate examines, and the census record of its case is
rewritten to match, so CI stays green and nothing says the file moved. That is how
`selfhost/emit_go.rvl` went from `refuse-out-of-slice/BAD` to `frontier-declined`
at #3079.

This test makes the move loud in two ways:

* `BUCKETS` names the bucket of every `selfhost/*.rvl` case in
  `docs/census-artifact/cases/selfhost/`. A bucket that moves, a record that is
  missing, and a self-host file that is not in the table are all failures, so
  the table has to be edited, and reviewed, in the PR that moves a file.
* `OVER_THE_LIMIT` names the files that are already larger than the bound. A file
  crossing it, in either direction, fails until the table says so. The set may
  only shrink: splitting a file into `use`d siblings (as `selfhost/emit_go.rvl`
  was) is the way out, and raising `MAX_SOURCE_BYTES` is not.

The bound itself is not touched here: it is read from the generator.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "docs" / "census-artifact" / "cases" / "selfhost"

# The census bucket of each self-host source, as the committed record has it.
# A change here is a change in what the gate says about the file.
BUCKETS = {
    "checker.rvl": ["frontier-declined"],
    # Re-recorded by d49c0af: the frontier scan sees 1035 items at one bracket
    # level (bound 1024), so it declines the file before admission. The file is
    # 138 KB, under MAX_SOURCE_BYTES, so this is not a size-class move.
    "compile.rvl": ["frontier-declined"],
    "emit_go.rvl": ["refuse-out-of-slice/BAD"],
    "emit_go_comp.rvl": ["refuse-out-of-slice/BAD"],
    "emit_go_core.rvl": ["refuse-out-of-slice/BAD"],
    "emit_go_module.rvl": ["refuse-out-of-slice/BAD"],
    "emit_java.rvl": ["refuse-out-of-slice/BAD"],
    "emit_py.rvl": ["frontier-declined"],
    "emit_rust.rvl": ["frontier-declined"],
    "emit_ts.rvl": ["refuse-out-of-slice/BAD"],
    "emit_wasm.rvl": ["refuse-out-of-slice/BAD"],
    "lexer.rvl": ["agree-admit"],
    "lower.rvl": ["frontier-declined"],
    "parser.rvl": ["no-objection-out-of-slice"],
    "types.rvl": ["no-objection-out-of-slice"],
}

# Files larger than `MAX_SOURCE_BYTES` today. emit_rust.rvl and emit_py.rvl are to
# be split the way emit_go.rvl was; lower.rvl is the gate crate's own source.
# Never add a name: split the file.
OVER_THE_LIMIT = {"emit_py.rvl", "emit_rust.rvl", "lower.rvl"}


def _max_source_bytes() -> int:
    spec = importlib.util.spec_from_file_location(
        "gate_crate_generator_for_buckets", ROOT / "tools" / "build_gate_crate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MAX_SOURCE_BYTES


def _sources() -> dict[str, Path]:
    return {p.name: p for p in sorted((ROOT / "selfhost").glob("*.rvl"))}


def test_the_table_covers_exactly_the_selfhost_sources():
    assert set(BUCKETS) == set(_sources()), (
        "BUCKETS has drifted from selfhost/*.rvl: add the bucket of a new file "
        "(`python tools/census_artifact.py --write` records it) or drop a removed one")


def test_every_record_matches_its_pinned_bucket():
    found = {}
    for name in BUCKETS:
        record = CASES / f"{name}.json"
        assert record.is_file(), (
            f"no census record for selfhost/{name}: run "
            f"`python tools/census_artifact.py --write`")
        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["case"] == f"selfhost/{name}"
        found[name] = data["buckets"]
    moved = {n: (BUCKETS[n], found[n]) for n in BUCKETS if found[n] != BUCKETS[n]}
    assert not moved, (
        "the census bucket of a self-host file moved (pinned, recorded): "
        f"{moved}. If the move is intended, say why in the PR and edit BUCKETS; "
        "a file that grew past MAX_SOURCE_BYTES must be split, not re-pinned")


def test_no_records_without_a_source():
    records = {p.name.removesuffix(".json") for p in CASES.glob("*.json")}
    assert records == set(BUCKETS), (
        "census records and selfhost/*.rvl disagree: "
        f"{sorted(records ^ set(BUCKETS))}")


@pytest.mark.parametrize("name", sorted(BUCKETS))
def test_size_class_of_each_source(name):
    limit = _max_source_bytes()
    size = _sources()[name].stat().st_size
    if name in OVER_THE_LIMIT:
        assert size > limit, (
            f"selfhost/{name} is {size} bytes, now within MAX_SOURCE_BYTES "
            f"({limit}): remove it from OVER_THE_LIMIT")
    else:
        assert size <= limit, (
            f"selfhost/{name} is {size} bytes, over MAX_SOURCE_BYTES ({limit}), "
            f"so the gate declines it whatever it contains. Split it into `use`d "
            f"sibling files, as selfhost/emit_go.rvl is; do not raise the bound "
            f"(tools/build_gate_crate.py) or add the file to OVER_THE_LIMIT")


def test_the_bound_is_the_fail_closed_one():
    # The stack-safety bound this test is built around; a change to it is a
    # decision that belongs in tools/build_gate_crate.py's own review.
    assert _max_source_bytes() == 256 * 1024

"""Split a root-suite run into N shards by test file (issue #1774).

`root-suite-affected` fell back to the whole root suite on 30 of 34 recent
pull requests, about 70 minutes each. Most of those fallbacks are sound (a
fixture, the compiler or a compile-reachable module changed), so the selection
is left alone and the run is split instead: `REVL_TEST_SHARD=k/N` keeps only
the test files assigned to shard k.

The assignment is a deterministic greedy bin-pack over the units the run
collected, heaviest first, by the seconds recorded in `shard_weights.json`
(a file with no recorded weight counts as the median). A unit is a test file,
except that a file heavier than half an even shard (and than SPLIT_MIN_SECONDS)
is split into its test families: one family is one test function with all of
its parametrizations, or one test class, so a class-scoped fixture and a
parametrized family always stay in one process. Module and session fixtures
are simply set up once in each shard that runs part of the file.
tests/test_selfhost_lower.py is the file this is for: 2% of its time is its
module fixture and the rest is per-test work (issue #1774).

Every shard runs the same command, collects the same tests and so computes the
same assignment: the N shards partition the collection, each test runs in
exactly one. tests/test_root_suite_shards_1774.py pins that. A stale weight can
only make the shards uneven, never drop a test.
"""

from __future__ import annotations

import json
from pathlib import Path

WEIGHTS = Path(__file__).with_name("shard_weights.json")
ENV = "REVL_TEST_SHARD"
# A sharded run ends by printing one `REVL_SHARD_SECONDS <seconds> <file>` line
# per file it ran, and one `... <file>::<family>` line per family of a split
# file (tests/conftest.py); tools/refresh_shard_weights.py reads them back out
# of the CI job logs into shard_weights.json.
SECONDS_TAG = "REVL_SHARD_SECONDS"


def parse(spec: str) -> tuple[int, int]:
    """`"k/N"` -> (k, N), 1 <= k <= N."""
    try:
        k_text, n_text = spec.split("/", 1)
        k, n = int(k_text), int(n_text)
    except ValueError as exc:
        raise ValueError(f"{ENV} must look like 2/4, not {spec!r}") from exc
    if not 1 <= k <= n:
        raise ValueError(f"{ENV}={spec!r}: the shard must be in 1..{n}")
    return k, n


# A file is split into its test families when it weighs more than this share
# of an even shard, and more than SPLIT_MIN_SECONDS (so a small or unweighted
# collection is never split at all).
SPLIT_SHARE = 0.5
SPLIT_MIN_SECONDS = 120.0


def _load(path) -> dict:
    try:
        return json.loads(Path(path or WEIGHTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_weights(path=None) -> dict:
    """file -> seconds."""
    data = _load(path)
    return {str(k): float(v) for k, v in (data.get("seconds") or {}).items()}


def load_families(path=None) -> dict:
    """file -> {family -> seconds}, for the files recorded as split."""
    data = _load(path)
    return {str(f): {str(k): float(v) for k, v in fams.items()}
            for f, fams in (data.get("families") or {}).items()}


def file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def family(nodeid: str) -> str:
    """`f.py::test_x[1]` -> `f.py::test_x`; `f.py::TestC::test_y[a]` -> `f.py::TestC`.
    Everything a family holds runs in one shard."""
    return "::".join(nodeid.split("[", 1)[0].split("::")[:2])


def _weigher(files, weights: dict):
    known = sorted(weights[f] for f in set(files) if f in weights)
    default = known[len(known) // 2] if known else 1.0
    return lambda f: weights.get(f, default)


def split_files(files, weights: dict, n: int) -> set:
    """The files of this collection that are split into families."""
    files = set(files)
    weight = _weigher(files, weights)
    limit = max(SPLIT_MIN_SECONDS, SPLIT_SHARE * sum(map(weight, files)) / n)
    return {f for f in files if n > 1 and weight(f) > limit}


def _pack(units: dict, n: int) -> dict:
    """unit -> shard index (0-based): heaviest first onto the lightest shard.
    Deterministic in (units, n)."""
    loads = [0.0] * n
    out = {}
    for u in sorted(units, key=lambda u: (-units[u], u)):
        target = min(range(n), key=lambda i: (loads[i], i))
        out[u] = target
        loads[target] += units[u]
    return out


def assign(files, weights: dict, n: int) -> dict:
    """file -> shard index (0-based), every file whole."""
    files = sorted(set(files))
    weight = _weigher(files, weights)
    return _pack({f: weight(f) for f in files}, n)


def units(nodeids, weights: dict, families: dict, n: int) -> tuple:
    """(nodeid -> unit, unit -> seconds). A unit is a file, or one family of a
    split file. A family with no recorded seconds gets its file's weight in
    proportion to its share of the file's collected tests."""
    nodeids = list(nodeids)
    files = {file_of(t) for t in nodeids}
    weight = _weigher(files, weights)
    split = split_files(files, weights, n)
    unit_of = {t: (family(t) if file_of(t) in split else file_of(t)) for t in nodeids}
    per_file = {f: 0 for f in split}
    per_unit: dict = {}
    for t, u in unit_of.items():
        per_unit[u] = per_unit.get(u, 0) + 1
        if file_of(t) in split:
            per_file[file_of(t)] += 1
    seconds = {}
    for u, count in per_unit.items():
        f = file_of(u)
        if f not in split:
            seconds[u] = weight(f)
        else:
            seconds[u] = families.get(f, {}).get(u, weight(f) * count / per_file[f])
    return unit_of, seconds


def plan(nodeids, weights: dict, families: dict, n: int) -> dict:
    """nodeid -> shard index (0-based). Each collected test gets exactly one."""
    unit_of, seconds = units(nodeids, weights, families, n)
    owner = _pack(seconds, n)
    return {t: owner[u] for t, u in unit_of.items()}


def loads(files, weights: dict, n: int, families=None) -> list:
    """The predicted seconds of each shard, without a collection: a split file
    contributes its recorded families as units (or itself, whole, when none
    are recorded)."""
    files = set(files)
    weight = _weigher(files, weights)
    split = split_files(files, weights, n) if families is not None else set()
    seconds = {}
    for f in files:
        fams = (families or {}).get(f) if f in split else None
        if fams:
            seconds.update(fams)
        else:
            seconds[f] = weight(f)
    out = [0.0] * n
    for u, i in _pack(seconds, n).items():
        out[i] += seconds[u]
    return out


def seconds_lines(per_file: dict) -> list:
    """The lines a sharded run prints: setup + call + teardown seconds per file."""
    return [f"{SECONDS_TAG} {per_file[f]:.2f} {f}" for f in sorted(per_file)]


def parse_seconds(text: str) -> dict:
    """file -> seconds from log text holding `seconds_lines` output. A CI log
    prefixes each line with a timestamp, so the tag is searched, not anchored."""
    out = {}
    for line in text.splitlines():
        _, tag, rest = line.partition(SECONDS_TAG + " ")
        if not tag:
            continue
        secs, _, name = rest.strip().partition(" ")
        try:
            out[name.strip()] = float(secs)
        except ValueError:
            continue
    return out


def refreshed(doc: dict, measured: dict, note: str, keep=lambda f: True) -> dict:
    """`doc` with `measured` seconds written over the old ones. A measured key
    holding `::` is a family of a split file and goes under `families`. A file
    `keep` rejects (one that no longer exists) is dropped; one the run did not
    time keeps its old weight and families."""
    seconds = {f: v for f, v in (doc.get("seconds") or {}).items() if keep(f)}
    families = {f: dict(v) for f, v in (doc.get("families") or {}).items() if keep(f)}
    for key, value in measured.items():
        f = file_of(key)
        if not keep(f):
            continue
        if "::" in key:
            families.setdefault(f, {})[key] = round(value, 2)
        else:
            seconds[key] = round(value, 2)
    out = {"//": note, "seconds": dict(sorted(seconds.items()))}
    if families:
        out["families"] = {f: dict(sorted(v.items())) for f, v in sorted(families.items())}
    return out

"""Split a root-suite run into N shards by test file (issue #1774).

`root-suite-affected` fell back to the whole root suite on 30 of 34 recent
pull requests, about 70 minutes each. Most of those fallbacks are sound (a
fixture, the compiler or a compile-reachable module changed), so the selection
is left alone and the run is split instead: `REVL_TEST_SHARD=k/N` keeps only
the test files assigned to shard k.

The assignment is a deterministic greedy bin-pack over the files the run
collected, heaviest first, by the seconds recorded in `shard_weights.json`
(a file with no recorded weight counts as the median). Every shard runs the
same command, collects the same files and so computes the same assignment:
the N shards partition the collection, each test runs in exactly one.
tests/test_root_suite_shards_1774.py pins that. A stale weight can only make
the shards uneven, never drop a test.
"""

from __future__ import annotations

import json
from pathlib import Path

WEIGHTS = Path(__file__).with_name("shard_weights.json")
ENV = "REVL_TEST_SHARD"


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


def load_weights(path: Path = WEIGHTS) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in (data.get("seconds") or {}).items()}


def assign(files, weights: dict, n: int) -> dict:
    """file -> shard index (0-based). Deterministic in (files, weights, n)."""
    files = sorted(set(files))
    known = sorted(weights[f] for f in files if f in weights)
    default = known[len(known) // 2] if known else 1.0

    def weight(f):
        return weights.get(f, default)

    loads = [0.0] * n
    out = {}
    for f in sorted(files, key=lambda f: (-weight(f), f)):
        target = min(range(n), key=lambda i: (loads[i], i))
        out[f] = target
        loads[target] += weight(f)
    return out


def loads(files, weights: dict, n: int) -> list:
    """The weight each shard carries, for reporting balance."""
    out = [0.0] * n
    known = sorted(weights[f] for f in set(files) if f in weights)
    default = known[len(known) // 2] if known else 1.0
    for f, i in assign(files, weights, n).items():
        out[i] += weights.get(f, default)
    return out

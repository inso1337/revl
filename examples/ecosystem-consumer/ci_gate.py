#!/usr/bin/env python3
"""agent-ci-gate — a THIRD-PARTY CI gate over `revl.gate` (roadmap item 338).

This file is not part of revl. It is what an external project looks like
once it does `pip install revl` and depends on the gate as a library: its
own `pyproject.toml` (in this directory) declares `revl` as a dependency,
and the only revl import anywhere in this project is the one below.

READ THIS BEFORE YOU COPY IT: THE ASYMMETRIC SECURITY CONTRACT
================================================================
A REFUSAL from `revl.gate.admit` is authoritative and fail-closed: if it
refuses a candidate, the reference compiler refuses it too, and this gate
must never run that candidate. That is the dependable half.

An ADMISSION is NOT a runtime safety guarantee. `admitted = True` means the
candidate's source type-checks, has no open holes, and its effects are
classified as of `gate_version()` — a COMPILE-TIME judgment, scoped to
`gate_version()['frontier']`. It does not mean the candidate is confined as
it runs: an admitted component's host body is exactly the code the gate
surfaced, not code the gate has neutered. "revl admitted it" is therefore
never read here as "safe to run unwitnessed" — this gate only decides
whether a candidate is REGISTERED for a human/operator to wire up next, and
every registration is logged with the `frontier` that produced it, because
an admission from a different gate tier (a rust or wasm gate on a narrower
frontier, once those ship) is not the same fact. Revertible execution of an
admitted candidate is a separate, py-only, explicitly-adopted layer
(`revl.gate.Gate`) that this project does not use — see README.md.

What this project does, concretely
-----------------------------------
1. Imports ONLY `revl.gate` (`admit`, `gate_version`) — nothing else under
   `revl.*` is depended on; that is the promised, versioned surface.
2. Walks a directory of agent-authored `.rvl` candidates and admits each one
   in-process, logging `gate_version()` once per run and, per candidate,
   `{admitted, code}` plus the verbatim `message` on a refusal.
3. Keys its verdict cache on the FULL `gate_version()` triple
   (`api`, `language`, `frontier`), so a language or frontier bump on the
   next revl release invalidates every stale cached verdict instead of
   silently trusting it (docs/design/338-revl-as-dependency.md, "Language
   skew").
4. Gates the "register" decision on `admitted` alone, and never registers
   (or runs) a refused candidate.

Usage:
  python ci_gate.py candidates/           # admit every *.rvl in a directory
  python ci_gate.py candidates/ --json     # machine-readable summary on stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The one revl import this project makes. Everything else under `revl.*` is
# importable (a wheel cannot hide its modules) but UNPROMISED — reaching past
# `revl.gate` is this project's own risk to take, not revl's contract
# (docs/gate-dependency-contract.md).
from revl.gate import admit, gate_version


def cache_key(version: dict) -> tuple:
    """The full `gate_version()` triple, as a hashable cache key. All three
    fields, never just `language`: two gates at the same language but
    different `frontier` (a py reference-full gate and a future rust
    self-host-frontier crate) can disagree on the SAME source, so a cached
    verdict is only valid for the exact (api, language, frontier) that
    produced it."""
    return (version["api"], version["language"], version["frontier"])


class VerdictCache:
    """An in-memory stand-in for what a real CI system or registry would
    persist. Every entry is stored alongside the `gate_version()` triple that
    produced it (not just the boolean verdict), so a stale entry is provably
    stale rather than silently trusted across a revl upgrade."""

    def __init__(self) -> None:
        self._store: dict[tuple, dict[str, dict]] = {}

    def get(self, version: dict, name: str) -> dict | None:
        return self._store.get(cache_key(version), {}).get(name)

    def put(self, version: dict, name: str, record: dict) -> None:
        self._store.setdefault(cache_key(version), {})[name] = record

    def __len__(self) -> int:
        return sum(len(bucket) for bucket in self._store.values())


def admit_candidate(path: Path, cache: VerdictCache, version: dict) -> dict:
    """Admit one candidate, consulting/populating the cache. The returned
    record always carries `frontier` — the field a consumer MUST record with
    any admission it caches or transmits (docs/design/338-revl-as-dependency.md
    §2), so a later reader of the cache alone (not this process) can still
    tell which gate produced the verdict."""
    cached = cache.get(version, path.name)
    if cached is not None:
        return dict(cached, cached=True)

    source = path.read_text(encoding="utf-8")
    verdict = admit(source)
    record: dict = {
        "admitted": verdict.admitted,
        "code": verdict.code,
        "frontier": version["frontier"],
        "cached": False,
    }
    if not verdict.admitted:
        # `message` is logged for a human to read (a repair signal), never
        # parsed — it is the reference compiler's diagnostic verbatim and is
        # NOT part of the versioned API (docs/design/338 §2).
        record["message"] = verdict.message
    cache.put(version, path.name, record)
    return record


def run_gate(candidates_dir: Path, cache: VerdictCache, *,
             log=print) -> list[dict]:
    """Admit every `*.rvl` file in `candidates_dir`, gate the "register"
    decision on `admitted`, and return one record per candidate. A refused
    candidate is reported and skipped — this function never invokes, loads,
    or otherwise runs a refused candidate's code, and it never treats an
    admitted candidate as safe to run unwitnessed (see the module docstring):
    "registered" here means nothing more than "eligible for a human/operator
    to wire up next", which is as far as a bare layer-1 `admit` can honestly
    take a decision."""
    version = gate_version()
    log(f"gate_version: api={version['api']} language={version['language']} "
        f"frontier={version['frontier']}")

    results = []
    for path in sorted(candidates_dir.glob("*.rvl")):
        record = admit_candidate(path, cache, version)
        results.append({"name": path.name, **record})
        if record["admitted"]:
            log(f"REGISTER {path.name}  (frontier={record['frontier']}, "
                f"cached={record['cached']})")
            # A real consumer's next step goes here: hand the candidate to an
            # operator-reviewed deploy path, or (py-only, a separate adoption)
            # `revl.gate.Gate.propose` for a revertible hot-swap. This example
            # stops at "eligible to register" on purpose — going further would
            # blur exactly the line the security contract draws.
        else:
            log(f"REFUSE   {path.name}  code={record['code']} — "
                f"{record.get('message')}")
            # Clause 1: a refusal is authoritative. Nothing about this
            # candidate runs, now or later, in this process.
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("candidates", type=Path,
                        help="directory of *.rvl candidate sources to admit")
    parser.add_argument("--json", action="store_true",
                        help="print a machine-readable summary instead of "
                             "the human log")
    args = parser.parse_args(argv)

    cache = VerdictCache()
    log = (lambda *_a, **_k: None) if args.json else print
    results = run_gate(args.candidates, cache, log=log)

    if args.json:
        print(json.dumps({"gate_version": gate_version(), "results": results}))

    # Exit status is always 0 on a completed run: admitting a batch that
    # contains a refusal is the expected, successful shape of a CI pass over
    # a mixed batch of agent proposals — the refusal was reported, never
    # swallowed. A caller that wants a hard fail on any refusal inspects
    # `results` itself (each record's `admitted` field).
    return 0


if __name__ == "__main__":
    sys.exit(main())

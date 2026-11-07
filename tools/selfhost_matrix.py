#!/usr/bin/env python3
"""Whether each reference emitter can emit the self-host sources (issue #2899).

    python3 tools/selfhost_matrix.py --measure   # re-measure, write the ledger
    python3 tools/selfhost_matrix.py --check     # the cheap staleness and acknowledgement gate
    python3 tools/selfhost_matrix.py --json      # the committed ledger

`selfhost/*.rvl` is the largest revl program in the tree (about 82,000 lines), so
whether a backend's `emit` can compile it is the closest thing the repository
has to a measurement of whether the backend works on a real program. The
per-tier support table in `docs/conformance.md` used to record only the `revl`
gate's agreement over the rejection corpus, which repeats one shared number on
all six rows; this ledger records the question that actually separates the
tiers.

WHAT A CELL IS
--------------
One (self-host file, tier) pair. The reference emitter of that tier is handed
the file's reference IR (the same IR the conformance matrix emits), so a
non-`ok` cell names the byte emitter, not the frontend. A deliberate
`EmitError` is `refused`: the emitter rejected the file on purpose and its
message names the construct, but a deliberate refusal is not automatically a
documented TIER LIMIT (issue #1342 makes the same distinction), so this ledger
publishes it as `refused` and requires a tracking decision below. Any other
exception is `gap`, an unhandled crash. A file the reference frontend itself
cannot compile is `frontend` on every tier: the measurement did not run, which
is `unmeasured` rather than a fact about a backend.

THE MEASUREMENT IS SLOW, SO IT IS A LEDGER
------------------------------------------
Compiling the self-host once per file and emitting it once per tier costs
minutes on a loaded machine, which is too much for a pre-commit gate and for
the test that regenerates the support table. So `--measure` writes the measured
cells to `tools/selfhost_compile_matrix.json` and `--check` is cheap:

  * the ledger's `digest` covers every self-host source, every stdlib source
    the self-host imports, and every tier emitter, so a change to any of them
    marks the ledger stale by construction and the fix is to measure again, not
    to edit a cell;
  * every non-`ok` cell must be named in `tools/selfhost_compile_gaps.json`
    with an issue number or an out-of-scope reason, and an acknowledgement whose
    cell now emits is stale too. This is the same ratchet as
    `tools/tier_guarantees.py::ACKNOWLEDGED`: the gap list can only move
    deliberately, and a gap cannot exist without somewhere to track it.

The digest deliberately does not cover `src/revl/`: a frontend change does not
change what an emitter does with a given IR, and the tiers' own suites cover
the frontend. A change that DOES move this table through the frontend is caught
by the next `--measure`.

WHAT --CHECK CANNOT SEE. The digest covers the measurement's INPUTS, not its
cells, so a hand-edited ledger whose digest still matches is indistinguishable
from a measured one, and the frontend exclusion above is the one fail-silent
path. Coverage and acknowledgement are gated; truth is not, and a cell that
looks wrong is settled by running `--measure` again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "tools" / "selfhost_compile_matrix.json"
GAPS = ROOT / "tools" / "selfhost_compile_gaps.json"
SCHEMA = 1

OK = "ok"
REFUSED = "refused"
GAP = "gap"
FRONTEND = "frontend"


def tiers() -> tuple[str, ...]:
    import conformance  # noqa: PLC0415, resolved next to this file
    return tuple(conformance.TIERS)


def sources(root: Path | None = None) -> list[Path]:
    return sorted((root or ROOT).glob("selfhost/*.rvl"))


def digest(root: Path | None = None) -> str:
    """A fingerprint of everything a measurement reads: corpus and emitters."""
    base = root or ROOT
    hasher = hashlib.sha256()
    paths = (sources(base)
             + sorted(base.glob("stdlib/*.rvl"))
             + sorted(base.glob("backends/*/emit.py")))
    for path in paths:
        hasher.update(path.relative_to(base).as_posix().encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _first_line(exc: BaseException) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def measure() -> dict:
    """Compile every self-host file once, then hand its IR to every tier."""
    import conformance  # noqa: PLC0415
    from revl import compile_files  # noqa: PLC0415

    names = [path.name for path in sources()]
    cells: dict[str, dict[str, dict]] = {name: {} for name in names}
    irs: dict[str, object] = {}
    for path, name in zip(sources(), names):
        try:
            irs[name] = compile_files([str(path.resolve())])
        except Exception as exc:  # noqa: BLE001, a frontend refusal is the datum
            for tier in tiers():
                cells[name][tier] = {"kind": FRONTEND, "message": _first_line(exc)}
            print(f"frontend: {name}: {_first_line(exc)}", file=sys.stderr)

    for name in names:
        if name not in irs:
            continue
        for tier in tiers():
            emitter = conformance.emitter(tier)
            try:
                code = emitter.emit(irs[name])
                cells[name][tier] = {"kind": OK, "bytes": len(code)}
            except Exception as exc:  # noqa: BLE001, any refusal is the datum
                deliberate = isinstance(exc, getattr(emitter, "EmitError", ()))
                cells[name][tier] = {
                    "kind": REFUSED if deliberate else GAP,
                    "message": _first_line(exc),
                }
            print(f"{tier}: {name}: {cells[name][tier]['kind']}", file=sys.stderr)
    return {
        "schema": SCHEMA,
        "digest": digest(),
        "tiers": list(tiers()),
        "files": names,
        "cells": cells,
    }


def load(ledger: Path = LEDGER) -> dict | None:
    try:
        return json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_gaps(path: Path = GAPS) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("gaps", []) if isinstance(data, dict) else data


def cells_of(data: dict) -> dict[str, str]:
    """(file, tier) -> kind, for every measured cell."""
    return {f"{name}::{tier}": cell["kind"]
            for name, row in data.get("cells", {}).items()
            for tier, cell in row.items()}


def tier_counts(data: dict | None, tier: str) -> tuple[int, int] | None:
    """(emitting files, files) for one tier, or None when it was not measured."""
    if not data or tier not in data.get("tiers", []):
        return None
    kinds = [row.get(tier, {}).get("kind") for row in data["cells"].values()]
    if not kinds or any(kind is None or kind == FRONTEND for kind in kinds):
        return None
    return sum(1 for kind in kinds if kind == OK), len(kinds)


def problems(data: dict | None = None, gaps: list[dict] | None = None,
             expected_digest: str | None = None) -> list[str]:
    """Every reason the committed ledger may not be published as it stands."""
    data = load() if data is None else data
    gaps = load_gaps() if gaps is None else gaps
    if data is None:
        return [f"{LEDGER.name} is missing or unreadable; run "
                "`python3 tools/selfhost_matrix.py --measure`"]
    found = []
    expected = digest() if expected_digest is None else expected_digest
    if data.get("digest") != expected:
        found.append(
            f"{LEDGER.name} was measured against different self-host, stdlib or "
            "tier emitter sources; run `python3 tools/selfhost_matrix.py --measure`")
    measured = cells_of(data)
    acknowledged: dict[str, dict] = {}
    for entry in gaps:
        cell = str(entry.get("cell", ""))
        if not cell:
            found.append("an acknowledgement has no cell")
        elif cell in acknowledged:
            found.append(f"{cell} is acknowledged twice")
        else:
            acknowledged[cell] = entry
    for cell in sorted(set(measured) - set(acknowledged)):
        # FRONTEND is not a fact about a tier: the measurement did not run, so
        # there is nothing to file against that emitter.
        if measured[cell] not in (OK, FRONTEND):
            found.append(
                f"{cell} is {measured[cell]} with no acknowledgement in "
                f"{GAPS.name}; file an issue or record it out of scope")
    for cell in sorted(set(acknowledged) - set(measured)):
        found.append(f"{cell} is acknowledged but not measured")
    for cell, entry in sorted(acknowledged.items()):
        if measured.get(cell) in (OK, FRONTEND):
            found.append(f"{cell} needs no acknowledgement now, so its entry is stale")
        issue = entry.get("issue")
        out_of_scope = str(entry.get("out_of_scope", "")).strip()
        if not isinstance(issue, int) and not out_of_scope:
            found.append(f"{cell} needs an integer `issue` or an `out_of_scope` reason")
        if not str(entry.get("reason", "")).strip():
            found.append(f"{cell} needs a `reason` naming the construct it refuses")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--measure", action="store_true",
                        help="re-measure every self-host file against every tier "
                             "and write the ledger (slow: minutes)")
    parser.add_argument("--check", action="store_true",
                        help="fail if the ledger is stale, a non-emitting cell is "
                             "unacknowledged, or an acknowledgement is stale")
    parser.add_argument("--json", action="store_true", help="print the ledger")
    args = parser.parse_args(argv)

    if args.measure:
        data = measure()
        LEDGER.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {LEDGER.relative_to(ROOT)}", file=sys.stderr)
        found = problems(data)
        for problem in found:
            print(problem, file=sys.stderr)
        return 1 if found else 0
    if args.check:
        found = problems()
        for problem in found:
            print(problem, file=sys.stderr)
        if found:
            return 1
        print(f"{LEDGER.name} is current and every gap is acknowledged.")
        return 0
    data = load()
    if data is None:
        print(f"{LEDGER.name} is missing", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

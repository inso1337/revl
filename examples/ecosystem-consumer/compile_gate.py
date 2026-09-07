#!/usr/bin/env python3
"""compile-gate — a THIRD-PARTY build step over `revl.gate.compile_to`
(roadmap item 338, the programmatic-compile half of the dependency surface).

This is the sibling of `ci_gate.py`. `ci_gate.py` demonstrates the ADMIT
(verdict-only) half of the promised `revl.gate` surface; this file demonstrates
the COMPILE half: an external tool that does `pip install revl`, imports ONLY
`revl.gate`, and programmatically compiles a batch of agent-authored candidates
to real target source with `compile_to`, gating whether it writes/emits each
one on the SAME verdict `ci_gate.py` gates its "register" decision on.

READ THIS BEFORE YOU COPY IT: THE ASYMMETRIC SECURITY CONTRACT
================================================================
`compile_to(source, tier)` returns an `Emit`: a `verdict` plus, on admission,
the emitted target `output` source (docs/gate-dependency-contract.md).

* A REFUSAL is authoritative and fail-closed. When `emit.verdict.admitted` is
  False, `emit.output` is None and NOTHING is emitted for that candidate — the
  reference compiler refuses it too, so there is no target source to write.
  This gate never fabricates output for a refused candidate.
* An ADMISSION plus emitted `output` is a COMPILE-TIME judgment scoped to
  `gate_version()['frontier']`, NOT a runtime safety guarantee. Emitting target
  source for an admitted candidate does not mean the emitted program is safe to
  RUN unwitnessed: an admitted component's host body is exactly the code the
  gate surfaced, not code the gate neutered. This example stops at "wrote the
  emitted target source to an output directory for a human/operator to review
  and wire up next"; it never runs what it emits. Revertible execution of an
  admitted candidate is a separate, py-only, explicitly-adopted layer
  (`revl.gate.Gate`) this project does not use (see README.md).
* An UNKNOWN tier is a control verdict (`code = "UNKNOWN_TIER"`), fail closed:
  a bad `--tier` can never be mistaken for an emission.

Every emitted-or-refused record is logged with the `frontier` that produced it,
because an emission from a different gate tier (a rust or wasm gate on a
narrower frontier, once those ship) is not the same fact.

What this project does, concretely
-----------------------------------
1. Imports ONLY `revl.gate` (`compile_to`, `gate_version`) — nothing else
   under `revl.*` is depended on; that is the promised, versioned surface.
2. Walks a directory of agent-authored `.rvl` candidates and compiles each one
   in-process to the chosen `--tier`, logging `gate_version()` once per run
   and, per candidate, `{admitted, code, frontier}` plus the verbatim `message`
   on a refusal.
3. Gates the "emit" decision on `emit.verdict.admitted` alone, writing target
   source ONLY for admitted candidates and never for a refused one.

Usage:
  python compile_gate.py candidates/ --tier py            # emit to stdout log
  python compile_gate.py candidates/ --tier py --out dist/ # write target files
  python compile_gate.py candidates/ --tier py --json      # machine-readable
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
from revl.gate import compile_to, gate_version

# The reference backend tiers `compile_to` covers, mapped to the file extension
# this example writes emitted source under. Purely a convenience for `--out`;
# the set of admitted tiers is `revl.gate`'s to define, and an unknown one is
# refused by `compile_to` itself (fail closed), never silently accepted here.
_TIER_EXT = {
    "py": ".py", "python": ".py",
    "ts": ".ts", "typescript": ".ts",
    "rust": ".rs", "java": ".java", "wasm": ".wat", "go": ".go",
}


def compile_candidate(path: Path, tier: str, version: dict) -> dict:
    """Compile one candidate to `tier`. The returned record always carries
    `frontier` — the field a consumer MUST record with any emission it keeps or
    transmits (docs/design/338-revl-as-dependency.md §2), so a later reader can
    still tell which gate's frontier produced this target source. `output` is
    the emitted source on admission, else None (fail closed on a refusal)."""
    source = path.read_text(encoding="utf-8")
    emit = compile_to(source, tier)
    record: dict = {
        "admitted": emit.verdict.admitted,
        "code": emit.verdict.code,
        "frontier": version["frontier"],
        "output": emit.output,
    }
    if not emit.verdict.admitted:
        # `message` is logged for a human to read (a repair signal), never
        # parsed — it is the reference compiler's diagnostic verbatim and is
        # NOT part of the versioned API (docs/design/338 §2).
        record["message"] = emit.verdict.message
    return record


def _write_output(out_dir: Path, name: str, tier: str, output: str) -> Path:
    """Write emitted target source for an admitted candidate. Only ever called
    on admission (`output is not None`); a refused candidate reaches no writer."""
    ext = _TIER_EXT.get(tier, ".out")
    dest = out_dir / (Path(name).stem + ext)
    dest.write_text(output, encoding="utf-8")
    return dest


def run_compile(candidates_dir: Path, tier: str, *, out_dir: Path | None = None,
                log=print) -> list[dict]:
    """Compile every `*.rvl` file in `candidates_dir` to `tier`, gate the "emit"
    decision on `admitted`, and return one record per candidate. A refused
    candidate is reported and skipped — this function never emits, writes, or
    runs target source for a refusal, and it never treats an admitted
    candidate's emitted source as safe to run unwitnessed (see the module
    docstring)."""
    version = gate_version()
    log(f"gate_version: api={version['api']} language={version['language']} "
        f"frontier={version['frontier']}")
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for path in sorted(candidates_dir.glob("*.rvl")):
        record = compile_candidate(path, tier, version)
        entry = {"name": path.name, **record}
        results.append(entry)
        if record["admitted"]:
            nbytes = len(record["output"] or "")
            written = ""
            if out_dir is not None:
                dest = _write_output(out_dir, path.name, tier, record["output"])
                written = f" -> {dest.name}"
            log(f"EMIT   {path.name}  ({tier}, {nbytes} bytes, "
                f"frontier={record['frontier']}){written}")
        else:
            log(f"REFUSE {path.name}  code={record['code']} — "
                f"{record.get('message')}")
            # A refusal is authoritative: no target source exists for this
            # candidate, and this process emits, writes, and runs nothing.
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("candidates", type=Path,
                        help="directory of *.rvl candidate sources to compile")
    parser.add_argument("--tier", default="py",
                        help="reference backend to emit (py, ts, rust, java, "
                             "wasm, go); default py")
    parser.add_argument("--out", type=Path, default=None,
                        help="directory to write emitted target source into "
                             "(admitted candidates only); omit to log only")
    parser.add_argument("--json", action="store_true",
                        help="print a machine-readable summary instead of "
                             "the human log")
    args = parser.parse_args(argv)

    log = (lambda *_a, **_k: None) if args.json else print
    results = run_compile(args.candidates, args.tier, out_dir=args.out, log=log)

    if args.json:
        # `output` can be large; the JSON summary reports its length, not its
        # bytes, so a caller keys on the verdict and reads files from --out.
        summary = [
            {k: (len(v or "") if k == "output" else v) for k, v in r.items()}
            for r in results
        ]
        print(json.dumps({"gate_version": gate_version(), "tier": args.tier,
                          "results": summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""In-process admission gate for an agent framework (roadmap item 333, Slice 1).

An agent tool-generation loop embeds revl's admission gate as a NATIVE function
and admits every component it proposes IN ITS OWN PROCESS, before that component
can run, with NO `revl mcp serve` subprocess, NO IPC, and NO wire between the
loop and the compiler. This is that embed made concrete: a standalone program in
the agent-framework role that holds a compiled base composition in memory,
admits a BATCH of proposed candidates in-process via `revl.gate.admit` (a
standalone candidate) and `revl.gate.admit_into` (a candidate against the held
running manifest), proves each in-process verdict equals the reference admission
verdict, and measures the per-candidate round-trip as a distribution.

The load-bearing invariant (design docs/design/333-inprocess-gate.md): the
in-process verdict MUST equal the reference admission verdict. On py this is
DEFINITIONAL, not reimplemented: `revl.gate.admit` IS `compile_source` followed
by `refuse_admission`, the same two calls `revl run` and the MCP `revl_admit`
verb make. This harness proves the end-to-end identity by comparing each
in-process verdict against a reference ADMISSION oracle run as a FRESH
subprocess, which also catches any drift from process-global state (A2).

THE ORACLE IS THE ADMISSION PATH, NOT `revl compile` (design A1, CRITICAL).
`revl compile` is a CHECK verb: it does NOT call `refuse_admission`, so it
accepts a draft-with-holes at exit 0. The in-process `admit` correctly REFUSES
that draft. Oracling against `revl compile` would therefore either report a
spurious mismatch on every hole draft, or tempt an implementer to "fix" it by
stripping `refuse_admission` from `admit`, which reintroduces the exact
false-admit the admission gate exists to close (`admit` would then ADMIT what
the reference refuses to run, a security regression). So the oracle here is a
subprocess that performs the IDENTICAL two-call admission sequence, constructed
from the IDENTICAL inputs (source, and for `admit_into` the same base source it
recompiles into the same manifest). The batch INCLUDES a hole-draft candidate
precisely to prove this: the admission oracle refuses it (matching in-process),
while a naive `revl compile` oracle would accept it - a disagreement this
harness reports, so wiring the wrong verb fails loudly instead of silently.

THE HONEST BOUNDARY (design section 4): this gate admits at COMPILE time. A
component it REFUSES never runs in the embedder's process, and a component it
ADMITS carries the reference's compile-time guarantees (types, no open holes,
classified effects, resolved requires/provides). It does NOT sandbox the
admitted code as it runs: `admitted` != `safe to run unwitnessed`. The
reversible-run half is item 334. An embedder who reads "in-process gate" as
"in-process sandbox" ships an unsafe host.

Usage:
  python bench/inprocess_gate_harness.py            # run, print, verify
  python bench/inprocess_gate_harness.py --write     # refresh results/*.md
  python bench/inprocess_gate_harness.py --iters 500 # more cost samples

`correctness_batch()`, `base_manifest()`, `check_matches()`,
`order_independence()` and `measure_cost()` are imported by
`tests/test_inprocess_gate.py`, so the guard test exercises this same code.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(BENCH))

import admission_latency as al  # noqa: E402 - reuse the item-16 scenario + stats

from revl import compile_source  # noqa: E402
from revl.gate import admit, admit_into, gate_version  # noqa: E402


# --------------------------------------------------------------------------- #
# The base composition the agent holds in memory across generations.
# --------------------------------------------------------------------------- #
#
# Reused verbatim from bench/admission_latency.py (item 16): a `Store` provider
# `Kv` and a consumer `App`. A candidate that `requires store: Store` resolves
# it against this ambient provider, never redeclaring it.
RUNNING = al.RUNNING


def base_manifest() -> dict:
    """Compile the base composition once (an agent keeps this in memory across
    generations; re-deriving it per candidate would not reflect the real loop)."""
    return compile_source(RUNNING, "base.rvl")


# --------------------------------------------------------------------------- #
# The curated correctness batch: a mix that exercises BOTH verdict directions,
# both entry points, and - the A1 probe - at least one hole draft.
# --------------------------------------------------------------------------- #

class Candidate(NamedTuple):
    name: str
    kind: str          # "admit" (standalone) or "admit_into" (against manifest)
    source: str
    note: str
    is_hole: bool = False


# A standalone-valid twin of the item-16 candidate (Store inlined), admitted via
# the layer-1 `admit` with no manifest.
_STANDALONE_TWIN = al.CANDIDATE_STANDALONE

# A candidate that REQUIRES the running Store but calls a method Store does not
# declare. It refuses ONLY because the manifest is consulted (store resolves to
# the running Store): a genuine against-the-running-composition refusal.
_CALLS_MISSING_METHOD = """
service Cache { fn lookup(key: Str) -> Str }
component CacheMiss requires store: Store provides cache: Cache {
  provide cache { fn lookup(key) = store.nonexistent(key) }
}
"""

# A candidate that provides a service but omits one of its declared methods.
_INCOMPLETE_PROVIDE = """
service Two { fn a() -> Str  fn b() -> Str }
component Half provides two: Two { provide two { fn a() = "x" } }
"""

# A candidate that redeclares the running `Store` service with a different shape:
# refused because it differs from the running manifest - an admit_into-specific
# refusal that a manifest-less compile could not produce.
_REDECLARE_RUNNING = """
service Store { fn totally_different() -> Int }
component Rogue requires store: Store provides s2: Store {
  provide s2 { fn totally_different() = 1 }
}
"""

# A candidate with a genuine syntax error: the frontend refuses it.
_SYNTAX_ERROR = "component X provides { fn = }\n"

# THE A1 HOLE-DRAFT PROBE. It COMPILES (a draft is checkable) but carries an
# open typed hole, so the admission gate (`refuse_admission`, T3) refuses to let
# it run. `revl compile` accepts it at exit 0; `revl.gate.admit` refuses it. Its
# presence in the batch proves the oracle is the admission verb, not `compile`.
_HOLE_DRAFT = (
    'service Lookup { fn lookup(key: Str) -> Str }\n'
    'component Drafty provides lk: Lookup {\n'
    '  provide lk { fn lookup(key) = hole "look it up in the store" }\n'
    '}\n'
)


def correctness_batch() -> list[Candidate]:
    """A mix that must ADMIT (2), must REJECT (6), spans both entry points, and
    includes two hole drafts (standalone + against the manifest) as the A1
    oracle-correctness probe. Verdict truth is decided by the reference oracle,
    not by these labels; the `note` is documentation for the report."""
    return [
        Candidate("cache_layer", "admit_into", al.CANDIDATE,
                  "requires the running Store, provides a new Cache -> ADMIT"),
        Candidate("standalone_twin", "admit", _STANDALONE_TWIN,
                  "standalone-valid, Store inlined -> ADMIT"),
        Candidate("calls_missing_method", "admit_into", _CALLS_MISSING_METHOD,
                  "calls a method the running Store lacks -> REJECT (manifest)"),
        Candidate("incomplete_provide", "admit_into", _INCOMPLETE_PROVIDE,
                  "provides a service but omits a declared method -> REJECT"),
        Candidate("redeclare_running_service", "admit_into", _REDECLARE_RUNNING,
                  "redeclares running Store with a different shape -> REJECT"),
        Candidate("syntax_error", "admit", _SYNTAX_ERROR,
                  "a genuine syntax error -> REJECT"),
        Candidate("hole_draft", "admit", _HOLE_DRAFT,
                  "A1 probe: draft with an open hole -> REJECT (T3)", True),
        Candidate("hole_draft_into", "admit_into", _HOLE_DRAFT,
                  "A1 probe against the manifest -> REJECT (T3)", True),
    ]


# --------------------------------------------------------------------------- #
# The in-process gate (what the agent framework actually embeds and calls).
# --------------------------------------------------------------------------- #

def inprocess_verdict(cand: Candidate, manifest: dict) -> tuple[bool, str | None]:
    """The in-process embed: one native call, no subprocess, no IPC. Returns
    the API-stable pair `(admitted, code)`."""
    if cand.kind == "admit_into":
        v = admit_into(cand.source, manifest)
    else:
        v = admit(cand.source)
    return (v.admitted, v.code)


# --------------------------------------------------------------------------- #
# The reference ADMISSION oracle, run as a FRESH subprocess (design section 3).
# Constructed from the IDENTICAL inputs the in-process call gets, so the two
# ask the IDENTICAL question. A fresh process also catches process-global drift.
# --------------------------------------------------------------------------- #

# The oracle shim performs the SAME two-call admission sequence `revl.gate.admit`
# / `admit_into` make. For an `admit_into` candidate it recompiles the SAME base
# source into the SAME manifest, so the question is identical to the in-process
# one. This is the admission path - NOT `revl compile`.
_ORACLE_SHIM = (
    "import sys, json\n"
    "from revl import compile_source\n"
    "from revl.gate import admit, admit_into\n"
    "req = json.load(sys.stdin)\n"
    "if req['kind'] == 'admit_into':\n"
    "    manifest = compile_source(req['base'], 'base.rvl')\n"
    "    v = admit_into(req['source'], manifest)\n"
    "else:\n"
    "    v = admit(req['source'])\n"
    "sys.stdout.write(json.dumps({'admitted': v.admitted, 'code': v.code}))\n"
)


def _subprocess_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def oracle_verdict(cand: Candidate) -> tuple[bool, str | None]:
    """The reference admission verdict for `cand`, from a fresh subprocess that
    runs the SAME admission engine on the SAME inputs. Returns `(admitted, code)`."""
    req = {"kind": cand.kind, "source": cand.source, "base": RUNNING}
    proc = subprocess.run(
        [sys.executable, "-c", _ORACLE_SHIM],
        input=json.dumps(req), capture_output=True, text=True,
        cwd=str(ROOT), env=_subprocess_env(), timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"admission oracle subprocess failed for {cand.name}:\n{proc.stderr}")
    out = json.loads(proc.stdout)
    return (out["admitted"], out["code"])


def naive_compile_accepts(source: str) -> bool:
    """The A1 NEGATIVE CONTROL: what a naive `revl compile` oracle would say.
    `revl compile` exits 0 on a draft-with-holes (it skips `refuse_admission`),
    so this returns True for a candidate the admission gate REFUSES. The match
    test asserts this DISAGREES with the admission verdict on the hole draft,
    proving the harness is NOT wired to `revl compile`."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "cand.rvl"
        f.write_text(source)
        proc = subprocess.run(
            [sys.executable, "-m", "revl", "compile", str(f)],
            capture_output=True, text=True, cwd=str(ROOT),
            env=_subprocess_env(), timeout=120,
        )
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# The match check + order-independence (A2).
# --------------------------------------------------------------------------- #

class MatchRecord(NamedTuple):
    name: str
    kind: str
    inproc: tuple[bool, str | None]
    oracle: tuple[bool, str | None]
    match: bool
    note: str
    is_hole: bool


def check_matches(batch: list[Candidate], manifest: dict) -> list[MatchRecord]:
    """For each candidate, compare the in-process `(admitted, code)` against the
    fresh-subprocess admission oracle's. `(admitted, code)` is the API-stable
    verdict; `message` carries the reference diagnostic verbatim (and cwd-
    dependent paths) so it is not part of the equality."""
    records = []
    for cand in batch:
        inproc = inprocess_verdict(cand, manifest)
        oracle = oracle_verdict(cand)
        records.append(MatchRecord(
            cand.name, cand.kind, inproc, oracle, inproc == oracle,
            cand.note, cand.is_hole))
    return records


def order_independence(batch: list[Candidate], manifest: dict,
                       seed: int = 1729) -> tuple[bool, dict]:
    """A2: admit the batch in FIXED order and in a SHUFFLED order in the SAME
    process, against the SAME held manifest, and assert every candidate's
    verdict is identical across orderings. Order-independence is the property
    that proves statelessness - if any per-process cache (or a mutated manifest)
    made admit N depend on admit N-1, a candidate's verdict would differ between
    the two orderings."""
    fixed = {c.name: inprocess_verdict(c, manifest) for c in batch}
    shuffled_order = list(batch)
    random.Random(seed).shuffle(shuffled_order)
    shuffled = {c.name: inprocess_verdict(c, manifest) for c in shuffled_order}
    mismatches = {n: (fixed[n], shuffled[n]) for n in fixed
                  if fixed[n] != shuffled[n]}
    return (not mismatches, {"fixed": fixed, "shuffled": shuffled,
                             "mismatches": mismatches})


# --------------------------------------------------------------------------- #
# The cost measurement: a DISTRIBUTION over candidate and manifest sizes, not a
# single reasserted headline (design section "The cost measurement", A5).
# --------------------------------------------------------------------------- #

def make_candidate_source(n_methods: int, name: str = "Sized") -> str:
    """A candidate that requires the running Store and provides a service with
    `n_methods` methods - a knob on candidate source size (parse/check/lower
    dominate the round-trip and scale with it)."""
    decls = "\n  ".join(f"fn m{i}(key: Str) -> Str" for i in range(n_methods))
    impls = "\n    ".join(f"fn m{i}(key) = store.get(key)" for i in range(n_methods))
    return (f"service Svc{name} {{\n  {decls}\n}}\n"
            f"component {name} requires store: Store provides c: Svc{name} {{\n"
            f"  provide c {{\n    {impls}\n  }}\n}}\n")


def make_manifest(n_extra: int) -> dict:
    """The base composition plus `n_extra` independent provider components - a
    knob on running-manifest size (the G2/G3 link walks running + candidate)."""
    parts = [RUNNING]
    for i in range(n_extra):
        parts.append(
            f"service Extra{i} {{ fn e{i}() -> Str }}\n"
            f"component ExtraC{i} provides x{i}: Extra{i} {{\n"
            f'  provide x{i} {{ fn e{i}() = "ok" }}\n}}\n')
    return compile_source("\n".join(parts), "base.rvl")


# (candidate methods, manifest extra components) cells spanning small -> large.
_CANDIDATE_SIZES = (3, 12, 48)
_MANIFEST_SIZES = (0, 20)
_CELL_LABELS = {3: "small", 12: "medium", 48: "large"}


def measure_cost(iters: int = 2000, warmup: int = al.WARMUP) -> dict:
    """Time the in-process `admit_into` round-trip across the size-spanning
    cells, plus the item-16 representative scenario for comparability. Returns
    a dict of per-cell distribution stats. The manifest for each cell is
    compiled ONCE outside the timer (an agent holds it across generations)."""
    cells = []
    for extra in _MANIFEST_SIZES:
        manifest = make_manifest(extra)
        for methods in _CANDIDATE_SIZES:
            source = make_candidate_source(methods)
            fn = (lambda s=source, m=manifest: admit_into(s, m))
            fn()  # correctness smoke + warm the first call
            for _ in range(warmup):
                fn()
            samples = al._time_ms(fn, iters)
            cells.append({
                "candidate": _CELL_LABELS[methods],
                "candidate_methods": methods,
                "manifest_components": len(manifest["manifest"]["components"]),
                "stats": al._stats(samples),
            })

    # The item-16 representative scenario, timed identically for comparison.
    rep_manifest = base_manifest()
    rep = (lambda: admit_into(al.CANDIDATE, rep_manifest))
    rep()
    for _ in range(warmup):
        rep()
    representative = al._stats(al._time_ms(rep, iters))
    return {"cells": cells, "representative": representative, "iters": iters}


# --------------------------------------------------------------------------- #
# Reporting.
# --------------------------------------------------------------------------- #

def render_md(cost: dict, records: list[MatchRecord], order_ok: bool,
              naive_hole_accepts: bool) -> str:
    version = gate_version()
    rep = cost["representative"]

    cell_rows = "\n".join(
        f"| {c['candidate']} ({c['candidate_methods']} methods) "
        f"| {c['manifest_components']} | {c['stats']['median']:.3f} "
        f"| {c['stats']['p90']:.3f} | {c['stats']['p99']:.3f} "
        f"| {c['stats']['n']} |"
        for c in cost["cells"])

    match_rows = "\n".join(
        f"| `{r.name}` | {r.kind} | {_fmt(r.inproc)} | {_fmt(r.oracle)} "
        f"| {'yes' if r.match else '**NO**'} |"
        for r in records)

    n_admit = sum(1 for r in records if r.inproc[0])
    n_reject = sum(1 for r in records if not r.inproc[0])
    all_match = all(r.match for r in records)
    medians = [c["stats"]["median"] for c in cost["cells"]]

    return f"""# In-process admission gate (item 333, Slice 1, py)

An agent tool-generation loop embeds revl's admission gate as a native function
and admits every component it proposes IN ITS OWN PROCESS - no `revl mcp serve`,
no IPC, no wire. The verdict the loop gets in-process is the SAME verdict the
reference `revl` admission path gives; that identity is the whole product claim
and the whole security claim. This file records the proof and the cost, produced
by `bench/inprocess_gate_harness.py`.

Gate surface: `api={version['api']}`, `language={version['language']}`,
`frontier={version['frontier']}` (logged so a drift caused by a version skew is
attributable, design A4).

## Verdict identity: in-process == reference admission oracle

Each candidate is admitted in-process (`revl.gate.admit` / `admit_into`) and,
independently, by the reference ADMISSION oracle run as a FRESH subprocess
constructed from the IDENTICAL inputs. The oracle is the admission path
(`compile_source` + `refuse_admission`), **not** `revl compile` - see below. The
API-stable verdict `(admitted, code)` must match for every candidate.

| candidate | entry | in-process | oracle | match |
|---|---|---|---|---|
{match_rows}

{n_admit} admitted, {n_reject} refused; **every in-process verdict {'matches' if all_match else 'DID NOT MATCH'} the
reference admission oracle**. Verdicts are order-independent: admitting the batch
in a fixed order and in a shuffled order in the same process yields identical
per-candidate verdicts ({'holds' if order_ok else 'FAILED'}), which is the property that proves the
layer-1 gate is stateless (design A2).

### The oracle is the admission path, not `revl compile` (design A1, CRITICAL)

`revl compile` is a CHECK verb: it does not call `refuse_admission`, so it
accepts a draft-with-holes at exit 0. The batch includes a hole-draft candidate
(`hole_draft`) that the admission gate REFUSES (code T3) both in-process and via
the oracle. A naive `revl compile` oracle, run on that same draft here, {'ACCEPTS' if naive_hole_accepts else 'rejects'}
it (exit {'0' if naive_hole_accepts else 'non-zero'}) - so oracling against `revl compile` would report a spurious
mismatch, or tempt stripping `refuse_admission` from `admit` and reintroducing
the false-admit the gate exists to close. The hole-draft candidate makes that
wrong wiring fail loudly.

## Cost: a distribution that scales with candidate and manifest size

The per-candidate round-trip is `compile_source(candidate, manifest=running)` +
`refuse_admission`: parse + check/lower the candidate + link it against the
running manifest. No disk I/O, network, or toolchain is in the timed section.
The cost is **not** a single universal constant: parse/check/lower dominate and
scale with candidate source size, and the G2/G3 link scales with running +
candidate declaration count. So it is reported as a distribution over cells
spanning small/medium/large candidates against small/large manifests.

| candidate size | manifest components | median (ms) | p90 (ms) | p99 (ms) | samples |
|---|---|---|---|---|---|
{cell_rows}

**Headline: tenths of a millisecond per candidate on the representative
scenario, scaling with candidate and manifest size.** The representative
scenario (the item-16 candidate/manifest) measured median
**{rep['median']:.3f} ms** (p90 {rep['p90']:.3f} ms, p99 {rep['p99']:.3f} ms,
n={rep['n']}) here, comparable to item 16's committed ~0.165 ms. Across all
cells the median ranged {min(medians):.3f}-{max(medians):.3f} ms; the cell table
shows how a larger model-authored component or a larger held composition raises
the round-trip - roughly with size, no super-linear phase observed.

## Why in-process matters: hop + tokens removed

The alternative to the in-process embed is the MCP bridge (docs/mcp-bridge.md),
where every candidate costs a process hop plus a JSON-RPC request/response whose
schema and result are output tokens the agent pays for on every candidate
(item 50, token economy). The in-process gate removes BOTH the hop and the
tokens: the verdict is a native return value at the cost measured above and zero
tokens. This is a structural difference, stated as such; a head-to-head bridge
latency number is only worth producing against a bridge that is already stood
up, not a strawman invented to lose.

## The honest boundary: this admits at COMPILE time, it does not confine the runtime

A component the gate REFUSES never runs in the embedder's process (the embedder
gates on the verdict before it loads or calls the component). A component the
gate ADMITS carries the reference's compile-time guarantees: it type-checks, has
no open holes, its effects are classified, its requires/provides resolve against
the running composition. The gate does NOT sandbox the admitted code as it runs:
an admitted emission still fires when called, and an `extern` host block is
arbitrary host code the gate SURFACES (G8), not neuters. **`admitted` is not
`safe to run unwitnessed`.** The reversible-run half - embed the gate plus the
witnessed-effect runtime, run the admitted tool under revertible effects, roll
back residue-free on abort - is item 334.

## Re-run

```
python bench/inprocess_gate_harness.py            # run, print, verify
python bench/inprocess_gate_harness.py --write     # rewrite this file
```

A guard test, `tests/test_inprocess_gate.py`, runs a small batch in CI: it
asserts every in-process verdict matches its admission oracle (the hole draft
included), that the naive `revl compile` oracle disagrees on the hole draft
(proving the oracle is the admission verb), that verdicts are order-independent,
and applies only a generous order-of-magnitude latency ceiling (machines vary).

## Methodology

- **Machine:** {al._machine()}
- **Base composition:** `Store` provider `Kv` + consumer `App`, reused from
  `bench/admission_latency.py` (item 16), compiled once outside every timer.
- **Batch:** {len(records)} candidates spanning both verdict directions and both
  entry points (`admit`, `admit_into`), including two hole-draft probes. Defined
  in `bench/inprocess_gate_harness.py::correctness_batch`.
- **Oracle:** a fresh subprocess per candidate running the SAME admission engine
  (`compile_source` + `refuse_admission`) on the SAME inputs; `admit_into`
  candidates recompile the SAME base source into the SAME manifest.
- **Distribution:** {cost['iters']} timed iterations per cell; median/p90/p99
  reported, never a single lucky sample.
- **Determinism:** pure `compile_source`/`admit_into` on in-memory strings - no
  disk I/O in the timed section, no network, no toolchain.
"""


def _fmt(verdict: tuple[bool, str | None]) -> str:
    admitted, code = verdict
    if admitted:
        return "admit"
    return f"refuse ({code})" if code else "refuse"


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="in-process admission gate harness")
    ap.add_argument("--iters", type=int, default=2000,
                    help="timed iterations per cost cell (default 2000)")
    ap.add_argument("--write", action="store_true",
                    help="rewrite bench/results/inprocess-gate.md")
    args = ap.parse_args()

    version = gate_version()
    print(f"gate surface: api={version['api']} language={version['language']} "
          f"frontier={version['frontier']}")

    manifest = base_manifest()
    batch = correctness_batch()

    print("\nverdict identity (in-process vs reference admission oracle):")
    records = check_matches(batch, manifest)
    for r in records:
        flag = "ok " if r.match else "MISMATCH"
        print(f"  [{flag}] {r.name:26s} {r.kind:11s} "
              f"in-process={_fmt(r.inproc)!s:14s} oracle={_fmt(r.oracle)}")
    all_match = all(r.match for r in records)

    order_ok, order_detail = order_independence(batch, manifest)
    print(f"\norder-independence (fixed vs shuffled batch order): "
          f"{'holds' if order_ok else 'FAILED: ' + str(order_detail['mismatches'])}")

    # A1 negative control: the hole draft the admission gate refuses is ACCEPTED
    # by a naive `revl compile` oracle. This must be true (else the control is
    # not exercising the trap) and it must DISAGREE with the admission verdict.
    hole = next(c for c in batch if c.is_hole)
    naive_hole_accepts = naive_compile_accepts(hole.source)
    hole_refused = not inprocess_verdict(hole, manifest)[0]
    print(f"\nA1 control: `revl compile` on the hole draft "
          f"{'ACCEPTS (exit 0)' if naive_hole_accepts else 'rejects'}; "
          f"in-process admission {'REFUSES' if hole_refused else 'admits'} it "
          f"-> {'they DISAGREE (oracle must be admission, not compile)' if naive_hole_accepts and hole_refused else 'CONTROL BROKEN'}")

    print("\ncost distribution:")
    cost = measure_cost(iters=args.iters)
    for c in cost["cells"]:
        s = c["stats"]
        print(f"  {c['candidate']:6s} candidate / {c['manifest_components']:2d} "
              f"manifest comps : median {s['median']:.3f} ms  p90 {s['p90']:.3f} "
              f"ms  p99 {s['p99']:.3f} ms  (n={s['n']})")
    rep = cost["representative"]
    print(f"  representative (item-16)      : median {rep['median']:.3f} ms  "
          f"p90 {rep['p90']:.3f} ms  (n={rep['n']})")

    ok = all_match and order_ok and naive_hole_accepts and hole_refused
    if args.write:
        out = BENCH / "results" / "inprocess-gate.md"
        out.write_text(render_md(cost, records, order_ok, naive_hole_accepts))
        print(f"\nwrote {out}")

    print(f"\n{'PASS' if ok else 'FAIL'}: verdict identity"
          f"{' + order-independence + A1 control' if ok else ' or a check FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

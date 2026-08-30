"""One-off generator for method_compensate.ir.json — NOT part of the build.

The TS-tier mirror of tests/test_provide_method_compensate.py (roadmap item
392): a component whose PROVIDE-METHOD does `emit ... compensate ...` PER TOOL
CALL, so each call registers a COMPENSATION onto the component's activation
frame (`Frame.compensationMethod`) — the compensation analog of item 318's
per-tool-call witnessed wiring. On a clean unload the offset is DISCHARGED
(never runs; the emission was the deliverable); on `frame.abort()` + unload it
FIRES in PHASE 2, strictly after the method's transactional proof inverse,
guarded and residue-collected.

The whole `.rvl` source — externs AND the component with its provide-method —
is compiled by `compile_source`, so the component/provide/method shapes are
real compiler output, not hand-assembled IR (mirrors the py suite). Run once,
by hand, to regenerate the checked-in fixture:

    python3 backends/typescript/tests/fixtures/_gen_method_compensate.py

The method body carries BOTH entry kinds in one call:
  * `effect stash_path(p)` — a witnessed transactional mutation (Phase-1 proof
    inverse) over an in-memory "file world" on `globalThis.__revlFsWorld`;
    `unstash` restores AND records 'unstash' onto `hostLog`.
  * `emit note(msg) compensate offset(msg)` — the emission deliverable plus its
    offsetting compensation (Phase 2); `offset` records 'compensate:<msg>'.
So the phase ordering ('unstash' before 'compensate:<msg>') is OBSERVED on the
shared `hostLog`, not inferred.
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_SOURCE = (
    "type Stash = { path: Str, bak: Str }\n"
    "type FsError = { code: Str }\n"
    "extern pure fn unstash(w: Stash) -> Unit = @ts {\n"
    "  const world = (globalThis as any).__revlFsWorld\n"
    "  record('unstash')\n"
    "  if (world[w.bak] !== undefined) {\n"
    "    world[w.path] = world[w.bak]\n"
    "    delete world[w.bak]\n"
    "  }\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @ts {\n"
    "  const world = (globalThis as any).__revlFsWorld\n"
    "  const bak = p + '.bak'\n"
    "  world[bak] = world[p]\n"
    "  delete world[p]\n"
    "  return { kind: 'Ok', value: { path: p, bak } }\n"
    "}\n"
    # the emission: a bare boundary crossing, no reversal possible.
    "extern emission fn note(msg: Str) -> Unit = @ts { return }\n"
    # the compensation: a best-effort offset that records 'compensate:<msg>'.
    "extern pure fn offset(msg: Str) -> Unit = @ts {\n"
    "  record('compensate:' + msg)\n"
    "}\n"
    # a compensation that records THEN throws — proves a failed Phase-2 offset
    # is best-effort (continue-and-record), never fails or interrupts the abort.
    "extern pure fn offset_fails(msg: Str) -> Unit = @ts {\n"
    "  record('compensate:' + msg)\n"
    "  throw new Error('offset boom')\n"
    "}\n"
    "service Ops {"
    " emission fn run(p: Str, msg: Str)"
    " emission fn run_fails(p: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn run(p, msg) {\n"
    "      effect stash_path(p)\n"
    "      emit note(msg) compensate offset(msg)\n"
    "    }\n"
    "    fn run_fails(p, msg) {\n"
    "      effect stash_path(p)\n"
    "      emit note(msg) compensate offset_fails(msg)\n"
    "    }\n"
    "  }\n"
    "}\n"
)


def build() -> dict:
    return compile_source(_SOURCE, "method_compensate.rvl")


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "method_compensate.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")

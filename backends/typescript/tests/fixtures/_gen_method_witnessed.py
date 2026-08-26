"""One-off generator for method_witnessed.ir.json — NOT part of the build.

The TS-tier mirror of tests/test_provide_method_witnessed.py (roadmap item 318
-> 324): a component whose PROVIDE-METHOD does a witnessed fs mutation PER TOOL
CALL, so each call registers a transactional inverse into the component's
activation frame (`Frame.transactionalMethod`). On a clean unload the per-call
mutations PERSIST (discharged); on `frame.abort()` + unload they REVERT,
residue-free; the WAL discharge-descriptors enumerate every crossing.

The whole `.rvl` source — externs AND the component with its provide-method —
is compiled by `compile_source`, so the component body/provide/method shapes
are real compiler output, not hand-assembled IR (mirrors
tests/test_provide_method_witnessed.py, which compiles the same shape). Run
once, by hand, to regenerate the checked-in fixture:

    python3 backends/typescript/tests/fixtures/_gen_method_witnessed.py

The witnessed extern is a rename-with-a-data-witness stand-in over an in-memory
"file world" on `globalThis.__revlFsWorld` (the same hermetic style
tests/witnessed_teardown.test.ts uses for its box), extended to take the target
path as a PARAMETER so each per-call invocation mutates a distinct entry — the
shape of an agent calling one fs tool repeatedly. `record()` events let the
suite assert ordering exactly like the other TS runtime suites.
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
    # idempotent (243 rule 5): un-stashing an already-restored entry is a no-op.
    "extern pure fn unstash(w: Stash) -> Unit = @ts {\n"
    "  const world = (globalThis as any).__revlFsWorld\n"
    "  if (world[w.bak] !== undefined) {\n"
    "    world[w.path] = world[w.bak]\n"
    "    delete world[w.bak]\n"
    "  }\n"
    "  record('unstash ' + w.path)\n"
    "}\n"
    "extern witnessed[fs] fn stash_path(p: Str) -> Result[Stash, FsError]"
    " undo unstash(result) = @ts {\n"
    "  const world = (globalThis as any).__revlFsWorld\n"
    "  const bak = p + '.bak'\n"
    "  world[bak] = world[p]\n"
    "  delete world[p]\n"
    "  record('stash ' + p)\n"
    "  return { kind: 'Ok', value: { path: p, bak } }\n"
    "}\n"
    # the service method declares the fs capability (`emission fn`), so the
    # per-call witnessed crossing stays visible to a consumer of `Ops`.
    "service Ops { emission fn touch(p: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn touch(p) {\n"
    "      effect stash_path(p)\n"
    "    }\n"
    "  }\n"
    "}\n"
)


def build() -> dict:
    return compile_source(_SOURCE, "method_witnessed.rvl")


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "method_witnessed.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")

"""One-off generator for witnessed_teardown.ir.json — NOT part of the build.

Mirrors tests/test_witnessed_runtime.py's approach: `compile_source` builds
the externs table (so the `witnessed[...]` classification, the declared
`undo`, and the checked undo-slot shape are all real compiler output, not
hand-typed IR), and the component bodies are hand-assembled dicts, exactly
the shape backends/typescript/emit.py's `_witnessed_step`/`_component_step`
consume (docs/design/243-witnessed-externs.md, docs/design/
teardown-contract.md). Run once, by hand, to regenerate the checked-in
fixture; not invoked by any test or CI path.

    python3 backends/typescript/tests/fixtures/_gen_witnessed_teardown.py
"""

import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

# The witnessed extern: an in-memory "stash" whose Ok witness carries enough
# to restore it. `@ts` bodies push into runtime.ts's shared `record()` trace
# so the vitest suite can assert ordering the same way TCK-style scenarios do
# (`hostLog`), without inventing a second observability channel.
#
# `ledger_insert`/`ledger_remove` are the compensation's forward/offset pair,
# deliberately over a SEPARATE resource (a plain global list, not the
# activation's own `store` Map) — the compensation must still be able to act
# in Phase 2, which runs strictly AFTER every Phase-1 bracket inverse
# (including `store`'s own `.drop()`); targeting `store` itself would tear
# down the very resource the compensation needs before Phase 2 ever reaches
# it. A real compensation almost always offsets a REMOTE/external system for
# exactly this reason (the audit surface's own row, not local activation
# state), so this mirrors the realistic shape, not just a test workaround.
_EXTERNS = (
    "type StashWitness = { was: Str }\n"
    "type StashError = { reason: Str }\n"
    # idempotent (243 rule 5): restoring an already-restored box is a no-op.
    "extern pure fn wit_unstash(w: StashWitness) -> Unit = @ts {\n"
    "  const box = (globalThis as any).__revlWitnessBox\n"
    "  box.value = w.was\n"
    "  record('wit_unstash was=' + w.was)\n"
    "}\n"
    "extern witnessed[fs] fn wit_stash() -> Result[StashWitness, StashError]"
    " undo wit_unstash(result) = @ts {\n"
    "  const box = (globalThis as any).__revlWitnessBox\n"
    "  const was = box.value\n"
    "  box.value = 'STASHED'\n"
    "  record('wit_stash was=' + was)\n"
    "  return { kind: 'Ok', value: { was } }\n"
    "}\n"
    "extern emission fn ledger_insert(row: Str) -> Unit = @ts {\n"
    "  const ledger = (globalThis as any).__revlLedger\n"
    "  ledger.push(row)\n"
    "  record('ledger.insert ' + row)\n"
    "}\n"
    "extern pure fn ledger_remove(row: Str) -> Unit = @ts {\n"
    "  const ledger = (globalThis as any).__revlLedger\n"
    "  const at = ledger.indexOf(row)\n"
    "  if (at >= 0) ledger.splice(at, 1)\n"
    "  record('ledger.remove ' + row)\n"
    "}\n"
)

_BASE = compile_source(_EXTERNS, "witnessed_teardown.rvl")


def _call(target, method, *args):
    return {"kind": "call", "target": target, "method": method,
            "args": [{"kind": "lit", "value": a} if not isinstance(a, dict) else a
                     for a in args]}


def _name(id_: str) -> dict:
    return {"kind": "name", "id": id_}


def _fn(name: str, *args) -> dict:
    return {"kind": "fn", "name": name,
            "args": [{"kind": "lit", "value": a} if not isinstance(a, dict) else a
                     for a in args]}


def _workflow(name: str, *, abort: bool) -> dict:
    """One activation exercising all three entry kinds, sharing one LIFO
    stack, in registration order:

      1. bracket        — a host `Map` (store), undo = store.drop()
      2. transactional  — the witnessed `wit_stash`/`wit_unstash` pair
      3. compensation    — ledger_insert("row") / compensate ledger_remove("row")

    The compensation targets a SEPARATE resource (a plain global list, not
    `store`) — see the module docstring: Phase 2 runs strictly after every
    Phase-1 bracket inverse, including `store`'s own `.drop()`, so a
    compensation over `store` itself would find it already torn down.

    `abort=True` appends a `fail` step after all three registrations, so the
    activation never commits — proving the mixed-entry two-phase abort
    (Phase 1: bracket + transactional LIFO to completion; Phase 2:
    compensation, after). `abort=False` leaves it to unload cleanly —
    proving persist-on-commit + bracket-still-reverts, side by side.
    """
    body = [
        {"step": "let-effect", "bind": "store",
         "acquire": {"kind": "host", "fn": "Map.new", "args": []},
         "undo": _call(_name("store"), "drop")},
        {"step": "effect", "acquire": _fn("wit_stash")},
        {"step": "emit",
         "expr": _fn("ledger_insert", "row"),
         "compensate": _fn("ledger_remove", "row")},
    ]
    if abort:
        body.append({"step": "fail", "message": {"kind": "lit", "value": "boom"}})
    return {"name": name, "config": [], "requires": {}, "provides": {}, "body": body}


def build() -> dict:
    ir = copy.deepcopy(_BASE)
    ir["components"] = [
        _workflow("WorkflowCommit", abort=False),
        _workflow("WorkflowAbort", abort=True),
    ]
    return ir


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent / "witnessed_teardown.ir.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")

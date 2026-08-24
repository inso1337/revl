"""FR-3 stdlib JSON on the TypeScript tier — the `Any` mapping (roadmap 79).

`json_parse(s: Str) -> Any` returns the type algebra's wildcard `Any`. Before
this fix the ts emitter mapped `Any` -> `any` only in signature position; in
every other position `_ts_v3_type("Any")` fell through to `_ident` and emitted
a *bare* `Any` identifier that `tsc` rejects with `Cannot find name 'Any'`
(harness milestone-2 finding #14). The same held for `Never`.

The durable guard is the golden `golden/fr3_json.ts`, which `npm run typecheck`
compiles (tsconfig includes golden/**), so a regression fails `tsc`. These
toolchain-free checks fold the same invariant into the Python suite:

    .venv/bin/pytest backends/typescript/test_fr3_ts_emit.py -q

Run with:
    .venv/bin/pytest backends/typescript/ -q
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_ts_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_fr3", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ts_v3_type_maps_any_and_never():
    """`Any`/`Never` resolve to real TS types in EVERY position — not a bare
    identifier. `Any` -> `any` (assignable both ways, so a parsed value flows
    into a typed position AND supports field access under `strict`, which
    `unknown` would reject); `Never` -> `never`."""
    m = _load_ts_emit()
    assert m._ts_v3_type("Any") == "any"
    assert m._ts_v3_type("Never") == "never"
    # ...and through the compound-type constructors, where the bug also bit.
    assert m._ts_v3_type("List[Any]") == "any[]"
    assert m._ts_v3_type("Opt[Any]") == "any | undefined"
    assert m._ts_v3_type("Map[Str, Any]") == "Map<string, any>"


def test_fr3_json_emits_no_bare_any():
    """Emit the FR-3 consumer and assert no bare `Any`/`Never` identifier
    survives — the token `tsc` rejects. `any`/`never` (lowercase) are fine."""
    m = _load_ts_emit()
    ir = json.loads((BACKEND / "tests" / "fixtures" / "fr3_json.ir.json")
                    .read_text(encoding="utf-8"))
    out = m.emit(ir)
    assert not re.search(r"\bAny\b", out), "bare `Any` leaked into emitted TS"
    assert not re.search(r"\bNever\b", out), "bare `Never` leaked into emitted TS"
    # the extern signatures carry the mapped type, both in return and param
    # position (the `json_stringify(v: Any)` param was the second bare-`Any`)
    assert "export function json_parse(s: string): any {" in out
    assert "export function json_stringify(v: any): string {" in out


def test_fr3_golden_is_current():
    """The checked-in golden matches the emitter, so a stale golden (or an
    emitter change that reintroduces the bug) fails here as well as under
    `tsc`. Regenerate with backends/typescript/scripts/regen-golden.py."""
    m = _load_ts_emit()
    ir = json.loads((BACKEND / "tests" / "fixtures" / "fr3_json.ir.json")
                    .read_text(encoding="utf-8"))
    golden = (BACKEND / "golden" / "fr3_json.ts").read_text(encoding="utf-8")
    assert m.emit(ir) == golden

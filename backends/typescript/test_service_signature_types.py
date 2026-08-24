"""Type names in emitted service signatures (roadmap item 89 + its regression).

Item 89 made a *declared* record render by name in a service interface so the
signature lines up with the emitted `interface` (`List[Msg]` -> `Msg[]`, not
`unknown[]`) — harness finding #19. It did so by making `_ts_type` fall through
to the v3 renderer for *every* unrecognised name, which over-reached: an IR
v1/v2 document has no `type` declarations, so a name like `examples/user_cache`
's `Row` (`fn query(sql) -> List[Row]`) resolved to nothing and emitted a
dangling `Row[]` that `tsc` rejects with `Cannot find name 'Row'`. Every other
tier erases that same undeclared `Row` to an opaque type (java `Object`, rust
`Value`); the ts analogue is `unknown`.

These toolchain-free checks pin both halves so neither can come back:

    .venv/bin/pytest backends/typescript/test_service_signature_types.py -q
"""

import importlib.util
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_ts_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_sigtypes", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v1_service_ir() -> dict:
    """A minimal IR v1 module whose only reference to `Row` is a service
    signature, and which declares no types — the `examples/user_cache` shape."""
    return {
        "ir_version": 1,
        "services": {
            "Database": {
                "methods": {
                    "query": {
                        "params": [{"name": "sql", "type": "Str"}],
                        "returns": "List[Row]",
                        "emission": False,
                    },
                },
            },
        },
        # No component needed: `_emit_v1` emits the service interface regardless,
        # and it is that interface — the only place `Row` appears — under test.
        "components": [],
    }


def _v3_service_ir() -> dict:
    """An IR v3 module that DECLARES a record `Row` and references it ONLY from
    a service signature — never in a component body — plus a second record `Cell`
    reached transitively through `Row`'s fields."""
    return {
        "ir_version": 3,
        "types": {
            "Cell": {"kind": "record", "params": [], "fields": {"v": "Str"}},
            "Row": {"kind": "record", "params": [],
                    "fields": {"id": "Int", "cell": "Cell"}},
        },
        "services": {
            "Database": {
                "methods": {
                    "query": {
                        "params": [{"name": "sql", "type": "Str"}],
                        "returns": "List[Row]",
                        "emission": False,
                    },
                },
            },
        },
        "components": [],
    }


def test_v1_undeclared_signature_name_is_opaque():
    """A v1/v2 document declares no types, so a name reachable only through a
    service signature erases to `unknown` — never a dangling `Row` that `tsc`
    rejects with `Cannot find name 'Row'`."""
    m = _load_ts_emit()
    out = m.emit(_v1_service_ir())
    assert "query(sql: string): unknown[]" in out
    assert not re.search(r"\bRow\b", out), "undeclared `Row` leaked into emitted TS"


def test_ts_type_gates_by_declared_names():
    """The unit that draws the line: a bare name renders by name only when the
    document declares it; otherwise it is opaque."""
    m = _load_ts_emit()
    known = frozenset({"Row"})
    assert m._ts_type("List[Row]", known) == "Row[]"
    assert m._ts_type("Row", known) == "Row"
    # undeclared -> opaque, in bare and compound position alike
    assert m._ts_type("List[Row]") == "unknown[]"
    assert m._ts_type("Row") == "unknown"
    assert m._ts_type("Opt[Row]") == "unknown | undefined"


def test_v3_record_used_only_in_signature_gets_its_interface():
    """A declared record referenced ONLY in a service signature still renders by
    name there AND has its `interface` emitted (transitively, for a record it
    reaches through its fields) — so the emitted service interface type-checks."""
    m = _load_ts_emit()
    out = m.emit(_v3_service_ir())
    # rendered by name in the signature (not `unknown[]`)
    assert "query(sql: string): Row[]" in out
    # ...and both the record and the record it reaches transitively are declared
    assert "export interface Row {" in out
    assert "export interface Cell {" in out
    # the transitive field carries the declared record name, not `unknown`
    assert "cell: Cell" in out

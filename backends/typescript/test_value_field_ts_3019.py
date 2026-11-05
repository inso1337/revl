"""A record field of type `Value` must typecheck on the TypeScript tier (#3019).

`Value` (stdlib/value.rvl, docs/stdlib-value.md) is the NAMED erased-dynamic
builtin: the checker reserves it, accepts it in a record field, and every tier
erases it to that host's dynamic value representation (py `Any`, Go `any`).
TypeScript already erased the wildcard `Any` to `any`, but `_TS_V3_TYPE` had no
`Value` entry, so `_ts_v3_type` fell through to `_ident` and a record emitted
`tags: Value[]` / `v: Value` against nothing that declares `Value`. `tsc`
rejects that with `Cannot find name 'Value'` (issue #3019). A type check fails
only; the bytes that reach the runtime are unaffected.

The mapping is asserted directly, the emitted module is asserted to hold no bare
`Value` identifier, and it is typechecked through ``tools/tscheck.mjs`` when the
backend's TypeScript toolchain is installed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402

VALUE_RECORD = """\
pub type Box = { tags: List[Value], v: Value, m: Map[Str, Value] }
"""


def _load_ts_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_value3019", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_value_maps_to_any_in_every_position():
    """`Value` is the erased dynamic value, which TS spells `any` exactly as it
    spells the wildcard `Any`, in a field and through every compound type."""
    m = _load_ts_emit()
    assert m._ts_v3_type("Value") == "any"
    assert m._ts_v3_type("List[Value]") == "any[]"
    assert m._ts_v3_type("Opt[Value]") == "any | undefined"
    assert m._ts_v3_type("Map[Str, Value]") == "Map<string, any>"


def test_record_field_of_value_emits_no_bare_value_identifier():
    m = _load_ts_emit()
    out = m.emit(compile_source(VALUE_RECORD, "value_record.rvl"))
    assert "tags: any[]" in out
    assert "v: any" in out
    assert "m: Map<string, any>" in out
    assert not re.search(r"\bValue\b", out), "bare `Value` leaked into emitted TS"


def test_record_field_of_value_typechecks():
    if shutil.which("node") is None:
        pytest.skip("node not available")
    if not (BACKEND / "node_modules" / "typescript").exists():
        pytest.skip("backends/typescript/node_modules not installed")
    m = _load_ts_emit()
    out = m.emit(compile_source(VALUE_RECORD, "value_record.rvl"))
    with tempfile.TemporaryDirectory(dir=BACKEND, prefix=".value-field-") as tmp:
        directory = Path(tmp)
        (directory / "case_0.ts").write_text(out, encoding="utf-8")
        payload = json.dumps({
            "dir": str(directory),
            "files": ["case_0.ts"],
            "tsconfig": str(BACKEND / "tsconfig.json"),
        })
        result = subprocess.run(
            ["node", str(ROOT / "tools" / "tscheck.mjs")],
            input=payload, capture_output=True, text=True, cwd=BACKEND,
            timeout=300,
        )
    assert result.returncode == 0, result.stderr
    diagnostics = json.loads(result.stdout)
    assert not diagnostics.get("case_0.ts"), diagnostics

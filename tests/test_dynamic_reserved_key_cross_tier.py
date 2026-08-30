"""Roadmap items 279 / 299 / 380 — the dynamic reserved-word field-access class,
now CLOSED at the frontend by item 380.

History. Item 279 found the TS tier reading a reserved-word key on a `json_parse`
/ `Any` (dynamic) value as `undefined`: the key was renamed on ACCESS
(`tc.function` -> `tc.function_`) but the runtime JSON object still held the raw
key, so the same admitted program diverged between tiers SILENTLY (py read the
raw key and worked; ts read `undefined`). Item 299 audited rust/go/java/wasm and
found none reproduced the *silent* class — a dynamic field access on those tiers
emits a static field selection the target compiler rejects loudly (or, for wasm,
`json_parse` is refused at emit).

Item 380 supersedes that whole finding at the ROOT. `tc.range.name` is a NON-`Opt`
field read off a value whose static type is `Any` — exactly the 279/299
divergence class — and item 380(2) makes the FRONTEND REFUSE it: a field read
off `Any`/`Value` is a compile error on every tier, before any emit, because an
erased value has no known fields and neither a py raw-key read nor a ts
`undefined` is a defensible total answer. So the class can no longer be WRITTEN,
uniformly, which is a stronger resolution than "py works, the others fail
loudly": there is nothing left to diverge.

The replacement is the total shape surface (stdlib/value.rvl): `value_field(tc,
"range")` reads a reserved key by STRING, so the identifier-mangle divergence
that started this whole saga cannot arise — there is no identifier to mangle. It
is total (absent / non-record -> null Value) and byte-identical on py + ts.

This file now LOCKS:
  * the dynamic reserved-word read off `Any` is REFUSED at the frontend, with a
    message that names `Any` and points at the record-cast / value.rvl surfaces;
  * `value_field(tc, "range")` is the mangle-free, cross-tier replacement, read
    identically on py + ts.

    pytest tests/test_dynamic_reserved_key_cross_tier.py -q
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

# The two-line 279 lighthouse: read a reserved-word key off a dynamic
# (`json_parse` / `Any`) value. `{w}` is BOTH a valid revl identifier AND a
# reserved word in some tier, so historically the sanitizer fired on the access.
# Item 380 now refuses the read at the frontend before any of that matters.
_SRC = """
pub extern pure fn jp(s: Str) -> Any
  = @py {{ import json; return json.loads(s) }}
  = @ts {{ return JSON.parse(s) }}
  = @go {{ return nil }}
  = @java {{ return null; }}
  = @rs {{ cordis::Value::Null }}

pub fn read_key(s: Str) -> Str {{
  let tc: Any = jp(s)
  return tc.{w}.name
}}
"""


def _compile(word: str) -> dict:
    return compile_source(_SRC.format(w=word))


# ------------------------------------------------- item 380: refused at the frontend

@pytest.mark.parametrize("word", ["range", "class", "move", "function"])
def test_dynamic_reserved_key_read_is_refused_at_the_frontend(word):
    """A field read off an `Any` value is refused for ANY key — reserved word or
    not — before a single tier emits. This is what closes the 279/299 class: the
    divergence cannot be written, so it cannot diverge."""
    with pytest.raises(RevlError) as ei:
        _compile(word)
    msg = str(ei.value)
    assert "Any" in msg
    # the refusal points the author at the designed surfaces
    assert "value_field" in msg or "record" in msg


# ------------------------------------------------ the migration: value_field, cross-tier

_STDLIB = ("json.rvl", "str.rvl", "value.rvl")

# The 279 program, rewritten on the total shape surface. The reserved key is a
# STRING argument to `value_field`, so there is no identifier to mangle and no
# tier can rename it — the root cause of the whole saga is structurally gone.
_MIGRATED = r'''
use "stdlib/json.rvl"  { json_parse }
use "stdlib/value.rvl" { value_field, value_str }

pub fn read_key(s: Str) -> Str {
  let tc = json_parse(s)
  return value_str(value_field(value_field(tc, "range"), "name"))
}
'''


def _compile_with_stdlib(src: str) -> dict:
    d = Path(tempfile.mkdtemp())
    (d / "stdlib").mkdir()
    for name in _STDLIB:
        (d / "stdlib" / name).write_text(
            (ROOT / "stdlib" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (d / "consumer.rvl").write_text(src, encoding="utf-8")
    return compile_files([str(d / "consumer.rvl")])


def _emit(backend: str, ir: dict) -> str:
    path = str(ROOT / "backends" / backend)
    sys.path.insert(0, path)
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_{backend}_dyn380", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out = module.emit(ir)
        return "\n".join(out.values()) if isinstance(out, dict) else str(out)
    finally:
        sys.path.remove(path)


def _exec_py(ir: dict) -> dict:
    code = _emit("python", ir)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had, previous = "runtime" in sys.modules, sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        ns: dict = {}
        exec(compile(code, "emitted.py", "exec"), ns)  # noqa: S102 — our own emitter
        return ns
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]


def _run_ts(ir: dict, tail: str):
    code = _emit("typescript", ir)
    d = Path(tempfile.mkdtemp())
    (d / "runtime.ts").write_text("export const host: any = {};\n", encoding="utf-8")
    pkg = d / "pkg"
    pkg.mkdir()
    (pkg / "mod.ts").write_text(code + "\n" + tail + "\n", encoding="utf-8")
    proc = subprocess.run(  # noqa: S603
        ["node", str(pkg / "mod.ts")], capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


_BODY = '{"range": {"name": "get_weather"}}'


def test_py_value_field_reads_the_reserved_key_by_string():
    """`value_field(tc, "range")` reads the raw reserved key with no mangle —
    the key is a string, not an identifier."""
    ns = _exec_py(_compile_with_stdlib(_MIGRATED))
    assert ns["read_key"](_BODY) == "get_weather"
    # absent / non-object tolerated (total), never a raise
    assert ns["read_key"]("{}") == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ts_value_field_matches_py():
    """The same source on ts (value.rvl is two-tier) reads the reserved key
    identically — no `tc.range_` rename, so no divergence."""
    ir = _compile_with_stdlib(_MIGRATED)
    ns = _exec_py(ir)
    for body in (_BODY, "{}"):
        py_out = ns["read_key"](body)
        ts_out = _run_ts(
            ir, f"console.log(JSON.stringify(read_key({json.dumps(body)})));")
        assert py_out == ts_out
    assert ns["read_key"](_BODY) == "get_weather"

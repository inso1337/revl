"""The TOTAL FIELD READ *surface* — roadmap item 379 (revl-harness lane H24).

Item 367 landed the pure-revl accessor `value_field_or(v, name, default)`, a
tolerant field read spelled as a FUNCTION CALL. That is not the surface the
harness's tolerant decoders want to write. The designed spelling for "may be
absent" is a plain field access under `??`:

    type EOpt = { kind: Opt[Str] }
    fn f(v: Any) -> Str {
      let e: EOpt = v            // a type-erased cast (JSON parse gives `Any`)
      return e.kind ?? "<none>"  // the one designed spelling, every tier
    }

On current main this DIVERGES: py emits `_revl_field(e, 'kind')` == `e['kind']`
(a KeyError when the parsed body lacks `kind`) while ts emits `e.kind` (total by
JS accident, `undefined` -> the `??` default). This module pins the fixed
semantics across every AVAILABLE executable tier (py + ts, the tiers on which
both stdlib/json.rvl and stdlib/value.rvl exist):

  (1) an `Opt[T]`-declared field read is TOTAL on every tier: absent, present,
      wrong-typed, and non-object receivers all read back the Opt's empty case
      (never a raise, never a silent `undefined` that outlives the `??`);
  (2) a NON-`Opt` field read on a value whose static type is `Any` is REFUSED by
      the frontend (a compile error, not a runtime divergence) — see the
      rejection fixture `docs/rejections.md` / tests below;
  (3) the Any-SHAPE discrimination surface (`value_is_object` / `value_is_list`
      / `value_is_scalar`, and the total `value_has` / `value_opt` has/lookup
      pair) answers is-object / is-list / is-scalar before any binding, so the
      harness externs' `isinstance(d, dict)` becomes expressible in-language.

Per-shape (present / absent / wrong-typed / non-object) the py and ts tiers
produce byte-identical results.
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

from revl import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402

_STDLIB = ("json.rvl", "str.rvl", "value.rvl")


def _compile(consumer_src: str) -> dict:
    d = Path(tempfile.mkdtemp())
    (d / "stdlib").mkdir()
    for name in _STDLIB:
        (d / "stdlib" / name).write_text(
            (ROOT / "stdlib" / name).read_text(encoding="utf-8"), encoding="utf-8")
    (d / "consumer.rvl").write_text(consumer_src, encoding="utf-8")
    return compile_files([str(d / "consumer.rvl")])


def _emit(backend: str, ir: dict) -> str:
    path = str(ROOT / "backends" / backend)
    sys.path.insert(0, path)
    try:
        spec = importlib.util.spec_from_file_location(
            f"emit_surface_{backend}", ROOT / "backends" / backend / "emit.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.emit(ir))
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


# ============================================================ (1) the surface

# `e.kind ?? "<none>"` — the designed spelling, over a type-erased cast from a
# parsed `Any`. Four shapes: the field present, absent, present-but-wrong-typed,
# and the whole document not an object.
_SURFACE = r'''
use "stdlib/json.rvl" { json_parse }

type EOpt = { kind: Opt[Str] }

// the ONE designed spelling for "may be absent": a plain field access under ??.
pub fn read_kind(body: Str) -> Str {
  let e: EOpt = json_parse(body)
  return e.kind ?? "<none>"
}
'''


@pytest.fixture(scope="module")
def surface_ir() -> dict:
    return _compile(_SURFACE)


# body -> expected result of `read_kind`
_SHAPES = {
    "present":     ('{"kind": "final"}', "final"),
    "absent":      ('{"other": 1}',      "<none>"),
    "present_null":('{"kind": null}',    "<none>"),
    "non_object":  ('[1, 2, 3]',         "<none>"),
    "scalar":      ('"hello"',           "<none>"),
}


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_py_opt_field_read_is_total(surface_ir, shape):
    body, expected = _SHAPES[shape]
    ns = _exec_py(surface_ir)
    assert ns["read_kind"](body) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("shape", list(_SHAPES))
def test_ts_opt_field_read_matches_py(surface_ir, shape):
    body, expected = _SHAPES[shape]
    ns = _exec_py(surface_ir)
    py_out = ns["read_kind"](body)
    ts_out = _run_ts(
        surface_ir,
        f"console.log(JSON.stringify(read_kind({json.dumps(body)})));")
    assert py_out == ts_out == expected


# ============================================ (2) refuse non-Opt read off `Any`

# A NON-`Opt` field read on a value whose static type is `Any` is the 279/299
# silent-divergence class: py raises, ts yields `undefined`. The frontend now
# REFUSES it, pushing the author to a cast-to-record (then Opt fields are total)
# or the shape-discrimination surface.
_REFUSE = r'''
use "stdlib/json.rvl" { json_parse }
pub fn bad(body: Str) -> Str {
  let v: Any = json_parse(body)
  return v.url            // refused: a field read off `Any`
}
'''


def test_field_read_off_any_is_refused():
    with pytest.raises(RevlError) as ei:
        _compile(_REFUSE)
    msg = str(ei.value)
    assert "Any" in msg


# A record-cast Opt field read is STILL allowed (it is exactly path (1)).
_ALLOW_OPT_ON_RECORD = r'''
use "stdlib/json.rvl" { json_parse }
type EOpt = { kind: Opt[Str] }
pub fn ok(body: Str) -> Str {
  let e: EOpt = json_parse(body)
  return e.kind ?? "x"
}
'''


def test_opt_field_read_on_record_cast_still_compiles():
    _compile(_ALLOW_OPT_ON_RECORD)  # must not raise


# ============================================ (3) Any-shape discrimination

# The isinstance(d, dict) replacement: ask is-object / is-list / is-scalar, and
# a total has/lookup, on a value bound straight from `Any`, before any cast.
_SHAPE_DISC = r'''
use "stdlib/json.rvl"  { json_parse }
use "stdlib/value.rvl" { value_is_object, value_is_list, value_is_scalar,
                         value_has, value_opt, value_str }

// "obj|list|scalar|null:<has-kind>:<lookup-kind>"
pub fn shape_of(body: Str) -> Str {
  let v = json_parse(body)
  let tag = value_is_object(v) ? "obj"
          : (value_is_list(v) ? "list"
          : (value_is_scalar(v) ? "scalar" : "null"))
  let has = value_has(v, "kind") ? "Y" : "N"
  let look = value_str(value_opt(v, "kind") ?? "-")
  return `${tag}:${has}:${look}`
}
'''


@pytest.fixture(scope="module")
def shape_disc_ir() -> dict:
    return _compile(_SHAPE_DISC)


_DISC_SHAPES = {
    "object_with_kind": ('{"kind": "final"}', "obj:Y:final"),
    "object_no_kind":   ('{"other": 1}',      "obj:N:-"),
    "list":             ('[1, 2]',            "list:N:-"),
    "scalar_str":       ('"hi"',              "scalar:N:-"),
    "null":             ('null',              "null:N:-"),
}


@pytest.mark.parametrize("shape", list(_DISC_SHAPES))
def test_py_shape_discrimination(shape_disc_ir, shape):
    body, expected = _DISC_SHAPES[shape]
    ns = _exec_py(shape_disc_ir)
    assert ns["shape_of"](body) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("shape", list(_DISC_SHAPES))
def test_ts_shape_discrimination_matches_py(shape_disc_ir, shape):
    body, expected = _DISC_SHAPES[shape]
    ns = _exec_py(shape_disc_ir)
    py_out = ns["shape_of"](body)
    ts_out = _run_ts(
        shape_disc_ir,
        f"console.log(JSON.stringify(shape_of({json.dumps(body)})));")
    assert py_out == ts_out == expected


# ============================ the payoff: a parse_envelope-style decode, pure revl

# The harness's `parse_envelope` floor: a provider reply whose keys may all be
# absent, and which may not even be an object. Written with the new surface —
# a shape guard then optional field reads — it needs no @py/@ts tolerant extern.
_ENVELOPE = r'''
use "stdlib/json.rvl"  { json_try_parse }
use "stdlib/value.rvl" { value_is_object, value_opt, value_str }

type Envelope = { id: Opt[Str], reasoning: Opt[Str] }

// tolerant reply decode: not-an-object -> "ERR"; else read id + reasoning if
// present (reasoning may be spelled reasoning_content), each tolerating absence.
pub fn parse_envelope(wire: Str) -> Str {
  return match json_try_parse(wire) {
    Ok(v) => decode(v),
    Err(e) => "ERR"
  }
}
fn decode(v: Any) -> Str {
  if (!value_is_object(v)) { return "ERR" }
  let e: Envelope = v
  let id = e.id ?? "?"
  let reasoning =
    value_str(value_opt(v, "reasoning") ?? (value_opt(v, "reasoning_content") ?? ""))
  return `${id}/${reasoning}`
}
'''


@pytest.fixture(scope="module")
def envelope_ir() -> dict:
    return _compile(_ENVELOPE)


_ENVELOPE_CASES = {
    "full":              ('{"id": "abc", "reasoning": "because"}', "abc/because"),
    "reasoning_content": ('{"id": "abc", "reasoning_content": "alt"}', "abc/alt"),
    "id_only":           ('{"id": "abc"}', "abc/"),
    "empty":             ('{}', "?/"),
    "not_object":        ('[1,2,3]', "ERR"),
    "bad_json":          ('{not json', "ERR"),
}


@pytest.mark.parametrize("case", list(_ENVELOPE_CASES))
def test_py_parse_envelope_pure_revl(envelope_ir, case):
    wire, expected = _ENVELOPE_CASES[case]
    ns = _exec_py(envelope_ir)
    assert ns["parse_envelope"](wire) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("case", list(_ENVELOPE_CASES))
def test_ts_parse_envelope_matches_py(envelope_ir, case):
    wire, expected = _ENVELOPE_CASES[case]
    ns = _exec_py(envelope_ir)
    py_out = ns["parse_envelope"](wire)
    ts_out = _run_ts(
        envelope_ir,
        f"console.log(JSON.stringify(parse_envelope({json.dumps(wire)})));")
    assert py_out == ts_out == expected


# ======================== item 104 cross-tier: `.length` property form in a
# ======================== PROVIDE method (folded in with the field-read work;
# ======================== it hits the SAME per-tier field-access emitters).

# A provide-method body reaching a multibyte `Str` literal's `.length`. In a fn
# body `.length` folds to a `len` node; in a component/provide body the frontend
# used to spell it as a `field` node (name="length"), which each tier's
# field-access emitter had no case for — so `"café".length` raised at emit/
# runtime (py/java: AttributeError/EmitError) or would have counted UTF-8 BYTES
# rather than CODE POINTS. The frontend now folds the component-position form to
# the same `len` node, so every tier routes it through its established
# code-point length path. These three multibyte strings each have a code-point
# count that differs from their UTF-8 byte count, so a byte-length regression
# would be caught.
_LEN_STRINGS = {
    "cafe":     ("café",   4),   # é: 1 code point, 2 UTF-8 bytes
    "japanese": ("日本語", 3),   # 3 code points, 9 UTF-8 bytes
    "naive":    ("naïve",  5),   # ï: 1 code point, 2 UTF-8 bytes
}

_LEN_BACKENDS = ("python", "typescript", "go", "rust", "java", "wasm")


def _length_component_src(literal: str) -> str:
    # the literal is embedded RAW (not json.dumps) so the multibyte code points
    # reach the revl string literal directly — none of these contain a quote or
    # backslash, so no escaping is needed.
    assert '"' not in literal and "\\" not in literal
    return (
        "service Greeter {\n  fn hello() -> Int\n}\n"
        "component G provides greeter: Greeter {\n"
        "  provide greeter {\n"
        f'    fn hello() = "{literal}".length\n'
        "  }\n"
        "}\n"
    )


def _compile_src(src: str) -> dict:
    d = Path(tempfile.mkdtemp())
    (d / "consumer.rvl").write_text(src, encoding="utf-8")
    return compile_files([str(d / "consumer.rvl")])


def _find_nodes(ir, kind: str):
    found = []
    def walk(o):
        if isinstance(o, dict):
            if o.get("kind") == kind:
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(ir)
    return found


@pytest.mark.parametrize("case", list(_LEN_STRINGS))
def test_component_length_lowers_to_len_not_field(case):
    literal, _ = _LEN_STRINGS[case]
    ir = _compile_src(_length_component_src(literal))
    # the fix: a `len` node, and NO record-field read named "length"
    assert _find_nodes(ir, "len"), "component-position .length must lower to a len node"
    assert not [n for n in _find_nodes(ir, "field") if n.get("name") == "length"], \
        "no `field` node named length should survive lowering"


@pytest.mark.parametrize("case", list(_LEN_STRINGS))
def test_component_length_emits_on_every_tier(case):
    """Every one of the six field-access emitters renders the property-form
    `.length` in a provide method (no EmitError). This is the cross-tier half of
    item 104: wasm landed in 8d75a73, py/ts/go/rust/java are folded in here."""
    literal, _ = _LEN_STRINGS[case]
    ir = _compile_src(_length_component_src(literal))
    for backend in _LEN_BACKENDS:
        code = _emit(backend, ir)  # raises EmitError on regression
        assert code, f"{backend} produced empty output"


@pytest.mark.parametrize("case", list(_LEN_STRINGS))
def test_component_length_code_point_count_py(case):
    """The py tier's emitted provide method counts CODE POINTS. The emitted
    provide method is `return len(<literal>)` (the emitter escapes non-ASCII as
    ASCII-safe `\\uXXXX`, still a valid python str literal); python's `len` on a
    str is the code-point count, so evaluating that exact emitted expression
    reproduces the provide method's result."""
    literal, expected = _LEN_STRINGS[case]
    ir = _compile_src(_length_component_src(literal))
    code = _emit("python", ir)
    ret = next(line.strip()[len("return "):]
               for line in code.splitlines()
               if line.strip().startswith("return len("))
    assert eval(ret) == expected  # noqa: S307 — the emitter's own `len(<literal>)`


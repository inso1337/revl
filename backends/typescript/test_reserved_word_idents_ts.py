"""Roadmap item 165: a valid revl identifier that collides with a JS/TS
reserved word (`class`, `function`, `new`, `default`, …) no longer crashes the
TypeScript emitter — it is deterministically renamed (A3 append-`_`) at the
declaration site AND every use site, so the emitted module is valid TS.

`class` is a legal revl identifier (not a revl keyword), so
`fn f(class: Str) -> Str { return class }` type-checks and lowers, then used to
die here with `parameter name identifier is a reserved word: 'class'`.

These are toolchain-free checks (they run under the main venv, no `npm`); the
end-to-end RUN proof is `test_reserved_word_idents_ts` compiling and executing
the emitted module under Node, folded into the vitest suite.
"""

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("revl_ts_emit_rw", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit(src: str) -> str:
    return _load().emit(compile_source(src))


def test_mangle_is_pure_and_free():
    m = _load()
    assert m._mangle("class") == "class_"
    assert m._mangle("function") == "function_"
    # non-reserved names are the identity — the byte-identical guarantee
    assert m._mangle("value") == "value"
    assert m._mangle("handler") == "handler"
    # the mangled form is itself not reserved (the loop guarantees it)
    assert m._mangle("class") not in m.JS_RESERVED


def test_keyword_param_local_and_call_are_consistent():
    """A keyword param, a keyword local, and a cross-function call all rename to
    the same token, so decl and use agree in the emitted TS."""
    out = _emit(
        "fn probe(class: Str, new: Str) -> Str {\n"
        "  let function = class\n"
        "  return function\n"
        "}\n"
        "fn go(x: Str) -> Str { return probe(x, x) }\n"
    )
    assert "export function probe(class_: string, new_: string): string {" in out
    assert "const function_ = class_" in out
    assert "return function_" in out
    # the call site references the renamed callee/args, and no bare keyword leaks
    assert "return probe(x, x)" in out
    for bad in ("(class:", " class ", "const function =", "return function\n"):
        assert bad not in out


def test_keyword_field_record_roundtrips_in_emit():
    """A structural record with a keyword-named field renames the property
    uniformly — interface declaration, literal construction, and member access
    all use the same `class_` token, so the record round-trips within TS."""
    out = _emit(
        "type Box = { class: Str, default: Str }\n"
        "fn make(a: Str) -> Box { return { class: a, default: a } }\n"
        "fn unbox(b: Box) -> Str { return b.class }\n"
    )
    assert "class_: string" in out          # interface field
    assert "{class_: a, default_: a}" in out  # literal construction
    assert "return b.class_" in out          # member access
    # no bare reserved-word property leaked
    assert "class:" not in out and "b.class\n" not in out

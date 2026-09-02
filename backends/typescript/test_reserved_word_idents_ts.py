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
    """A structural record with a keyword-named field uses the RAW key uniformly
    — interface declaration, literal construction, and member access all carry
    the unrenamed `"class"` — so the record round-trips within TS AND matches the
    key a dynamic JSON value carries at runtime (item 279, superseding the item
    165 append-`_` rename for record FIELDS; bindings still mangle, above).

    A reserved-word field is emitted as a raw quoted key and reached by bracket
    access, exactly as the py tier keeps raw keys (`_revl_field(t, 'class')`),
    so the same record has one meaning on both tiers instead of a ts-only
    `class_` that a `json_parse` result could never produce."""
    out = _emit(
        "type Box = { class: Str, default: Str }\n"
        "fn make(a: Str) -> Box { return { class: a, default: a } }\n"
        "fn unbox(b: Box) -> Str { return b.class }\n"
    )
    assert '"class": string' in out            # interface field (raw key)
    assert '{"class": a, "default": a}' in out  # literal construction
    assert 'return b["class"]' in out           # member access (bracket)
    # the append-`_` rename must NOT leak onto a record field any more
    assert "class_" not in out and "default_" not in out


# --------------------------------------------------------------------------
# Injectivity: the rename must never merge two distinct revl identifiers.
#
# The item-165 rename was "append `_` while the name is reserved", which is a
# pure function of the name (what decl/use agreement needs) but NOT injective:
# `function` maps to `function_` and the equally legal revl identifier
# `function_` maps to itself, so both reach `function_`. On this tier that
# breaks loudly (`node --check`: "Identifier 'function_' has already been
# declared"); on the python tier the same shape silently captures. The rule is
# injective on every tier now, and these tests pin it here.
# --------------------------------------------------------------------------


def test_mangle_is_injective_over_the_reserved_ladder():
    m = _load()
    seen: dict[str, str] = {}
    for word in sorted(m.JS_RESERVED):
        for name in (word, word + "_", word + "__", word + "___"):
            out = m._mangle(name)
            assert out not in m.JS_RESERVED, f"{name!r} mangled to reserved {out!r}"
            assert out not in seen, f"{name!r} and {seen[out]!r} both mangle to {out!r}"
            seen[out] = name


def test_reserved_local_does_not_collide_with_its_underscore_twin():
    """Two distinct revl locals, `function` and `function_`, stay two distinct
    `const` declarations. Before the fix both were `const function_`, which the
    JS parser rejects outright."""
    out = _emit(
        'pub fn probe() -> Str {\n'
        '  let function = "PUBLIC-VALUE"\n'
        '  let function_ = "SEKRIT-CANARY-416"\n'
        '  return function\n'
        '}\n'
    )
    assert 'const function_ = "PUBLIC-VALUE"' in out
    assert 'const function__ = "SEKRIT-CANARY-416"' in out
    assert "return function_" in out
    assert out.count("const function_ =") == 1


def test_top_level_fn_pair_stays_two_exports():
    out = _emit(
        'pub fn class() -> Str { return "PUBLIC-VALUE" }\n'
        'pub fn class_() -> Str { return "SEKRIT-CANARY-416" }\n'
    )
    assert out.count("export function class_(") == 1
    assert out.count("export function class__(") == 1


def test_record_field_pair_keeps_both_raw_keys():
    """A record field is never `_mangle`d — not even by the injective ladder
    shift — because the emitted key must equal the raw revl field name that the
    runtime value is keyed by (item 279). `function` stays a raw quoted key and
    `function_` stays a raw bare key, so the two fields stay two fields."""
    out = _emit(
        "type Box = { function: Str, function_: Str }\n"
        "fn mk(a: Str, b: Str) -> Box { return { function: a, function_: b } }\n"
        "fn read1(b: Box) -> Str { return b.function }\n"
        "fn read2(b: Box) -> Str { return b.function_ }\n"
    )
    assert '"function": string' in out
    assert "  function_: string" in out
    assert '{"function": a, function_: b}' in out
    assert 'return b["function"]' in out
    assert "return b.function_" in out
    # the ladder shift must not reach a field name
    assert "function__" not in out


def test_ordinary_reserved_rename_is_unchanged():
    """False-positive guard: a reserved-word rename with no twin in the program
    is exactly what it always was — one `_`, not two."""
    out = _emit("fn f(class: Str) -> Str { let new = class\n  return new }")
    assert "export function f(class_: string)" in out
    assert "const new_ = class_" in out
    assert "class__" not in out and "new__" not in out


def test_non_reserved_underscore_names_are_untouched():
    """False-positive guard: `value_` has no reserved root, so it is verbatim."""
    out = _emit("fn g(value_: Str) -> Str { let out_ = value_\n  return out_ }")
    assert "export function g(value_: string)" in out
    assert "const out_ = value_" in out
    assert "value__" not in out and "out__" not in out

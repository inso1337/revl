"""Roadmap item 165: a valid revl identifier that collides with a *Rust*
reserved word (`impl`, `move`, `loop`, `struct`, `ref`, `fn`, …) no longer
crashes the Rust emitter — it is deterministically renamed (A3 append-`_`) at
the declaration site AND every use site, so the emitted crate is valid Rust.

`impl` is a legal revl identifier (not a revl keyword), so
`fn f(impl: Str) -> Str { return impl }` type-checks and lowers, then used to
die here with `parameter name identifier collides with Rust/reserved name`.

Toolchain-free emit assertions (run under the main venv, no cargo). The RUN
proof (`rustc` compiling and executing the emitted functions) is recorded in
the item-165 report; the runtime-scenario cargo harness is unchanged.
"""

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_rust_emit_rw", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)


def _emit(src: str) -> str:
    return emit.emit(compile_source(src))


def test_mangle_is_pure_and_free():
    assert emit._mangle("impl") == "impl_"
    assert emit._mangle("move") == "move_"
    assert emit._mangle("loop") == "loop_"
    # non-reserved names are the identity — the byte-identical guarantee
    assert emit._mangle("value") == "value"
    assert emit._mangle("impl") not in emit._RUST_RESERVED


def test_keyword_param_local_and_call_are_consistent():
    out = _emit(
        "fn probe(impl: Str, move: Str) -> Str {\n"
        "  let loop = impl\n"
        "  return loop\n"
        "}\n"
        "fn go(x: Str) -> Str { return probe(x, x) }\n"
    )
    assert "fn probe(impl_: String, move_: String) -> String {" in out
    assert "let loop_ = impl_;" in out
    assert "return loop_;" in out
    # cross-fn call target/args reference the renamed names, no bare keyword
    assert "probe(x.clone(), x.clone())" in out
    for bad in ("(impl:", " impl_ =", "let loop =", "return loop;"):
        assert bad not in out


def test_keyword_field_record_is_consistent():
    """A record struct with keyword-named fields renames uniformly — struct
    declaration, construction, and access all use the same `impl_` token."""
    out = _emit(
        "type Box = { impl: Str, struct: Str }\n"
        "fn make(a: Str) -> Box { return { impl: a, struct: a } }\n"
        "fn unbox(b: Box) -> Str { return b.impl }\n"
    )
    assert "impl_: String," in out          # struct field decl
    assert "impl_:" in out                   # construction
    assert "return b.impl_;" in out          # field access


# --------------------------------------------------------------------------
# Injectivity. `_mangle` was "append `_` while reserved" and `_mname` was a
# plain table lookup: both pure functions of the name, neither injective —
# `match`/`match_` both landed on `match_`, and a method named `drop_` collided
# with the `drop` -> `drop_` destructor rename. rustc catches most of that
# loudly; the python tier silently captures on the same shape, so the rule is
# injective on every tier now.
# --------------------------------------------------------------------------


def test_mangle_is_injective_over_the_reserved_ladder():
    reserved = emit._RUST_RESERVED | emit._EMITTER_RESERVED
    for word in sorted(reserved):
        ladder = [word, word + "_", word + "__", word + "___"]
        images = [emit._mangle(n) for n in ladder]
        assert len(set(images)) == len(ladder), f"{word!r} ladder collapsed: {images}"
        assert not set(images) & reserved


def test_method_rename_is_injective():
    """`drop` -> `drop_` must not swallow a method actually named `drop_`."""
    assert emit._mname("drop") == "drop_"
    assert emit._mname("drop_") == "drop__"
    assert emit._mname("drop__") == "drop___"
    assert emit._mname("run") == "run"
    assert len({emit._mname(n) for n in ("drop", "drop_", "drop__")}) == 3


def test_keyword_local_does_not_collide_with_its_underscore_twin():
    out = _emit(
        'pub fn probe() -> Str {\n'
        '  let const = "PUBLIC-VALUE"\n'
        '  let const_ = "SEKRIT-CANARY-416"\n'
        '  return const\n'
        '}\n'
    )
    assert 'let const_ = String::from("PUBLIC-VALUE");' in out
    assert 'let const__ = String::from("SEKRIT-CANARY-416");' in out
    assert "return const_;" in out
    assert out.count("let const_ =") == 1


def test_top_level_fn_pair_stays_two_functions():
    out = _emit(
        'pub fn const() -> Str { return "PUBLIC-VALUE" }\n'
        'pub fn const_() -> Str { return "SEKRIT-CANARY-416" }\n'
    )
    assert out.count("pub fn const_()") == 1
    assert out.count("pub fn const__()") == 1


def test_record_field_pair_stays_two_fields():
    out = _emit(
        "type Box = { const: Str, const_: Str }\n"
        "fn mk(a: Str, b: Str) -> Box { return { const: a, const_: b } }\n"
        "fn r1(b: Box) -> Str { return b.const }\n"
        "fn r2(b: Box) -> Str { return b.const_ }\n"
    )
    assert "const_: String," in out
    assert "const__: String," in out
    assert "return b.const_;" in out
    assert "return b.const__;" in out


def test_ordinary_reserved_rename_is_unchanged():
    """False-positive guard: one `_`, not two, when there is no twin."""
    out = _emit("pub fn f(impl: Str) -> Str { let struct = impl\n  return struct }")
    assert "impl_: " in out
    assert "let struct_ = impl_" in out
    assert "impl__" not in out and "struct__" not in out


def test_non_reserved_underscore_names_are_untouched():
    out = _emit("pub fn g(value_: Str) -> Str { let out_ = value_\n  return out_ }")
    assert "value_: " in out
    assert "let out_ = value_" in out
    assert "value__" not in out and "out__" not in out

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

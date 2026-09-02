"""Roadmap item 165: a valid revl identifier that collides with a *Go* reserved
word (`func`, `range`, `map`, `chan`, `type`-adjacent, …) emits and RUNS on the
Go tier. The Go emitter already renames such identifiers (`_v3_ident` appends
`_`) at the declaration site AND every use site; this suite pins that behavior
so a regression is caught, and — where a real Go toolchain is present — proves
the emitted package builds.

Go was already correct for this (unlike python/typescript/rust/java, which had
to be fixed); these tests are the durable guard that keeps it correct.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_go_emit_rw", HERE / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)


PROGRAM = """
type Box = { func: Str, range: Str }
fn probe(func: Str, chan: Str) -> Str {
  let range = func
  return range
}
fn unbox(b: Box) -> Str { return b.func }
pub fn run(x: Str) -> Str { return probe(x, x) }
"""


def test_v3_ident_renames_go_keywords():
    assert emit._v3_ident("func", "x") == "func_"
    assert emit._v3_ident("range", "x") == "range_"
    assert emit._v3_ident("value", "x") == "value"   # identity off the keyword set


def test_keyword_decls_and_uses_are_consistent():
    out = emit.emit(compile_source(PROGRAM))
    # Params and locals that collide with a Go keyword get the `_` suffix at the
    # declaration site and every use (item 165).
    assert "func_ string" in out                  # keyword-named param
    assert "range_ := func_" in out               # local decl from keyword param
    assert "return range_" in out
    # Record struct fields are EXPORTED (UpperCamel) with a `json:"<revl>"` tag
    # preserving the source name (item 390); capitalizing sidesteps the keyword
    # set, so `func`/`range` become `Func`/`Range` at the declaration and every
    # access, keeping record round-trips byte-consistent across tiers.
    assert 'Func string `json:"func"`' in out     # struct field decl
    assert 'Range string `json:"range"`' in out
    assert "return b.Func" in out                 # field access uses the exported spelling
    assert "return probe(x, x)" in out            # cross-fn call
    # no bare reserved word survives as an identifier
    assert "func func(" not in out and "b.func\n" not in out


def test_go_build_accepts_keyword_named_identifiers():
    """Definition-of-done pin: the emitted package compiles under a real Go
    toolchain (skips cleanly when Go is unavailable)."""
    sys.path.insert(0, str(ROOT / "tools"))
    from validate import GoValidator  # noqa: PLC0415

    validator = GoValidator()
    reason = validator.unavailable()
    if reason:
        pytest.skip(reason)
    src = emit.emit(compile_source(PROGRAM), package="reservedwords")
    results = validator.check([("reserved-word-idents", src)])
    status, detail = results["reserved-word-idents"]
    assert status == "ok", detail


# --------------------------------------------------------------------------
# Injectivity. The rename was "reserved -> name + '_'", a pure function of the
# name but NOT injective: `func` and the equally legal revl identifier `func_`
# both landed on `func_`. On this tier that breaks loudly (`go build`: "no new
# variables on left side of :="); on the python tier the same shape silently
# captures, so the rule is injective on every tier now.
# --------------------------------------------------------------------------

LOCALS = """
pub fn probe() -> Str {
  let func = "PUBLIC-VALUE"
  let func_ = "SEKRIT-CANARY-416"
  return func
}
"""

FNS = """
pub fn func() -> Str { return "PUBLIC-VALUE" }
pub fn func_() -> Str { return "SEKRIT-CANARY-416" }
"""


def test_renames_are_injective_over_the_keyword_ladder():
    for word in sorted(emit._GO_RESERVED):
        ladder = [word, word + "_", word + "__", word + "___"]
        images = [emit._v3_ident(n, "x") for n in ladder]
        assert len(set(images)) == len(ladder), f"{word!r} ladder collapsed: {images}"
        assert not set(images) & emit._GO_RESERVED
    for word in sorted(emit._GO_KEYWORDS):
        ladder = [word, word + "_", word + "__", word + "___"]
        images = [emit._safe_local(n) for n in ladder]
        assert len(set(images)) == len(ladder), f"{word!r} ladder collapsed: {images}"
        assert not set(images) & emit._GO_KEYWORDS


def test_keyword_local_does_not_collide_with_its_underscore_twin():
    out = emit.emit(compile_source(LOCALS))
    assert 'func_ := "PUBLIC-VALUE"' in out
    assert 'func__ := "SEKRIT-CANARY-416"' in out
    assert "return func_" in out
    assert out.count('func_ := ') == 1


def test_top_level_fn_pair_stays_two_functions():
    out = emit.emit(compile_source(FNS))
    assert out.count("func func_()") == 1
    assert out.count("func func__()") == 1


def test_record_field_pair_is_refused_loudly():
    """Go record fields are EXPORTED (`Func`), so `func`/`func_` collide under
    the exported-name mapping rather than under the keyword rename. That path
    already refuses instead of emitting one field twice; this pins the refusal
    so the go tier never silently drops a field the way python did."""
    src = ("type Box = { func: Str, func_: Str }\n"
           "fn mk(a: Str, b: Str) -> Box { return { func: a, func_: b } }\n")
    with pytest.raises(emit.EmitError, match="both lower to the exported Go field"):
        emit.emit(compile_source(src))


def test_ordinary_keyword_rename_is_unchanged():
    """False-positive guard: one `_`, not two, when there is no twin."""
    out = emit.emit(compile_source(
        "pub fn f(func: Str) -> Str { let range = func\n  return range }"))
    assert "func_ string" in out
    assert "range_ := func_" in out
    assert "func__" not in out and "range__" not in out


def test_non_keyword_underscore_names_are_untouched():
    out = emit.emit(compile_source(
        "pub fn g(value_: Str) -> Str { let out_ = value_\n  return out_ }"))
    assert "value_ string" in out
    assert "out_ := value_" in out
    assert "value__" not in out and "out__" not in out

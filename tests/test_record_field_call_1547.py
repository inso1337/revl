"""A method call on a record is refused in a component body. Issue #1547.

`let r = { f: twice }` then `r.f(n)` in a provide method compiled, and on the
py tier it crashed at run time with `AttributeError: 'dict' object has no
attribute 'f'`: the emitter renders records as dicts, and the lowering had
passed the call through as a generic method call that no emitter can tell
from a host-object call. In a `fn` body the same call was a compile error all
along ("no builtin method `f` on values"); the component-method lowering
refused only a Str/List/Bytes receiver.

Decided (option A): refuse the shape in component bodies with the `fn` body's
message and hint, so every tier rejects it at compile time. The documented
spelling, `let g = r.f` then `g(n)`, keeps working, and the py rows RUN it.

tests/fixtures/record_field_call/ is the corpus: `t1_` refused (the message
family the census tags T1), `ok_` admitted. tests/test_gate_reference_census.py
holds the self-host gate to the same verdicts.
"""

from pathlib import Path
import sys

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402

CORPUS = ROOT / "tests" / "fixtures" / "record_field_call"
_XT = load_by_path("test_cross_tier_execution",
                   ROOT / "tests" / "test_cross_tier_execution.py")

_SURFACE = ("charAt, charCodeAt, checked_div_euclid, checked_div_floor, "
            "checked_div_trunc, checked_mod, codepoint_at, concat, div_euclid, "
            "div_floor, div_trunc, endsWith, field, has, indexOf, is_alnum, "
            "is_alpha, is_digit, is_space, join, keys, length, list, lookup, "
            "mod, push, remove, repeat, set, size, slice, split, startsWith, "
            "str, to_int, to_int32, to_str")


def _message(method: str) -> str:
    return (f"no builtin method `{method}` on values — the stdlib surface is "
            f"{_SURFACE} (docs/stdlib-2.0.md)")


REFUSED = {
    "t1_let": "f", "t1_return": "f", "t1_nested_field": "f",
    "t1_arrow_field": "f", "t1_in_place": "f", "t1_declared_type": "f",
}
ADMITTED = ("ok_let_binding", "ok_field_read", "ok_in_place_binding")


def _src(stem: str) -> str:
    return (CORPUS / f"{stem}.rvl").read_text()


def test_the_corpus_is_the_refusals_and_the_admissions():
    assert sorted(p.stem for p in CORPUS.glob("*.rvl")) == sorted(
        list(REFUSED) + list(ADMITTED))


@pytest.mark.parametrize("stem", sorted(REFUSED))
def test_a_method_call_on_a_record_is_refused_in_a_component(stem):
    with pytest.raises(RevlError) as excinfo:
        compile_source(_src(stem), f"{stem}.rvl")
    assert excinfo.value.message == _message(REFUSED[stem])
    assert excinfo.value.hint == (
        "records carry data, not methods; call functions as `f(x)`, and call "
        "arrows through a `let` binding")


def test_the_component_message_is_the_fn_body_message():
    """The same call in a `fn` body, refused as it always was: the component
    refusal is byte-identical to it."""
    src = ("extern pure fn twice(n: Int) -> Int = @py { return n * 2 }\n"
           "fn h(n: Int) -> Int {\n  let r = { f: twice }\n  return r.f(n)\n}\n"
           "service S { fn go(n: Int) -> Int }\n"
           "component C provides s: S {\n  provide s { fn go(n: Int) = h(n) }\n}\n")
    with pytest.raises(RevlError) as fn_body:
        compile_source(src, "t.rvl")
    with pytest.raises(RevlError) as component:
        compile_source(_src("t1_let"), "t.rvl")
    assert fn_body.value.message == component.value.message
    assert fn_body.value.hint == component.value.hint


@pytest.mark.parametrize("stem", ADMITTED)
def test_the_documented_spellings_compile(stem):
    assert compile_source(_src(stem), f"{stem}.rvl")


_LIFECYCLE = """
lifecycle test "the function read off the field is called through a binding" {
  load C
  let a = call s.go(5)
  assert a == 10
  unload C
  assert no_residue
}
"""


def test_the_let_binding_spelling_returns_the_right_value_on_py():
    """The documented spelling, run on the py tier (the one tier whose plain
    test suite executes a live composition)."""
    pytest.importorskip("cordis", reason="cordis-py runtime not installed")
    status, message = _XT._run("py", _src("ok_let_binding") + _LIFECYCLE)
    assert status == "pass", message

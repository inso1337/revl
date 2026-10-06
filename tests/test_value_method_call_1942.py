"""A method outside the builtin table on a stdlib value is refused in a
component body, however the receiver is written. Issue #1942.

`[1, 2].map((x) => x + n)` in a provide method compiled, and on the py tier it
crashed at run time: the emitter renders the call as `getattr([1, 2], 'map')`.
The component lowering checked only a NAMED receiver, and only one typed
Str/List/Bytes, so a receiver written in place (a list literal, a
parenthesised value, a call's result) and a named Int/Float/Bool/Map passed
through as a generic method call no tier defines. A `fn` body refuses every
such call already.

The rule (`lower._refuse_value_method`) is one for both spellings: a
non-builtin method on a receiver whose static type is List, Str, Bytes, Int,
Int32, Float or Bool is refused with the named receiver's message. A receiver
with no static type (an arrow parameter) stays lenient, as before.

`Map` joined the refused heads in issue #1968 and is left to its own file,
`tests/test_value_map_method_1968.py`. It is the one value head a host handle
shares its name with, so the two cannot be told apart by the type the rule
reads; `lower.Env.host_handles` records which bindings an `effect` bracket
made a handle and `_refuse_value_method` stands aside for those, so a
`Map[K, V]`-typed acquisition keeps its host verbs (`drop`, `insert`, `get`).
`ok_typed_host_acquisition` and `ok_host_handle_verbs` here are that guard.

tests/fixtures/value_method_call/ is the corpus: `t1_` refused, `ok_`
admitted. tests/test_gate_reference_census.py holds the self-host gate to the
same verdicts.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "value_method_call"

_SURFACE = ("charAt, charCodeAt, checked_div_euclid, checked_div_floor, "
            "checked_div_trunc, checked_mod, codepoint_at, concat, div_euclid, "
            "div_floor, div_trunc, endsWith, field, has, indexOf, is_alnum, "
            "is_alpha, is_digit, is_space, join, keys, length, list, lookup, "
            "mod, push, remove, repeat, set, size, slice, split, startsWith, "
            "str, to_int, to_int32, to_str")


def _message(method: str, recv: str) -> str:
    return (f"no builtin method `{method}` on `{recv}` — the stdlib surface is "
            f"{_SURFACE} (docs/stdlib-2.0.md)")


REFUSED = {
    "t1_inplace_effect_arg": ("frob", "List[Int]"),
    "t1_inplace_list": ("map", "List[Int]"),
    "t1_inplace_map": ("frob", "Map[Str, Int]"),
    "t1_inplace_paren_int": ("frob", "Int"),
    "t1_inplace_req_result": ("map", "List[Int]"),
    "t1_inplace_str_call": ("frob", "Str"),
    "t1_named_int": ("frob", "Int"),
    "t1_named_list": ("map", "List[Int]"),
    "t1_named_map": ("frob", "Map[Str, Int]"),
    "t1_record_map_field": ("frob", "Map[Str, Int]"),
}
ADMITTED = ("ok_arrow_param", "ok_host_handle_verbs", "ok_host_map",
            "ok_inplace_builtin", "ok_typed_host_acquisition")


def _src(stem: str) -> str:
    return (CORPUS / f"{stem}.rvl").read_text()


def test_the_corpus_is_the_refusals_and_the_admissions():
    assert sorted(p.stem for p in CORPUS.glob("*.rvl")) == sorted(
        list(REFUSED) + list(ADMITTED))


@pytest.mark.parametrize("stem", sorted(REFUSED))
def test_a_non_builtin_method_on_a_stdlib_value_is_refused(stem):
    with pytest.raises(RevlError) as excinfo:
        compile_source(_src(stem), f"{stem}.rvl")
    assert excinfo.value.message == _message(*REFUSED[stem])
    assert excinfo.value.hint == (
        "records carry data, not methods; call functions as `f(x)` (G6)")


@pytest.mark.parametrize("stem", ADMITTED)
def test_the_reference_admits_the_document(stem):
    compile_source(_src(stem), f"{stem}.rvl")


@pytest.mark.parametrize("receiver", ["[1, 2]", "(3)", "\"s\".concat(\"t\")"])
def test_the_fn_body_refused_these_receivers_already(receiver):
    """The `fn` body has always refused a non-builtin method on any receiver;
    the component rule closes the gap to it rather than adding a new rule."""
    src = f"fn f(n: Int) -> Int {{ return {receiver}.frob(n) }}"
    with pytest.raises(RevlError) as excinfo:
        compile_source(src)
    assert excinfo.value.message.startswith("no builtin method `frob` on values")

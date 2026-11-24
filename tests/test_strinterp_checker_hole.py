"""Roadmap item 380: string interpolation in a non-backtick string is
SILENT-WRONG, and it exposes a type-checker SOUNDNESS HOLE.

Two independent defects, both fixed here:

(A) The soundness hole (the serious one). `return <a function value>` where the
    declared return type is a non-function type (e.g. `Str`) type-checked as
    ANY declared return type: a bare reference to a top-level `fn` inferred to
    `None` (unknown), and `None` short-circuits every downstream compatibility
    check. `fn f() -> Str { return f }` compiled clean and silently returned
    the function value where a `Str` was declared. The fix types a bare `fn`
    reference as its function type so the mismatch is refused — while a return
    of a correctly function-typed value (item 92/342) still compiles.

(B) The interpolation diagnostic. `"hi ${name}"` compiled clean and emitted the
    LITERAL `${name}`; a leading `f"..."` parsed as `return f` (an identifier)
    plus a dead string. Both are now redirected to revl's backtick template.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.lexer import lex  # noqa: E402


def _err(source: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    return str(excinfo.value)


# -- (A) the soundness hole -------------------------------------------------

def test_return_a_function_value_where_str_is_declared_is_refused():
    # THE soundness hole, exactly as the roadmap describes it. `f` in scope is
    # the function itself; returning it where `Str` is declared must be a
    # type-mismatch, not a silent miscompile to exit 0.
    err = _err("fn f() -> Str { return f }\n")
    assert "expects `Str`" in err and "() -> Str" in err


def test_return_a_named_fn_where_str_is_declared_is_refused():
    # the same hole with a distinct name and a parameter, so the message names
    # the real function type of the returned value.
    err = _err("fn inc(n: Int) -> Int { return n + 1 }\n"
               "fn g() -> Str { return inc }\n")
    assert "expects `Str`" in err and "(Int) -> Int" in err


def test_passing_a_wrong_signature_fn_by_name_is_refused():
    # the SAME root cause on the argument path: a bare fn name passed where a
    # function type is expected was never checked (its inferred type was None).
    err = _err("fn apply(g: (Int) -> Int, x: Int) -> Int { return g(x) }\n"
               "fn wrong(s: Str) -> Str { return s }\n"
               "fn demo(n: Int) -> Int { return apply(wrong, n) }\n")
    assert "(Int) -> Int" in err and "(Str) -> Str" in err


def test_returning_a_correctly_function_typed_fn_still_compiles():
    # item 92/342: a bare fn name whose type MATCHES the declared function
    # return type must still compile — the fix refuses mismatches, not returns.
    compile_source("fn inc(n: Int) -> Int { return n + 1 }\n"
                   "fn get_inc() -> (Int) -> Int { return inc }\n", "t.rvl")


def test_passing_a_correctly_typed_fn_by_name_still_compiles():
    compile_source("fn apply(g: (Int) -> Int, x: Int) -> Int { return g(x) }\n"
                   "fn inc(n: Int) -> Int { return n + 1 }\n"
                   "fn demo(n: Int) -> Int { return apply(inc, n) }\n", "t.rvl")


# -- (B) the interpolation diagnostic ---------------------------------------

def test_dollar_brace_in_a_plain_string_is_redirected_to_a_template():
    # `"hi ${name}"` used to compile clean and emit the LITERAL `${name}`. It is
    # now a refusal that says so and names both working forms (#3502): a
    # triple-quoted string for literal text, a backtick template to interpolate.
    err = _err('fn greet(name: Str) -> Str { return "hi ${name}" }\n')
    assert "does not interpolate" in err
    assert "refused, not emitted as literal text" in err
    assert "triple-quoted string" in err and '"""..."""' in err
    assert "backtick template" in err and "`hi ${name}`" in err


def test_f_string_prefix_is_redirected_to_a_template():
    # `f"hi {name}"` parsed as `return f` plus a dead string; with an `f` in
    # scope it silently miscompiled. It is now a diagnostic.
    err = _err('fn greet(name: Str) -> Str { return f"hi {name}" }\n')
    assert "f-string" in err
    assert "backtick template" in err and "`hi ${name}`" in err


def test_f_string_is_caught_even_when_an_f_is_in_scope():
    # the exact soundness scenario from the roadmap: an `f` in scope made the
    # f-string parse-and-typecheck clean. The lexer diagnostic fires first, so
    # it can never reach that miscompile.
    err = _err("fn f(name: Str) -> Str { return f\"hi {name}\" }\n")
    assert "f-string" in err


def test_backtick_template_still_compiles():
    # the redirect target must actually be valid revl.
    compile_source("fn greet(name: Str) -> Str { return `hi ${name}` }\n", "t.rvl")


def test_a_plain_string_quoting_a_template_as_data_is_not_flagged():
    # a plain string that carries a backtick is deliberately quoting template
    # source as DATA (the selfhost lexer/parser fixtures) — not a mistake.
    compile_source('fn t() -> Str { return "`hi ${name}!`" }\n', "t.rvl")


def test_a_dollar_brace_fragment_without_a_close_brace_is_not_flagged():
    # the ts emitter assembles template literals from fragments like `"${"`;
    # an incomplete `${` shape is not a mistaken interpolation.
    compile_source('fn t() -> Str { return "${" }\n', "t.rvl")


def test_literal_braces_in_a_plain_string_are_not_flagged():
    # `{}` / `{value}` as literal text (JSON, emit placeholders) must survive.
    compile_source('fn t() -> Str { return "{value}L" }\n', "t.rvl")
    compile_source('fn t() -> Str { return "{}" }\n', "t.rvl")


def test_literal_dollar_brace_is_written_as_a_triple_quoted_string():
    # the refusal's hint names the literal spelling, and it works: the shell
    # fragment from #3502 keeps its `${HOME}` characters verbatim.
    ir = compile_source('fn env() -> Str { return """echo ${HOME}""" }\n', "t.rvl")
    assert ir is not None


def test_a_template_has_no_escape_processing_so_two_backslashes_are_two():
    # #3503's rule: a template body is verbatim except for `${...}`, so `\\`
    # (two source backslashes) is two characters, where a plain string's `\\`
    # is one.
    toks = lex("fn f() -> Str { return `a\\\\b` }\n", "t.rvl")
    tmpl = next(t for t in toks if t.kind == "template")
    assert tmpl.value == [("text", "a\\\\b")]


def test_a_single_template_backslash_is_one_character():
    # to build a regex `\(x`, the template takes ONE backslash, not two.
    toks = lex("fn f() -> Str { return `\\(` }\n", "t.rvl")
    tmpl = next(t for t in toks if t.kind == "template")
    assert tmpl.value == [("text", "\\(")]

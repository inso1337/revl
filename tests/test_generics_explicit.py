"""Explicit generic declaration syntax (roadmap item 6).

revl already had *implicit* type parameters: a single-uppercase name in a
`fn`/`extern` signature that is not a declared type is that function's type
parameter (see test_typesafety.py, "generic instantiation"). This suite pins
the *explicit* form:

    fn id[T](x: T) -> T { return x }
    fn map_[A, B](xs: List[A], f: (A) -> B) -> List[B] { ... }

Guarantees under test:
  - `[T]` after a fn/extern name is accepted and its names feed the *same*
    unify machinery as the implicit form;
  - the explicit form is a strict superset — it can name a parameter the
    single-uppercase heuristic would miss (`[Elem]`);
  - a mismatch at a call site is still a compile error;
  - a declared `type S = A | B` is still checked (no wildcard regression), and
    an explicit `[S]` that shadows a declared type is *rejected*, not allowed;
  - the type-parameter marker never reaches the IR — an implicit and an
    explicit generic emit byte-identical IR;
  - a type parameter inside a function-type annotation unifies positionally.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402


def _err(source: str) -> str:
    with pytest.raises(RevlError) as excinfo:
        compile_source(source)
    return str(excinfo.value)


# ---- accepted and unified --------------------------------------------------

def test_explicit_type_parameter_is_accepted_and_unified():
    ir = compile_source("fn id[T](x: T) -> T { return x }\n"
                        "fn g() -> Int { return id(5) }")
    assert len(ir["functions"]) == 2


def test_explicit_return_is_instantiated_at_the_call_site():
    err = _err("fn id[T](x: T) -> T { return x }\n"
               'fn g() -> Int { return id("hello") }')
    assert "this function's return expects `Int`, got `Str`" in err


def test_explicit_parameters_must_agree_across_arguments():
    err = _err("fn pair[T](a: T, b: T) -> T { return a }\n"
               'fn g() -> Int { return pair(1, "x") }')
    assert "argument 2 of `pair(...)` expects `Int`, got `Str`" in err


def test_explicit_under_a_constructor_is_instantiated():
    err = _err("fn head[T](xs: List[T]) -> T { return xs[0] }\n"
               "fn g() -> Str { return head([1, 2]) }")
    assert "this function's return expects `Str`, got `Int`" in err


def test_multiple_explicit_parameters_do_not_collide():
    ir = compile_source("fn fst[A, B](a: A, b: B) -> A { return a }\n"
                        'fn g() -> Int { return fst(1, "x") }')
    assert len(ir["functions"]) == 2


# ---- the superset property: names the implicit heuristic would miss --------

def test_explicit_names_a_parameter_the_heuristic_would_miss():
    # `Elem` is multi-character, so the single-uppercase heuristic never treats
    # it as implicit — only the explicit `[Elem]` makes it a type parameter.
    ir = compile_source("fn ident[Elem](x: Elem) -> Elem { return x }\n"
                        "fn g() -> Int { return ident(5) }")
    assert len(ir["functions"]) == 2


def test_without_explicit_a_multichar_name_stays_nominal():
    # The counterpart proving the superset is real: without `[Elem]`, `Elem` is
    # an ordinary nominal type, so an Int argument is a definite mismatch.
    err = _err("fn ident(x: Elem) -> Elem { return x }\n"
               "fn g() -> Int { let y: Int = ident(5)\n  return y }")
    assert "argument 1 of `ident(...)` expects `Elem`, got `Int`" in err


# ---- no wildcard regression + shadowing decision ---------------------------

def test_declared_type_is_still_checked_alongside_explicit_generics():
    # A declared `type S = A | B` must stay checked even though the file also
    # uses the explicit generic form. The implicit-generics fix closed the hole
    # where any one-letter name was silently wildcarded; explicit `[T]` must
    # not reopen it.
    err = _err("type S = A | B\n"
               "fn id[T](x: T) -> T { return x }\n"
               "fn f(s: S) -> Int { return s }")
    assert "this function's return expects `Int`, got `S`" in err


def test_explicit_parameter_shadowing_a_declared_type_is_rejected():
    # Shadowing is rejected, not allowed: a `[...]` name may not collide with a
    # declared type. See docs/generics.md for the rationale.
    err = _err("type S = A | B\nfn f[S](x: S) -> S { return x }")
    assert "type parameter `S` shadows a declared type" in err


def test_explicit_parameter_shadowing_a_builtin_type_is_rejected():
    err = _err("fn f[Int](x: Int) -> Int { return x }")
    assert "type parameter `Int` shadows a builtin type" in err


def test_duplicate_explicit_parameter_is_rejected():
    err = _err("fn f[T, T](x: T) -> T { return x }")
    assert "duplicate type parameter `T`" in err


def test_empty_type_parameter_list_is_rejected():
    err = _err("fn f[](x: Int) -> Int { return x }")
    assert "empty type-parameter list" in err


# ---- IR is byte-identical to the implicit form -----------------------------

def test_explicit_and_implicit_emit_identical_ir():
    explicit = compile_source("fn id[T](x: T) -> T { return x }\n"
                              'fn g() -> Str { return id("hi") }')
    implicit = compile_source("fn id(x: T) -> T { return x }\n"
                              'fn g() -> Str { return id("hi") }')
    assert explicit["functions"] == implicit["functions"]


def test_explicit_type_parameter_marker_never_reaches_the_ir():
    ir = compile_source("fn id[T](x: T) -> T { return x }\n"
                        'fn g() -> Str { return id("hi") }')
    assert ir["functions"][0]["returns"] == "T"
    assert ir["functions"][0]["params"] == [{"name": "x", "type": "T"}]


# ---- externs share the machinery -------------------------------------------

def test_extern_takes_an_explicit_type_parameter_list():
    ir = compile_source(
        "extern pure fn identity[T](x: T) -> T\n  = @python { return x }\n"
        "fn g() -> Int { return identity(5) }")
    # the call type-checks and unifies; the extern's own signature is generic
    assert any(f["name"] == "g" for f in ir["functions"])


def test_extern_explicit_return_is_instantiated_at_the_call_site():
    err = _err(
        "extern pure fn identity[T](x: T) -> T\n  = @python { return x }\n"
        'fn g() -> Int { return identity("hi") }')
    assert "this function's return expects `Int`, got `Str`" in err


# ---- function-type positional unification (higher-order) -------------------

def test_higher_order_generic_unifies_positionally():
    # `A` and `B` are learned positionally: `A` from `xs`, and the function
    # type `(A) -> B` unifies against the passed function elementwise.
    ir = compile_source(
        "fn map_[A, B](xs: List[A], f: (A) -> B) -> List[B] { return xs }\n"
        "fn g() -> List[Int] { return map_([1, 2], (n) => n + 1) }")
    assert any(f["name"] == "map_" for f in ir["functions"])


def test_type_parameter_inside_a_structural_position_conflicts():
    # `A` appears inside `List[A]` and again bare; unifying `List[Int]` binds
    # `A = Int`, so a `Str` second argument is a definite conflict. This is the
    # same positional recursion a function-type argument uses.
    err = _err("fn head_or[A](xs: List[A], dflt: A) -> A { return dflt }\n"
               'fn g() -> Int { return head_or([1, 2], "s") }')
    assert "argument 2 of `head_or(...)` expects `Int`, got `Str`" in err


# ---- provide-methods deliberately do not take the list ---------------------

def test_service_method_does_not_take_a_type_parameter_list():
    # Methods are not entries in the shared fn/extern signature table, so the
    # explicit list is scoped out for them; `[` after the name still fails.
    err = _err("service S { fn m[T](x: T) -> T }")
    assert "found '['" in err or "expected (" in err


# ---- an explicit list turns the implicit heuristic OFF (roadmap 75(c)) -----
#
# Without `[T]`, a one-letter undeclared name in a signature is that fn's
# implicit type parameter. WITH `[T]`, declared means declared: only the
# listed names are type parameters, and a stray one-letter name is an
# ordinary undeclared (opaque nominal) type — so a typo'd name errors where
# it is used instead of silently quantifying (t25_explicit_tparam_heuristic_off.rvl).

def test_typoed_one_letter_name_under_an_explicit_list_errors():
    # `U` is a typo for `T`; it used to become a second type parameter and
    # wildcard at the call site, so `typo([1, 2])` compiled as if the
    # signature said `List[T]`. Now it is an opaque nominal and the mismatch
    # is refused.
    err = _err("fn typo[T](xs: List[U]) -> T { return xs[0] }\n"
               "fn g() -> Int { return typo([1, 2]) }")
    assert "argument 1 of `typo(...)` expects `List[U]`, got `List[Int]`" in err


def test_stray_one_letter_name_does_not_unify_across_arguments():
    err = _err("fn g[T](x: T, y: E) -> E { return y }\n"
               'fn h() -> Int { return g(1, 2) }')
    assert "argument 2 of `g(...)` expects `E`, got `Int`" in err


def test_stray_one_letter_name_is_an_opaque_nominal_not_a_declaration_error():
    # the revl reading of "undeclared": like `Row`, a stray `E` types its own
    # positions consistently and only errors where a use conflicts with a real
    # type — the fn itself still compiles
    ir = compile_source("fn g[T](x: T, y: E) -> E { return y }\n"
                        "fn h(e: E) -> E { return e }\n"
                        "fn j[T](x: T, y: E) -> T { return x }")
    assert len(ir["functions"]) == 3


def test_implicit_heuristic_is_still_on_without_an_explicit_list():
    # the change is scoped to signatures that carry `[...]`: a plain
    # `fn ident(x: T)` still quantifies `T` exactly as before
    ir = compile_source("fn ident(x: T) -> T { return x }\n"
                        'fn g() -> Str { return ident("hi") }')
    assert len(ir["functions"]) == 2


def test_implicit_and_explicit_forms_coexist_in_one_program():
    ir = compile_source("fn id[T](x: T) -> T { return x }\n"
                        "fn ident(x: T) -> T { return x }\n"
                        'fn a() -> Int { return id(5) }\n'
                        'fn b() -> Str { return ident("s") }')
    assert len(ir["functions"]) == 4


def test_explicit_declared_names_still_wildcard_and_unify():
    # only *undeclared* one-letter names stop quantifying; a declared `[T]`
    # is still a wildcard inside the body and unifies at the call site
    ir = compile_source("fn id[T](x: T) -> T { return x }\n"
                        'fn g() -> Str { return id("hi") }')
    assert len(ir["functions"]) == 2

"""Values of a user-declared generic variant (issue #2402).

`type Mb[T] = Jj(T) | Nn` could be declared and taken as a parameter, but no
value of it could be built: `Jj(1)` for `Mb[Int]` was refused with "payload
expects `T`, got `Int`" and `Nn` with "expects `Mb[Int]`, got `Mb`". The
checker compared the declared payload and the bare ADT head without
substituting the type arguments, which generic records already get through
`nominal_record_fields`. Payload typing for a match over an applied generic
remains a separate gap. Issue #2926 fixes exhaustiveness in both the native and
self-host checkers by resolving the declared variant head. Payload substitution
remains a separate issue.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402

MB = "type Mb[T] = Jj(T) | Nn\n\n"
EI = "type Ei[L, R] = Le(L) | Ri(R)\n\n"

UN = """
fn un(m: Mb[Int]) -> Int {
  return match m {
    Jj(x) => x + 1,
    Nn => 0,
  }
}
"""

ADMITTED = {
    # the three programs in the issue
    "issue_return": MB + "fn a() -> Mb[Int] { return Jj(1) }\n",
    "issue_let": MB + """
fn b(x: Int) -> Int {
  let v: Mb[Int] = Jj(x)
  return 0
}
""",
    "issue_nullary": MB + """
fn c() -> Mb[Int] {
  let v: Mb[Int] = Nn
  return v
}
""",
    "match_binds_payload": MB + UN,
    "inferred_let": MB + """
fn d() -> Mb[Int] {
  let v = Jj(3)
  return v
}
""",
    "argument": MB + UN + "fn e() -> Int { return un(Jj(2)) + un(Nn) }\n",
    "nested": MB + """
fn n() -> Mb[Mb[Int]] { return Jj(Jj(1)) }
fn n2() -> Mb[Mb[Int]] { return Jj(Nn) }
fn deep(m: Mb[Mb[Int]]) -> Int {
  return match m {
    Jj(inner) => match inner {
      Jj(x) => x,
      Nn => 1,
    },
    Nn => 0,
  }
}
""",
    "exhaustive_match": MB + """
fn f(m: Mb[Int]) -> Int {
  return match m {
    Jj(x) => x,
    Nn => 0,
  }
}
""",
    "wildcard_match": MB + """
fn f(m: Mb[Int]) -> Int {
  return match m {
    _ => 0,
  }
}
""",
    # `Nn` alone names no `T`: it is `Mb[Any]`, the placeholder `None` gets as
    # `Opt[Any]`, which a sibling arm fills and a declared boundary accepts
    "nullary_without_expectation": MB + """
fn f(c: Bool) -> Mb[Int] {
  let v = Nn
  let w = if (c) { Jj(1) } else { Nn }
  return w
}
""",
    "two_parameters": EI + """
fn l() -> Ei[Int, Str] { return Le(1) }
fn r() -> Ei[Int, Str] { return Ri("s") }
fn side(e: Ei[Int, Str]) -> Str {
  return match e {
    Le(n) => "left",
    Ri(s) => s,
  }
}
""",
    "generic_fn": MB + """
fn wrap[T](x: T) -> Mb[T] { return Jj(x) }
fn u() -> Mb[Int] { return wrap(3) }
""",
    "list_payload": "type Bag[T] = Many(List[T]) | NoBag\n\n"
                    "fn g() -> Bag[Int] { return Many([1, 2]) }\n",
}

REFUSED = {
    "wrong_payload_return": (
        MB + 'fn a() -> Mb[Int] { return Jj("x") }\n',
        "`Jj(...)` payload expects `Int`, got `Str`"),
    "wrong_payload_let": (
        MB + 'fn b() -> Int {\n  let v: Mb[Int] = Jj("x")\n  return 0\n}\n',
        "`Jj(...)` payload expects `Int`, got `Str`"),
    "wrong_payload_nested": (
        MB + 'fn n() -> Mb[Mb[Int]] { return Jj(Jj("x")) }\n',
        "`Jj(...)` payload expects `Int`, got `Str`"),
    "wrong_inferred_instance": (
        MB + 'fn d() -> Mb[Int] {\n  let v = Jj("x")\n  return v\n}\n',
        "expects `Mb[Int]`, got `Mb[Str]`"),
    "wrong_second_parameter": (
        EI + "fn r() -> Ei[Int, Str] { return Ri(1) }\n",
        "`Ri(...)` payload expects `Str`, got `Int`"),
    "nonexhaustive_match": (
        MB + """
fn f(m: Mb[Int]) -> Int {
  return match m {
    Nn => 0,
  }
}
""",
        "non-exhaustive match: missing case `Jj`"),
    "wrong_list_payload": (
        "type Bag[T] = Many(List[T]) | NoBag\n\n"
        "fn g() -> Bag[Int] { return Many(3) }\n",
        "`Many(...)` payload expects `List[Int]`, got `Int`"),
    "wrong_join": (
        MB + 'fn f(c: Bool) -> Mb[Str] {\n'
             '  let w = if (c) { Nn } else { Jj(1) }\n  return w\n}\n',
        "expects `Mb[Str]`, got `Mb[Int]`"),
    "arm_binds_substituted_payload": (
        MB + 'fn f(m: Mb[Int]) -> Str {\n  return match m {\n'
             '    Jj(x) => x,\n    Nn => "",\n  }\n}\n',
        "expects `Str`, got `Int`"),
    "arm_binds_second_parameter": (
        EI + 'fn f(e: Ei[Int, Str]) -> Str {\n  return match e {\n'
             '    Le(n) => n,\n    Ri(s) => s,\n  }\n}\n',
        "expects `Str`, got `Int`"),
    # the non-generic case table is untouched
    "non_generic_payload": (
        "type P = Pp(Int) | Qq\n\n"
        'fn a() -> P { return Pp("x") }\n',
        "`Pp(...)` payload expects `Int`, got `Str`"),
}


def _compile(tmp_path: Path, source: str) -> dict:
    path = tmp_path / "prog.rvl"
    path.write_text(source, encoding="utf-8")
    return compile_files([str(path)])


@pytest.mark.parametrize("name", sorted(ADMITTED))
def test_generic_variant_program_is_admitted(tmp_path, name):
    ir = _compile(tmp_path, ADMITTED[name])
    assert ir["functions"]


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_generic_variant_misuse_is_refused_with_substituted_type(tmp_path, name):
    source, message = REFUSED[name]
    with pytest.raises(RevlError) as excinfo:
        _compile(tmp_path, source)
    assert message in str(excinfo.value)


def test_case_table_payload_keeps_the_declared_spelling(tmp_path):
    """The emitted `types` table still carries the declaration's own `T`:
    substitution happens at each use, never in the declaration."""
    ir = _compile(tmp_path, ADMITTED["issue_return"])
    spec = ir["types"]["Mb"]
    assert spec["params"] == ["T"]
    assert [c["payload"] for c in spec["cases"]] == ["T", None]


def test_aggregate_missing_agreed_is_refused(tmp_path):
    """Issue #2926: resolving the applied head reaches the provided
    `Aggregate` as well, so a consumer that names both dissent arms but
    leaves out `Agreed` is refused (main admitted it)."""
    source = """fn settle(a: Aggregate[Str]) -> Str {
  return match a {
    Split(d) => "no",
    Inquorate(d) => "no",
  }
}
"""
    with pytest.raises(RevlError) as excinfo:
        _compile(tmp_path, source)
    assert "non-exhaustive match: missing case `Agreed`" in str(excinfo.value)

"""The go and java tiers emit the character classifiers and a record literal two
records share the fields of (issue #3144, part of #2899).

WHAT WAS WRONG. `is_digit`/`is_alpha`/`is_alnum`/`is_space` had an arm on the
py, ts and rust tiers only, so go refused them (`unknown v3 builtin method`) and
java too (`unknown builtin method`). A record literal whose field-set several
declared records share (`{ i, e }` is `PR` and `AtExpr`) was refused too: go
said no unique record has exactly those fields, java that the literal is
ambiguous. The frontend checker names such a literal from the type it flows
into; the two emitters read the field-set alone.

WHAT IS EMITTED NOW. Each classifier is a native comparison on the ASCII
contract the py tier states (empty receiver false, no input faults). A literal
takes the record its declared target names: a return, an assignment, a called
fn's parameter, an element of a `push` receiver, a field of a resolved record,
or a `let` the local is handed on from. A literal with no such target is still
refused by name.

The go run tests execute the program's `test` blocks on py and on go and need
the go toolchain; a skip is not a pass. The java run tests live in
backends/java/test_char_class_record_target_java_3144.py, which the
`backend-java` job runs with the JDK required.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from revl import compile_source  # noqa: E402
from revl.test import RUNNERS  # noqa: E402


def _load(tier: str):
    spec = importlib.util.spec_from_file_location(
        f"revl_{tier}_emit_3144", ROOT / "backends" / tier / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EMITTERS = {"go": _load("go"), "java": _load("java")}

# every classifier, at the edges of its range and on the empty string
CLASSES = """\
pub fn d(c: Str) -> Bool { return c.is_digit() }
pub fn a(c: Str) -> Bool { return c.is_alpha() }
pub fn nn(c: Str) -> Bool { return c.is_alnum() }
pub fn s(c: Str) -> Bool { return c.is_space() }

fn ch(n: Int) -> Str { return n.to_char() }

// the receiver is evaluated once, whatever it is
fn pick(xs: List[Str], i: Int) -> Str { return xs[i] }
pub fn at(xs: List[Str], i: Int) -> Bool { return pick(xs, i).is_digit() }

test "digits" {
  assert d("0") && d("5") && d("9")
  assert !d("/") && !d(":") && !d("a") && !d(" ") && !d("")
}
test "letters" {
  assert a("a") && a("m") && a("z") && a("A") && a("M") && a("Z")
  assert !a("`") && !a("{") && !a("@") && !a("[") && !a("_") && !a("0") && !a("")
}
test "alphanumerics" {
  assert nn("0") && nn("9") && nn("a") && nn("Z")
  assert !nn("_") && !nn("-") && !nn(" ") && !nn("")
}
test "whitespace" {
  assert s(" ") && s(ch(9)) && s(ch(10)) && s(ch(13))
  assert !s("") && !s("a") && !s("0") && !s(ch(12)) && !s(ch(11)) && !s(ch(160))
}
test "every ASCII code point" {
  var n = 0
  while (n < 128) {
    let c = ch(n)
    let digit = n >= 48 && n <= 57
    let alpha = (n >= 65 && n <= 90) || (n >= 97 && n <= 122)
    assert d(c) == digit
    assert a(c) == alpha
    assert nn(c) == (digit || alpha)
    assert s(c) == (n == 9 || n == 10 || n == 13 || n == 32)
    n += 1
  }
}
test "a computed receiver" {
  assert at(["x", "7"], 1)
  assert !at(["x", "7"], 0)
}
"""

# `{ i, e }` is `PR` and `AtExpr`; `{ name, ty }` is `Bind`, `Param`, `Field`.
# Every literal below names its record only through where it flows.
RECORDS = """\
type PR = { i: Int, e: Str }
type AtExpr = { i: Int, e: Int }
type Bind = { name: Str, ty: Str }
type Param = { name: Str, ty: Str }
type Field = { name: Str, ty: Str }
type Env = { binds: List[Bind], n: Int }

// a returned literal
fn bad(msg: Str) -> PR { return { i: 0 - 1, e: msg } }
fn at_e(n: Int) -> AtExpr { return { i: n, e: n + 1 } }

// a literal handed to a parameter
fn show(p: PR) -> Str { return p.e }
pub fn via_arg() -> Str { return show({ i: 1, e: "arg" }) }

// a `push` onto a typed receiver
fn binds(names: List[Str]) -> List[Bind] {
  var out: List[Bind] = []
  var i = 0
  while (i < names.length()) {
    out = out.push({ name: names[i], ty: "Int" })
    i += 1
  }
  return out
}
fn params(names: List[Str]) -> List[Param] {
  var out: List[Param] = []
  for (nm of names) { out = out.push({ name: nm, ty: "Str" }) }
  return out
}

// a field of a record already resolved
fn env(bs: List[Bind]) -> Env { return { binds: bs.push({ name: "z", ty: "Bool" }), n: 1 } }

// a let-bound literal, named by the push that takes it
fn field_of(nm: Str) -> List[Field] {
  var out: List[Field] = []
  let f = { name: nm, ty: "Float" }
  out = out.push(f)
  return out
}

// a let-bound literal, named by the return
fn pr(n: Int) -> PR {
  let p = { i: n, e: "ret" }
  return p
}

// an assignment to a typed local
fn assigned(n: Int) -> AtExpr {
  var cur: AtExpr = { i: 0, e: 0 }
  cur = { i: n, e: n * 2 }
  return cur
}

// the same local name in two sibling blocks, each handed to a different record
fn sibling(flag: Bool, bs: List[Bind], ps: List[Param]) -> Int {
  if (flag) {
    let p = { name: "b", ty: "Int" }
    return bs.push(p).length()
  } else {
    let p = { name: "p", ty: "Str" }
    return ps.push(p).length() + 10
  }
}

test "sibling blocks that reuse a local name" {
  assert sibling(true, [], []) == 1
  assert sibling(false, [], []) == 11
}
test "returned, argument and assigned literals" {
  assert bad("m").i == 0 - 1 && bad("m").e == "m"
  assert at_e(4).e == 5
  assert via_arg() == "arg"
  assert assigned(3).e == 6
  assert pr(2).e == "ret" && pr(2).i == 2
}
test "pushed and nested literals" {
  assert binds(["a", "b"]).length() == 2
  assert binds(["a", "b"])[1].name == "b"
  assert params(["p"])[0].ty == "Str"
  assert env([]).binds[0].ty == "Bool"
  assert field_of("q")[0].ty == "Float"
}
"""

# nothing says which of the two records this is
UNTARGETED = """\
type PR = { i: Int, e: Str }
type AtExpr = { i: Int, e: Int }
pub fn f(n: Int) -> Int {
  let p = { i: n, e: "x" }
  return p.i
}
"""


def _run(tier: str, src: str, name: str, count: int) -> None:
    py_status, py_message = RUNNERS["py"](compile_source(src, name))
    assert py_status == "pass", py_message
    status, message = RUNNERS[tier](compile_source(src, name))
    if status == "skip":
        pytest.skip(message)
    assert status == "pass", message
    assert f"{count} test(s) passed" in message, message


@pytest.mark.parametrize("tier", ["go", "java"])
def test_each_classifier_is_emitted(tier):
    src = EMITTERS[tier].emit(compile_source(CLASSES, "classes_3144.rvl"))
    assert '"0"' in src and '"9"' in src, src
    assert '"a"' in src and '"Z"' in src, src


@pytest.mark.parametrize("tier,built", [
    ("go", ("PR{", "AtExpr{", "Bind{", "Param{", "Field{")),
    ("java", ("new PR(", "new AtExpr(", "new Bind(", "new Param(", "new Field(")),
])
def test_a_shared_field_set_takes_its_declared_target(tier, built):
    src = EMITTERS[tier].emit(compile_source(RECORDS, "records_3144.rvl"))
    # the literals in `bad`, `at_e`, `binds`, `params` and `field_of` each
    # build the record their target names
    for literal in built:
        assert literal in src, (literal, src)
    # a returned `{ i, e }` is a PR in `bad` and an AtExpr in `at_e`
    body = src[src.index("bad("):]
    assert built[0] in body[:body.index("at_e(")], src
    assert built[1] in body[body.index("at_e("):], src


# one local handed on to two records that share its fields: no one of them
# is the literal's type, so both tiers refuse it instead of picking the first
TWO_TARGETS = """\
type Bind = { name: Str, ty: Str }
type Param = { name: Str, ty: Str }
fn tb(b: Bind) -> Str { return b.ty }
fn tp(p: Param) -> Str { return p.name }
fn both(n: Str) -> Str {
  let x = { name: n, ty: "T" }
  return tb(x) + tp(x)
}
test "both" { assert both("n") == "Tn" }
"""


# the second hand-on only appears once the outer literal is named: `outer` is
# an OB (it is returned as one), which makes `inner` a Param, while `tb(inner)`
# makes it a Bind
THROUGH_OUTER = """\
type Bind = { name: Str, ty: Str }
type Param = { name: Str, ty: Str }
type OA = { b: Bind, k: Str }
type OB = { b: Param, k: Str }
fn tb(b: Bind) -> Str { return b.ty }
fn mk(n: Str) -> OB {
  let inner = { name: n, ty: "T" }
  let outer = { b: inner, k: tb(inner) }
  return outer
}
test "mk" { assert mk("n").k == "T" }
"""


@pytest.mark.parametrize("tier", ["go", "java"])
def test_a_literal_with_no_target_is_still_refused(tier):
    with pytest.raises(EMITTERS[tier].EmitError, match="PR, AtExpr|share"):
        EMITTERS[tier].emit(compile_source(UNTARGETED, "untargeted_3144.rvl"))


@pytest.mark.parametrize("tier,words", [
    ("go", "Bind, Param share them"),
    ("java", "ambiguous between Bind, Param"),
])
def test_a_local_handed_to_two_records_is_refused_by_name(tier, words):
    with pytest.raises(EMITTERS[tier].EmitError, match=words):
        EMITTERS[tier].emit(compile_source(TWO_TARGETS, "two_targets_3144.rvl"))


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_the_classifiers_like_py():
    _run("go", CLASSES, "classes_3144.rvl", 6)


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_the_record_targets_like_py():
    _run("go", RECORDS, "records_3144.rvl", 3)


@pytest.mark.parametrize("tier,words", [
    ("go", "Bind, Param share them"),
    ("java", "ambiguous between Bind, Param"),
])
def test_a_conflict_found_through_an_outer_literal_is_refused(tier, words):
    with pytest.raises(EMITTERS[tier].EmitError, match=words):
        EMITTERS[tier].emit(compile_source(THROUGH_OUTER, "through_outer_3144.rvl"))


def _deep(levels: int) -> str:
    """`levels` nested records, each outer field-set shared by two records, with
    the innermost local handed to a `Bind` parameter and, through the chain, to
    a `Param` field: every depth must be refused, not only the shallow ones."""
    out = ["type Bind = { name: Str, ty: Str }",
           "type Param = { name: Str, ty: Str }"]
    for n in range(1, levels + 1):
        inner_a = "Bind" if n == 1 else f"L{n - 1}A"
        inner_b = "Param" if n == 1 else f"L{n - 1}B"
        out.append(f"type L{n}A = {{ f{n}: {inner_a}, k: Str }}")
        out.append(f"type L{n}B = {{ f{n}: {inner_b}, k: Str }}")
    out.append("fn tb(b: Bind) -> Str { return b.ty }")
    out.append(f"fn mk(n: Str) -> L{levels}B {{")
    out.append('  let v0 = { name: n, ty: "T" }')
    out.append('  let v1 = { f1: v0, k: tb(v0) }')
    for n in range(2, levels + 1):
        out.append(f'  let v{n} = {{ f{n}: v{n - 1}, k: "k" }}')
    out.append(f"  return v{levels}")
    out.append("}")
    out.append('test "mk" { assert mk("n").k == "k" }')
    return "\n".join(out) + "\n"


@pytest.mark.parametrize("levels", [3, 7, 8, 9, 10, 12])
@pytest.mark.parametrize("tier,words", [
    ("go", "Bind, Param share them"),
    ("java", "ambiguous between Bind, Param"),
])
def test_a_deep_chain_of_hand_ons_is_refused_at_every_depth(tier, words, levels):
    with pytest.raises(EMITTERS[tier].EmitError, match=words):
        EMITTERS[tier].emit(compile_source(_deep(levels), f"deep{levels}_3144.rvl"))

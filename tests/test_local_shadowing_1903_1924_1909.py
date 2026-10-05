"""Issues #1903 / #1924 / #1909 — three separate reports, one shared hole.

The type environment answers "what type does this name have". The resolver was
using it to answer a different question — "is this name bound at all" — by
recording a binding only when the binder's type was KNOWN. A local that could
not be typed was therefore indistinguishable from a name that was never bound,
and `ExprVar` fell through to the item-380 rule: a bare reference to a
top-level `fn` is a first-class function value of type `(params) -> returns`.
The local silently resolved to a same-named FUNCTION:

  * #1903 — `let names = match ...` whose arms bind a builtin `Opt`/`Result`
    payload recorded nothing, because the arm-payload lookup only knew about
    user `variant` declarations (builtin `Opt`/`Result` have no `types` entry).
    `names` then resolved to `fn names(n: Int) -> List[Str]` and the checker
    refused `` `==` cannot compare `(Int) -> List[Str]` with `Str` ``. The
    plain `let names = "x"` spelling of the same binding was fine, so the two
    binding forms disagreed — the report's own complaint.
  * #1924 — an UNANNOTATED lambda parameter was actively removed from the
    arrow's inner scope (`inner.pop(param, None)`), so `(q) => shout(q)`
    resolved `q` to any top-level `fn q` in the compile. Not even visibility
    was required: a PRIVATE `fn q` in another file of the same compile captured
    the name, which is how a harness lambda came to fail with `` argument 1 of
    `rt_search(...)` expects `Str`, got `(Str) -> Str` ``.
  * #1909 — the same missing payload type left the `Some(e) => e.key` arm at
    the bottom, so the `None => []` arm's `List[Never]` won the join and the
    match bound `List[Never]`; `indexOf(k)` was then refused with `` builtin
    `indexOf` argument expects `Never`, got `Str` ``.

Root cause, in all three: a binding whose type is not known still OCCUPIES the
name (`bind_local`). The payload of a builtin `Opt`/`Result` arm is now read
from the scrutinee the same way lowering already read it (`match_arm_payload`
mirrors `lower._arm_payload_type`), and every arrow parameter is bound in the
arrow's scope whether or not it carries a written annotation.

Each fixture is the issue's own reproducer and is measured by RUNNING the
emitted module, so a "fix" that only silences a diagnostic does not pass.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _py_emit(ir):
    spec = importlib.util.spec_from_file_location(
        "revl_py_emit_shadowing", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(ir)


def _call(ir, name, *args):
    """Emit the module, exec it and call the fn — the value is the assertion."""
    out = _py_emit(ir)
    namespace: dict = {}
    exec(compile(out, f"<emitted:{name}>", "exec"), namespace)  # noqa: S102
    return namespace[name](*args)


# --------------------------------------------------------------------------- #
# #1903 — `let x = match ...` binds over a same-named top-level `fn`
# --------------------------------------------------------------------------- #

# the issue's reproducer, verbatim (including its `test` block: the report
# notes the same body in a `test` block failed the same way)
_ISSUE_1903 = '''fn names(n: Int) -> List[Str] {
  return []
}

fn probe() -> Result[Int, Str] {
  return Ok(1)
}

fn check() -> Bool {
  let names = match probe() {
    Ok(v) => "x",
    Err(e) => e,
  }
  return names == "x"
}

test "in a fn body" {
  assert check()
}
'''

# The same binding with the plain form the report calls the working one. The
# two must agree: `names` reads the LOCAL in both, never the function.
_PLAIN_1903 = _ISSUE_1903.replace(
    '  let names = match probe() {\n'
    '    Ok(v) => "x",\n'
    '    Err(e) => e,\n'
    '  }\n',
    '  let names = "x"\n')


def test_1903_let_match_local_binds_over_a_same_named_top_level_fn():
    """RED before: `let names` bound nothing and the read resolved to
    `fn names`, so the compile died with `` `==` cannot compare
    `(Int) -> List[Str]` with `Str` ``."""
    ir = compile_source(_ISSUE_1903, "issue1903.rvl")
    assert _call(ir, "check") is True


def test_1903_the_two_binding_forms_agree():
    """The report's actual complaint: the `match` form and the plain form
    disagreed. Both must resolve `names` to the local."""
    assert _call(compile_source(_ISSUE_1903, "issue1903.rvl"), "check") \
        == _call(compile_source(_PLAIN_1903, "issue1903.rvl"), "check")


def test_1903_a_match_local_shadows_across_a_use_import():
    """The report's module-shaped half: a test block in module A declares the
    local while module B declares `fn names` and imports A. Both modules are in
    one compile, so the local must still win."""
    a = ('pub fn names(n: Int) -> List[Str] {\n'
         '  return []\n'
         '}\n'
         'pub fn check() -> Bool {\n'
         '  let names = match probe() {\n'
         '    Ok(v) => "x",\n'
         '    Err(e) => e,\n'
         '  }\n'
         '  return names == "x"\n'
         '}\n'
         'fn probe() -> Result[Int, Str] {\n'
         '  return Ok(1)\n'
         '}\n')
    b = 'use "./a.rvl" { check }\npub fn run() -> Bool { return check() }\n'
    ir = compile_files(["b.rvl"], sources={"b.rvl": b, "a.rvl": a})
    assert _call(ir, "run") is True


# --------------------------------------------------------------------------- #
# #1924 — a lambda parameter is not another file's top-level `fn`
# --------------------------------------------------------------------------- #

_A_1924 = ('fn q(s: Str) -> Str { return "<" + s + ">" }\n'
           'pub fn wrap(s: Str) -> Str { return q(s) }\n')
_B_1924 = ('fn shout(x: Str) -> Str { return x + "!" }\n'
           'fn apply(f: (Str) -> Str) -> Str { return f("hi") }\n'
           'pub fn t() -> Str { return apply((q) => shout(q)) }\n')


def test_1924_a_private_fn_in_another_file_does_not_capture_a_lambda_param():
    """RED before: `q` resolved to a.rvl's private `fn q`, so `shout(q)` was
    refused with `` argument 1 of `shout(...)` expects `Str`, got
    `(Str) -> Str` `` — and only when a.rvl was part of the compile."""
    ir = compile_files(["a.rvl", "b.rvl"],
                       sources={"b.rvl": _B_1924, "a.rvl": _A_1924})
    assert _call(ir, "t") == "hi!"


def test_1924_a_same_named_fn_in_the_same_file_does_not_capture_it():
    """The report's "both files concatenated into one" shape."""
    ir = compile_source(_A_1924 + _B_1924, "issue1924.rvl")
    assert _call(ir, "t") == "hi!"


def test_1924_an_annotated_parameter_behaves_the_same():
    """`(q) => ...` and `(q: Str) => ...` are the same binding. Only the
    annotated one was ever recorded, which is what made this a one-spelling
    bug."""
    unannotated = compile_files(
        ["a.rvl", "b.rvl"], sources={"b.rvl": _B_1924, "a.rvl": _A_1924})
    annotated = compile_files(
        ["a.rvl", "b.rvl"],
        sources={"b.rvl": _B_1924.replace("(q) =>", "(q: Str) =>"),
                 "a.rvl": _A_1924})
    assert _call(unannotated, "t") == _call(annotated, "t") == "hi!"


# --------------------------------------------------------------------------- #
# #1909 — an empty arm unifies with the scrutinee's element type, not `Never`
# --------------------------------------------------------------------------- #

_ISSUE_1909 = '''type E = { key: List[Str] }
fn has(o: Opt[E], k: Str) -> Bool {
  let key = match o { Some(e) => e.key, None => [] }
  return key.indexOf(k) >= 0
}
'''


def test_1909_empty_arm_does_not_bottom_out_at_never():
    """RED before: the `None => []` arm won the join as `List[Never]` and the
    compile died with `` builtin `indexOf` argument expects `Never`, got
    `Str` ``."""
    ir = compile_source(_ISSUE_1909, "issue1909.rvl")
    assert _call(ir, "has", {"key": ["a", "b"]}, "b") is True
    assert _call(ir, "has", {"key": ["a", "b"]}, "z") is False
    assert _call(ir, "has", None, "b") is False


def test_1909_the_annotated_and_unannotated_bindings_agree():
    """The report's workaround (`let key: List[Str] = match ...`) must not be
    the only spelling that typechecks."""
    annotated = _ISSUE_1909.replace("let key = match", "let key: List[Str] = match")
    for src in (_ISSUE_1909, annotated):
        assert _call(compile_source(src, "issue1909.rvl"), "has",
                     {"key": ["a", "b"]}, "b") is True


def test_1909_the_scrutinee_element_type_is_what_the_binding_gets():
    """The structural half, so the fix is caught at inference and not only by a
    running value: a `Str` payload is admitted against the `List[Str]` binding
    (which `List[Never]` would refuse, as the report's `indexOf` error shows)
    and a non-`Str` one is still refused."""
    ok = ('type E = { key: List[Str] }\n'
          'fn at(o: Opt[E], k: Str) -> Int {\n'
          '  let key = match o { Some(e) => e.key, None => [] }\n'
          '  return key.indexOf(k)\n'
          '}\n')
    ir = compile_source(ok, "issue1909.rvl")
    assert _call(ir, "at", {"key": ["a", "b"]}, "b") == 1
    assert _call(ir, "at", None, "b") == -1

    bad = ('type E = { key: List[Str] }\n'
           'fn at(o: Opt[E]) -> Int {\n'
           '  let key = match o { Some(e) => e.key, None => [] }\n'
           '  return key.indexOf(1)\n'
           '}\n')
    with pytest.raises(RevlError):
        compile_source(bad, "issue1909.rvl")


# --------------------------------------------------------------------------- #
# The follow-on the #1903 fix exposed: a `Never` nested inside a declared type
# --------------------------------------------------------------------------- #

# The shape selfhost/checker.rvl:3444 hits once the arm really infers its
# payload: `var svc = { name: ..., methods: [] }` declares the structural
# `{methods: List[Never], name: Str}`, and the arm now yields the nominal
# `SvcD`. The bottom sits one level DOWN, inside a structural record.
_ACC_WIDEN = '''type MethSig = { nm: Str }
type SvcD = { name: Str, methods: List[MethSig] }
fn pick(o: Opt[SvcD]) -> Str {
  var svc = { name: "x", methods: [] }
  svc = match o { Some(s) => s, None => svc }
  return svc.name
}
'''


def test_a_never_nested_in_a_structural_declared_type_is_still_filled():
    """`widen_bottom` had no structural branch, so it could only fill a bottom
    at the TOP of a declared type. Here the declared type is a structural
    record whose field is `List[Never]`, and the actual is a nominal record —
    the widening has to recurse into the field, or the assignment is refused
    with ``assignment to `svc` ... expects `{methods: List[Never], name: Str}`,
    got `SvcD` ``."""
    ir = compile_source(_ACC_WIDEN, "acc_widen.rvl")
    assert _call(ir, "pick", {"name": "y", "methods": [{"nm": "m"}]}) == "y"
    assert _call(ir, "pick", None) == "x"


# --------------------------------------------------------------------------- #
# The rule the three fixes must NOT disable
# --------------------------------------------------------------------------- #

_NEG_380 = ('fn shout(x: Str) -> Str { return x + "!" }\n'
            'fn apply(f: (Str) -> Str) -> Str { return f("hi") }\n'
            'fn shout2(x: Int) -> Int { return x }\n'
            'pub fn t1() -> Str { return apply(shout2) }\n')
_NEG_380_LAMBDA = ('fn shout(x: Str) -> Str { return x + "!" }\n'
                   'fn apply(f: (Str) -> Str) -> Str { return f("hi") }\n'
                   'fn shout2(x: Int) -> Int { return x }\n'
                   'pub fn t2() -> Str { return apply((q) => shout2(q)) }\n')


@pytest.mark.parametrize("src,expected", [
    (_NEG_380, "got `(Int) -> Int`"),
    # the lambda shape is refused for the reason the arrow's own parameter type
    # gives, now that `q` really is the lambda's `Str` parameter
    (_NEG_380_LAMBDA, "argument 1 of `shout2(...)` expects `Int`, got `Str`"),
])
def test_a_top_level_fn_used_as_a_value_is_still_checked(src, expected):
    """Item 380: a bare reference to a top-level `fn` IS a first-class
    function value, and it is still typed and still checked. #1903 and #1924
    are about a LOCAL occupying the name, not about this rule, so a binding
    that genuinely names the function must keep being refused when the types
    disagree — otherwise the fixes would have swapped one silent resolution for
    another."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "item380.rvl")
    assert expected in str(excinfo.value)


def test_a_local_may_still_shadow_a_fn_that_would_have_agreed():
    """The other side of the same guard: when the local's type DOES match, the
    local is what is read — the fix is resolution, not a new refusal."""
    ir = compile_source(
        'fn shout(x: Str) -> Str { return x + "!" }\n'
        'pub fn t() -> Str {\n'
        '  let shout = "quiet"\n'
        '  return shout\n'
        '}\n', "shadow.rvl")
    assert _call(ir, "t") == "quiet"

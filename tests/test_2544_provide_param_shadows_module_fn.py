"""Issue #2544: a provide-method parameter whose declared type is not a
function is silently dropped when it is called.

`fn tag(s: Str) -> Str` in scope and a provide method `fn f(tag) = tag("x")`
used to compile: `tag` is a `Str`, so `tag("x")` cannot be a call on the
parameter. Lowering fell through to the module `fn tag` and the parameter's
value was never read. The `let` form of the same shadowing was refused (item
445); the parameter form was the one inconsistent case.

The owner ruling applied here:

  * the function-valued parameter case (#1823) is LEGAL and carved out: a
    parameter whose declared type is a function type and which is called is
    admitted, and the reference and the self-host agree on the lowering, so
    refusing it breaks #1823's byte-for-byte coverage. The carve-out is scoped
    to that shape, not a repeal of the item-445 refusal for other binders;
  * the `Str`-shadowing case is refused on the TYPECHECK side, not by a
    blanket lowering refusal. `typecheck.refuse_call_on_non_function` sits
    after the function-type arm in `infer_ast`'s `ExprCall` and in the IR
    `call`/`name` arm, and inside `infer_ir`'s `fn` arm, so it fires only
    when the type environment holds a binder of that name that is definitely
    not a function. The arrow spelling returns at the `FN_HEAD` arms above
    the first two; a `Fn[...]` binder reaches the guard and passes
    `is_function_type` (the IR `fn` guard admits both spellings inside
    itself). An unknown (⊥) binder stays lenient.

The guard is keyed on the TYPE ENVIRONMENT, not on an enumeration of binder
forms: every typed binder (`let`, `for`, arrow parameter, match-arm payload,
provide-method parameter, an effect/approval `let`) is recorded there, so a
form added to the grammar later is covered without a second edit. The one
binder that is NOT typed is the `every … in` item of a plain iteration, so
that form stays enumerated in the lowering refusal list and fails closed
instead of being silently admitted.

The pre-existing lowering refusal (`_refuse_callable_shadowing`, item 445)
still owns the other binder forms; the controls below pin that the carve-out
did not widen into them. The self-host lowering still admits the `Str` case
(it is the next slice of #2544), so this file tests the reference alone.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

_TAG = "fn tag(s: Str) -> Str { return s }\n"
_SERVICE = "service Cache {\n  fn f(tag: Str) -> Str\n}\n"
# the typecheck-side refusal, with its real code
_REFUSED = "`tag` is bound here as `Str`, which is not a function"
# the pre-existing lowering refusal for the other binder forms
_LOWER_REFUSED = "is bound here and called in this body"


def test_a_str_parameter_shadowing_a_called_module_fn_is_refused():
    """The issue's own reproducer. Before this change it compiled, and the
    lowered body called the module `fn tag` -- the parameter was never read.
    The refusal carries a real code and the call's own location."""
    source = "\n".join([
        "fn tag(s: Str) -> Str { return s }",      # 1
        "service Cache {",                          # 2
        "  fn f(tag: Str) -> Str",                  # 3
        "}",                                        # 4
        "component C provides cache: Cache {",      # 5
        "  provide cache {",                        # 6
        "    fn f(tag) = tag(\"x\")",               # 7
        "  }",                                      # 8
        "}",                                        # 9
    ]) + "\n"
    with pytest.raises(RevlError, match=_REFUSED) as exc:
        compile_source(source, "t.rvl")
    assert exc.value.code == "T1"
    assert exc.value.category == "type-mismatch"
    assert (exc.value.filename, exc.value.line) == ("t.rvl", 7)


def test_the_extern_half_is_refused_by_the_same_rule():
    """A module `extern` of the name is the same collision, and the typecheck
    guard needs no separate extern arm: the parameter's type is what decides."""
    source = "\n".join([
        "extern pure fn stamp(s: Str) -> Str = @py { return s }",  # 1
        "service Cache {",                                          # 2
        "  fn f(stamp: Str) -> Str",                                # 3
        "}",                                                        # 4
        "component C provides cache: Cache {",                      # 5
        "  provide cache {",                                        # 6
        "    fn f(stamp) = stamp(\"x\")",                           # 7
        "  }",                                                      # 8
        "}",                                                        # 9
    ]) + "\n"
    with pytest.raises(RevlError, match="`stamp` is bound here as `Str`") as exc:
        compile_source(source, "t.rvl")
    assert exc.value.code == "T1"
    assert (exc.value.filename, exc.value.line) == ("t.rvl", 7)


def test_the_fn_body_str_parameter_is_refused():
    """The other stratum #2544's acceptance names. A `fn` body's `Str`
    parameter named like a module fn and called was already refused by the
    item-445 lowering scan, which runs before body typing; the typecheck rule
    refuses the same shape when the call reaches it (the provide-method and
    case tests below)."""
    with pytest.raises(RevlError, match=_LOWER_REFUSED):
        compile_source(
            _TAG + "fn f(tag: Str) -> Str {\n  return tag(\"x\")\n}\n",
            "t.rvl",
        )


def test_a_str_parameter_shadowing_a_called_case_is_refused():
    """A case constructor is the third callable kind the guard names. In a
    component a case call lowers to a `call`/`name` node, not the `fn` node,
    so this pins the IR half of the rule as well as the AST half: the
    parameter is a `Str` and cannot be `Mk("x")`, and the binding is not
    silently dropped."""
    source = "\n".join([
        "type T = Mk(Str)",                         # 1
        "service M {",                              # 2
        "  fn go(Mk: Str) -> T",                    # 3
        "}",                                        # 4
        "component C provides m: M {",              # 5
        "  provide m {",                            # 6
        "    fn go(Mk) = Mk(\"x\")",                # 7
        "  }",                                       # 8
        "}",                                        # 9
    ]) + "\n"
    with pytest.raises(RevlError, match="`Mk` is bound here as `Str`") as exc:
        compile_source(source, "t.rvl")
    assert exc.value.code == "T1"
    assert (exc.value.filename, exc.value.line) == ("t.rvl", 7)


def test_a_case_call_with_no_shadowing_binding_is_admitted():
    """The case path the guard must not widen into: a component method calling
    an ordinary case constructor, with no local of that name."""
    ir = compile_source(
        "type T = Mk(Str)\n"
        "service M { fn go(x: Str) -> T }\n"
        "component C provides m: M {\n"
        "  provide m { fn go(x) = Mk(x) }\n"
        "}\n",
        "t.rvl",
    )
    assert ir is not None


def test_an_open_typed_parameter_called_stays_admitted():
    """`Any`/`Value` are erased dynamic types, not known non-functions: a call
    on one stays with the ordinary checks, as it did before #2544's rule."""
    for head in ("Any", "Value"):
        ir = compile_source(f"fn g(f: {head}) -> Any {{ return f(1) }}", "t.rvl")
        assert ir is not None


# ------------------------------------------- the carve-out: this one is LEGAL

def test_a_function_valued_parameter_named_like_a_module_fn_is_admitted():
    """The #1823 shape, restored: a module `fn f` in scope, a provide-method
    parameter `f` of function type, and a call on it. The owner ruling admits
    this shape, and the reference and the self-host lower it identically (the
    byte-for-byte half is pinned in tests/test_selfhost_lower_ir.py). The
    module fn stays in scope, so the control is not the module fn's absence."""
    ir = compile_source(
        "fn f(x: Int) -> Int { return x }\n"
        "service M { fn go(f: (Int) -> Int, n: Int) -> Int }\n"
        "component C provides m: M {\n"
        "  provide m { fn go(f, n) = f(n) }\n"
        "}\n",
        "t.rvl",
    )
    assert [fn["name"] for fn in ir["functions"]] == ["f"]
    method = ir["components"][0]["body"][0]["methods"][0]
    assert method["params"] == ["f", "n"]
    assert method["body"]


def test_the_fn_spelling_of_a_function_type_is_admitted_too():
    """The same carve-out for the `Fn[...]` spelling. `Fn[Int, Int]` compiles
    on origin/main (the wasm tier's fixtures use it, and taint.py already
    treats the `Fn` head as a function type), and the first cut of the #2544
    guard refused it: it compared the parsed head with `->` only. Both
    spellings name a callable value, so neither may be judged a fall-through
    to a same-named module callable. This is the minimal regression that the
    wasm module's `test_v3_escaping_arrow_still_rejected` compiles."""
    ir = compile_source(
        "fn apply(f: Fn[Int, Int], x: Int) -> Int { return f(x) }\n",
        "t.rvl",
    )
    assert [fn["name"] for fn in ir["functions"]] == ["apply"]
    # the provide-method half, with a same-named module fn in scope
    ir = compile_source(
        "fn f(x: Int) -> Int { return x }\n"
        "service M { fn go(f: Fn[Int, Int], n: Int) -> Int }\n"
        "component C provides m: M {\n"
        "  provide m { fn go(f, n) = f(n) }\n"
        "}\n",
        "t.rvl",
    )
    method = ir["components"][0]["body"][0]["methods"][0]
    assert method["params"] == ["f", "n"]
    assert method["body"]


# -------------------------- the binder forms the carve-out must NOT widen to

@pytest.mark.parametrize("source", [
    # a `let` local called in the same body
    "fn helper(n: Int) -> Int { return n }\n"
    "fn g(h: (Int) -> Int) -> Int {\n"
    "  let helper = h\n"
    "  return helper(1)\n"
    "}\n",
    # a `for` bind called in the loop
    "fn helper(n: Int) -> Int { return n }\n"
    "fn g() -> Int {\n"
    "  for (helper of [1, 2]) {\n"
    "    return helper(1)\n"
    "  }\n"
    "  return 0\n"
    "}\n",
    # an arrow parameter called in the arrow body
    "fn helper(n: Int) -> Int { return n }\n"
    "fn g() -> Int {\n"
    "  let f = (helper: (Int) -> Int) => helper(1)\n"
    "  return f((x: Int) => x)\n"
    "}\n",
    # a match-arm payload called in the arm
    "fn helper(n: Int) -> Int { return n }\n"
    "type B = Box(Int)\n"
    "fn g(b: B) -> Int {\n"
    "  return match b { Box(helper) => helper(1) }\n"
    "}\n",
    # a module-fn parameter called in the fn body (the item 445 form)
    "fn tag(s: Str) -> Str { return s }\n"
    "fn f(tag: (Str) -> Str) -> Str {\n"
    "  return tag(\"x\")\n"
    "}\n",
    # a provide-method `let` (not a parameter) called in the same method
    "fn helper(s: Str) -> Str { return s }\n"
    "service Cache { fn f(key: Str) -> Str }\n"
    "component C provides cache: Cache {\n"
    "  provide cache {\n"
    "    fn f(key) {\n"
    "      let helper = \"x\"\n"
    "      return helper(key)\n"
    "    }\n"
    "  }\n"
    "}\n",
])
def test_the_pre_existing_binder_forms_still_refuse(source):
    """The carve-out is by TYPE, not a repeal of item 445. Every other binder
    form still meets `_refuse_callable_shadowing` with its own message."""
    with pytest.raises(RevlError, match=_LOWER_REFUSED):
        compile_source(source, "t.rvl")


def test_the_every_in_item_is_enumerated_and_fails_closed():
    """`every x in sub` binds an item for the body. A plain iteration carries
    no contract, so the item has no type the typecheck rule could judge;
    `_binder_names` enumerates it so the form fails closed rather than
    silently calling a module `fn` of the same name from an emission argument.
    Reading the item without calling it stays admitted."""
    source = (
        "fn x(s: Str) -> Str { return s }\n"
        "service Sink { emission fn write(v: Str) }\n"
        "component C requires sink: Sink {\n"
        "  let src = effect Stream.source() undo src.close()\n"
        "  let sub = subscribe src undo sub.close()\n"
        "  every x in sub { emit sink.write(x(\"y\")) }\n"
        "}\n"
    )
    with pytest.raises(RevlError, match="`x` is bound here and called in this body"):
        compile_source(source, "s.rvl")
    read_only = source.replace('x("y")', "x")
    assert compile_source(read_only, "s.rvl") is not None


# ------------------------------------------------ what must NOT be refused

def test_a_provide_parameter_that_only_shares_a_fn_name_is_admitted():
    """The typecheck guard must not widen the refusal: only the CALL position
    is ambiguous. Here the parameter is a `Str` and is never called, which is
    the common shape (`backends/wasm/golden/functions.revl` declares
    `fn name(row: Row)` beside `fn make_row(id: Int, name: Str)`)."""
    ir = compile_source(
        _TAG + _SERVICE + """
        component C provides cache: Cache {
          provide cache {
            fn f(tag) = "x"
          }
        }
        """,
        "t.rvl",
    )
    assert ir is not None


def test_a_provide_method_calling_a_module_fn_it_does_not_bind_is_admitted():
    """A method whose parameter is named unlike anything else still calls the
    module `fn` through `callables` -- the arm the fix leaves ungated for an
    UNSCOPED name."""
    ir = compile_source(
        _TAG + "service Cache {\n  fn f(key: Str) -> Str\n}\n" + """
        component C provides cache: Cache {
          provide cache {
            fn f(key) = tag(key)
          }
        }
        """,
        "t.rvl",
    )
    assert ir is not None


def test_a_method_call_through_the_parameter_is_not_a_call_position():
    """`tag.concat(...)` names a method on the parameter, not a bare call:
    `_called_names` reads only `ExprCall` with an `ExprVar` callee, the same
    exclusion that keeps `x.m()` unambiguous in the `fn` stratum."""
    ir = compile_source(
        _TAG + _SERVICE + """
        component C provides cache: Cache {
          provide cache {
            fn f(tag) = tag.concat("x")
          }
        }
        """,
        "t.rvl",
    )
    assert ir is not None


def test_a_component_body_binding_still_pairs_with_a_call_in_a_method():
    """A component body's `let` is captured by the provide methods' closures,
    so a bare call inside a method still names both the local and the module
    fn. The component-level scan keeps that pairing."""
    with pytest.raises(RevlError,
                       match="`helper` is bound here and called in this body"):
        compile_source(
            "fn helper(s: Str) -> Str { return s }\n"
            "service Cache { fn f(key: Str) -> Str }\n"
            "component C provides cache: Cache {\n"
            "  let helper = effect Map.new() undo helper.drop()\n"
            "  provide cache {\n"
            "    fn f(key) = helper(key)\n"
            "  }\n"
            "}\n",
            "t.rvl",
        )


def test_a_sibling_method_binding_another_name_does_not_hide_the_refusal():
    """Each provide method is its own emitted body, and the typecheck guard is
    keyed on the type environment of the body it checks: the first method's
    `tag` parameter is there as a `Str`, so its call is still refused when a
    sibling method binds a DIFFERENT module fn name and never calls it. The
    refusal is the typecheck one, not a component-wide lowering scan."""
    source = (
        "fn clean(s: Str) -> Str { return s }\n"
        + _TAG + "service Cache {\n  fn f(tag: Str) -> Str\n"
        "  fn g(clean: Str) -> Str\n}\n" + """
        component C provides cache: Cache {
          provide cache {
            fn f(tag) = tag("x")
            fn g(clean) = clean
          }
        }
        """
    )
    with pytest.raises(RevlError, match=_REFUSED) as exc:
        compile_source(source, "t.rvl")
    assert exc.value.code == "T1"
    assert (exc.value.filename, exc.value.line) == ("t.rvl", 10)


def test_a_sibling_methods_call_does_not_make_a_parameter_ambiguous():
    """A provide parameter binds only inside its own method, and only that
    method's type environment holds it. Here `f`'s `tag` parameter is never
    called there, and the call in `g` names the module fn, so nothing is
    ambiguous: the two methods are separate emitted bodies, not one scope."""
    ir = compile_source(
        _TAG + "service Cache {\n  fn f(tag: Str) -> Str\n"
        "  fn g(key: Str) -> Str\n}\n" + """
        component C provides cache: Cache {
          provide cache {
            fn f(tag) = "x"
            fn g(key) = tag(key)
          }
        }
        """,
        "t.rvl",
    )
    assert ir is not None

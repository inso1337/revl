"""Issue #2544: a provide-method PARAMETER named like a module `fn` is a
binding, and a body that calls it is refused exactly like the `let` form.

`lower._refuse_callable_shadowing` refuses a body that both BINDS a name and
CALLS it where a module `fn`/`extern` of that name is visible. It enumerates
`_binder_names`, and a provide-method parameter was not one of them:
`parser.ProvideMethod` carries its surfaces as a plain `list[str]`, not as
`FnParam` nodes, and every other walker reaches those names through
`env.bind_params` instead. So the `=>` / `let` / `for` arms fired and the one
form that actually ships in every provide block did not.

The consequence was silent, not merely inconsistent: with the parameter
unseen by the scan, nothing refused the body, and `_lower_component_pure_expr`'s
`callables` arm (which is deliberately not gated on `name not in scope`)
lowered `tag("x")` to a call of the module `fn tag`, so the parameter's value
was never read. The binding was still created, so this was a dropped value,
not a dropped name. The issue's own example is the reproducer below.

The `fn` body stratum already refused this (`fn shadowed(helper: …)`); this
file adds the provide-method half the acceptance list asks for, plus the cases
that pin the precision of the newly recognised binder: a parameter that merely
SHARES a module fn's name is still admitted, a provide method whose call names
a DIFFERENT module fn still compiles, a method call is not a call position, and
a sibling method's call does not blame another method's parameter (a provide
method is its own emitted body). It also pins the #1823 function-valued shape
as refused: the decided rule has no type carve-out, so a same-name module fn
and parameter are one rule whatever the parameter's type.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

_TAG = "fn tag(s: Str) -> Str { return s }\n"
_SERVICE = "service Cache {\n  fn f(tag: Str) -> Str\n}\n"
_REFUSED = "`tag` is bound here and called in this body"


def test_a_provide_method_parameter_shadowing_a_called_module_fn_is_refused():
    """The issue's own reproducer. Before the fix this compiled, and the
    lowered body called the module `fn tag` — the parameter was never read."""
    with pytest.raises(RevlError, match=_REFUSED):
        compile_source(
            _TAG + _SERVICE + """
            component C provides cache: Cache {
              provide cache {
                fn f(tag) = tag("x")
              }
            }
            """,
            "t.rvl",
        )


def test_the_fn_body_form_of_the_same_parameter_is_refused():
    """The twin strata the acceptance list names: the `fn` body already
    refused this, and it still does. The two now agree."""
    with pytest.raises(RevlError, match=_REFUSED):
        compile_source(
            _TAG + """
            fn f(tag: (Str) -> Str) -> Str {
              return tag("x")
            }
            """,
            "t.rvl",
        )


def test_a_function_valued_parameter_named_like_a_module_fn_is_refused():
    """The decided rule has no type carve-out. This is the #1823 shape: a
    module `fn f` in scope, a provide-method parameter `f` of function type,
    and a call on it. `f(n)` reads as both the parameter and the module fn,
    so it is the same ambiguity as the `tag: Str` case and is refused. The
    two #1823 cases with NO same-name module fn still lower, in
    tests/test_selfhost_lower_ir.py."""
    with pytest.raises(RevlError,
                       match="`f` is bound here and called in this body"):
        compile_source(
            "fn f(x: Int) -> Int { return x }\n"
            "service M { fn go(f: (Int) -> Int, n: Int) -> Int }\n"
            "component C provides m: M {\n"
            "  provide m { fn go(f, n) = f(n) }\n"
            "}\n",
            "t.rvl",
        )


def test_the_extern_half_names_the_module_extern():
    """The diagnostic spells the two kinds differently, and a provide-method
    parameter reaches it through the same newly recognised binder."""
    with pytest.raises(RevlError, match="module extern of that name is in scope"):
        compile_source(
            "extern pure fn stamp(s: Str) -> Str = @py { return s }\n"
            + "service Cache {\n  fn f(stamp: Str) -> Str\n}\n" + """
            component C provides cache: Cache {
              provide cache {
                fn f(stamp) = stamp("x")
              }
            }
            """,
            "t.rvl",
        )


# ------------------------------------------------ what must NOT be refused

def test_a_provide_parameter_that_only_shares_a_fn_name_is_admitted():
    """The newly recognised binder must not widen the refusal: only the CALL
    position is ambiguous. Here the parameter is a `Str` and is never
    called, which is the common shape
    (`backends/wasm/golden/functions.revl` declares `fn name(row: Row)`
    beside `fn make_row(id: Int, name: Str)`)."""
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
    module `fn` through `callables` — the arm the fix deliberately leaves
    ungated for an UNSCOPED name."""
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


def test_a_sibling_method_binding_another_name_does_not_hide_the_refusal():
    """Each provide method is checked as its own body, so the first method's
    `tag` parameter called there is still refused when a sibling method binds
    a different module fn name and never calls it."""
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
    with pytest.raises(RevlError, match=_REFUSED):
        compile_source(source, "t.rvl")


def test_a_sibling_methods_call_does_not_make_a_parameter_ambiguous():
    """A provide parameter binds only inside its own method. Here `f`'s
    `tag` parameter is never called there, and the call in `g` names the
    module fn, so nothing is ambiguous: the two methods are separate emitted
    bodies, not one scope."""
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

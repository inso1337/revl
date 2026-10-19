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
unseen, nothing refused the body, and `_lower_component_pure_expr`'s
`callables` arm (which is deliberately not gated on `name not in scope`)
lowered `tag("x")` to a call of the module `fn tag` while `env.bind_params`
renamed the parameter to `tag_` beside it. The parameter was dropped without a
word. The issue's own example is the reproducer below.

The `fn` body stratum already refused this (`fn shadowed(helper: …)`); this
file adds the provide-method half the acceptance list asks for, plus the two
cases that pin the precision of the newly recognised binder: a parameter that
merely SHARES a module fn's name is still admitted, and a provide method whose
call names a DIFFERENT module fn still compiles.
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
    with pytest.raises(RevlError, match="`tag` is bound here and called"):
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
    with pytest.raises(RevlError, match="`tag` is bound here and called"):
        compile_source(
            _TAG + """
            fn f(tag: (Str) -> Str) -> Str {
              return tag("x")
            }
            """,
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


def test_two_methods_binding_sibling_names_each_refuse_only_their_own():
    """The scan is per-declaration on the component's binder list, so a
    second provide method binding a DIFFERENT module fn name is judged on its
    own body, not on the first one's."""
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
    with pytest.raises(RevlError, match="`tag` is bound here and called"):
        compile_source(source, "t.rvl")

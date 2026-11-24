"""An annotated `let` in a provide method is checked, not trusted (#3212).

The provide-method `let` wrote its annotation into the type environment
without checking the value against it, so `let s: Str = maybe(u)` (an
`Opt[Str]`), `let n: Str = 5` and `let row: Row = { a: 5 }` compiled and the
local then had the written type. The same `let` in a `fn` body is refused
(T1/T2). The provide method now refuses it with the `fn` body's message, and so
does a `let` inside a match block arm lowered inline in a component body.
"""

import pytest

from revl import compile_source
from revl.errors import RevlError

PRELUDE = """type Row = { a: Str }
extern pure fn maybe(a: Str) -> Opt[Str] = @py { return None }
extern emission[log] fn logit(m: Str) -> Int = @py { return 0 }
service Ops { emission fn go(u: Str) -> Int }
"""


def _method(body: str) -> str:
    return PRELUDE + f"""component Agent provides ops: Ops {{
  provide ops {{
    fn go(u: Str) {{
{body}
      return 0
    }}
  }}
}}
"""


@pytest.mark.parametrize("binding, message", [
    ("let s: Str = maybe(u)", "`let s: Str` expects `Str`, got `Opt[Str]`"),
    ("let s: Str = 5", "`let s: Str` expects `Str`, got `Int`"),
    ("let row: Row = { a: 5 }", "field `a` of `Row` expects `Str`, got `Int`"),
])
def test_annotated_let_in_a_provide_method_is_checked(binding, message):
    with pytest.raises(RevlError) as err:
        compile_source(_method(f"      {binding}"), "let_3212.rvl")
    assert message in str(err.value)


def test_the_unannotated_let_is_still_refused_at_the_use():
    with pytest.raises(RevlError) as err:
        compile_source(_method("      let s = maybe(u)\n"
                               "      let r = emit logit(s)"), "let_3212.rvl")
    assert "expects `Str`, got `Opt[Str]`" in str(err.value)


def test_a_correctly_annotated_let_still_compiles():
    compile_source(_method("      let s: Str = u\n"
                           "      let row: Row = { a: s }\n"
                           "      let o: Opt[Str] = maybe(u)\n"
                           "      let f: Float = 1.5\n"
                           "      let r = emit logit(row.a)"), "let_3212.rvl")


def test_annotated_let_in_a_match_block_arm_is_checked():
    src = _method("""      let t = match maybe(u) {
        Some(x) => {
          let s: Str = 5
          s
        },
        None => "z"
      }
      let r = emit logit(t)""")
    with pytest.raises(RevlError) as err:
        compile_source(src, "let_3212.rvl")
    assert "`let s: Str` expects `Str`, got `Int`" in str(err.value)

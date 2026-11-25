"""A field read in a ternary branch is cloned when its base is reused (#3627).

The rust emitter's body-level reuse signal (`ctx.multi_use`) reached call
arguments and record-literal field values, but not the TAIL of an
`if`-expression branch: `decl.caps` in a ternary branch lowered to a move,
and a later read of the same field in the same record literal borrowed a moved
value — E0382, the emitted crate did not build. Branch tails now go through
`_by_value_field_clone` first (same as call arguments), so a reused non-Copy
field clones in the branch. Single-use and Copy tails stay bare, byte-identical.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

RUST = backend_emitter("rust")


def _emitted(source: str) -> str:
    emitted = RUST.emit(compile_source(source, "ternary_3627.rvl"))
    if isinstance(emitted, dict):
        return "\n".join(v for v in emitted.values() if isinstance(v, str))
    return str(emitted)


_UNION = """
type Decl = { caps: List[Str] }
type Ac = { ecaps: List[Str], unnamed: List[Str] }
fn union_into(xs: List[Str], ys: List[Str]) -> List[Str] {
  var out = xs
  var i = 0
  while (i < ys.length()) {
    if (out.indexOf(ys[i]) == 0 - 1) { out = out.push(ys[i]) }
    i += 1
  }
  return out
}
"""


def test_issue_repro_ternary_field_tail_clones_when_reused_after():
    """The issue's repro: the branch reads `decl.caps`, the same record
    literal reads it again in `unnamed`. The branch must clone (E0382)."""
    source = _UNION + """
fn widen(a: Ac, decl: Decl) -> Ac {
  return { ecaps: union_into(a.ecaps, decl.caps.length() > 0 ? decl.caps : ["*"]),
           unnamed: union_into(a.unnamed, decl.caps) }
}
"""
    text = _emitted(source)
    assert "{ decl.caps.clone() }" in text, text
    # the later read still borrows
    assert "&decl.caps" in text
    # and no bare moved tail remains
    assert "{ decl.caps }" not in text


def test_ternary_field_tail_single_use_stays_a_move():
    """Control: a base read only in the branch (the condition does not mention
    it) is not in `multi_use`, so the tail stays a move — byte-identical to
    before."""
    source = _UNION + """
fn pick(decl: Decl, b: Bool) -> List[Str] {
  return b ? decl.caps : ["*"]
}
"""
    text = _emitted(source)
    assert "{ decl.caps }" in text
    assert "decl.caps.clone()" not in text


def test_ternary_copy_field_tail_never_clones():
    """Control: a Copy field (Int) in a reused base's ternary branch stays bare."""
    source = """
type P = { y: Int, z: Int }
fn f(p: P) -> Int {
  return p.y > 0 ? p.y : p.z
}
"""
    text = _emitted(source)
    assert "p.y.clone()" not in text
    assert "{ p.y }" in text


def test_ternary_bare_name_tail_still_clones_on_reuse():
    """Control: the pre-existing bare-name branch-tail clone is unchanged."""
    source = """
fn f(xs: List[Str], b: Bool) -> List[Str] {
  let r = b ? xs : []
  return r
}
"""
    # `xs` is read in the branch and never again, so it stays a move; bind a
    # second read to force the clone path:
    source2 = """
fn f(xs: List[Str], b: Bool) -> Int {
  let r = b ? xs : []
  return r.length() + xs.length()
}
"""
    text = _emitted(source2)
    assert "{ xs.clone() }" in text, text
    text1 = _emitted(source)
    assert "{ xs }" in text1


@pytest.mark.parametrize("branch", ["then", "else"])
def test_either_branch_tail_clones(branch):
    """Both branch tails get the field clone, not only the `then` one."""
    if branch == "then":
        source = _UNION + """
fn widen(a: Ac, decl: Decl) -> Ac {
  return { ecaps: union_into(a.ecaps, decl.caps.length() > 0 ? decl.caps : ["*"]),
           unnamed: union_into(a.unnamed, decl.caps) }
}
"""
    else:
        source = _UNION + """
fn widen(a: Ac, decl: Decl) -> Ac {
  return { ecaps: union_into(a.ecaps, decl.caps.length() > 0 ? ["*"] : decl.caps),
           unnamed: union_into(a.unnamed, decl.caps) }
}
"""
    text = _emitted(source)
    assert "{ decl.caps.clone() }" in text, text
    assert "{ decl.caps }" not in text

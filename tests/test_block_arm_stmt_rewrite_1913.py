"""Issue #1913: a block match arm holding a non-`let` statement crashed the
module-privacy rewrite.

`compiler._rewrite_expr` walked an `ExprBlockArm`'s statements as if every one
were a `let` (`s.value`, `s.name`), so the first `ForStmt`/`WhileStmt`/`IfStmt`
raised `AttributeError: 'ForStmt' object has no attribute 'value'` -- but only
when `_apply_module_privacy` had something to rename, i.e. when two included
modules shared a private name.

A block arm accepts the same statement set a fn body does (`let`, `var`,
`while`, `if`, `for`, assignments, ...), so it must get the same statement walk
(`_rewrite_stmt`), which also threads the binders it introduces out of the arm's
tail. These tests pin both halves: that the walk no longer raises, and that a
private reference *inside* a block arm's statements is still renamed (a fix that
merely skipped unknown statement kinds would leave it resolving to the other
module's same-named private).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.test import test_command as _test_command  # noqa: E402


# `b.rvl` keeps a private `helper` with a DIFFERENT signature so the merged
# program must rename the two apart (the condition that makes the privacy pass
# run at all -- issue #1913's measured "no shared private name => admitted").
_B_SRC = (
    "fn helper(s: Str) -> Str { return s }\n"
    "pub fn shout(s: Str) -> Str { return helper(s) }\n"
)


def _write_modules(tmp_path, a_src: str, expected: int) -> str:
    (tmp_path / "a.rvl").write_text(a_src)
    (tmp_path / "b.rvl").write_text(_B_SRC)
    main = tmp_path / "main.rvl"
    main.write_text(
        'use "./a.rvl" { total }\n'
        'use "./b.rvl" { shout }\n'
        f'test "t" {{ assert total(Many, [1, 2]) == {expected} }}\n'
    )
    return str(main)


def _lifted_arms(ir):
    """The synthetic helper fns a block arm lowers into (`match_arm_N`)."""
    return [fn for fn in ir["functions"] if fn["name"].startswith("match_arm")]


def _calls(node) -> set[str]:
    """Every `var`-callee call name anywhere in an IR expression/statement."""
    out: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "callee" and isinstance(value, dict) and value.get("kind") == "var":
                out.add(value["name"])
            out |= _calls(value)
    elif isinstance(node, list):
        for item in node:
            out |= _calls(item)
    return out


def _a_helper_name(ir) -> str:
    """a.rvl's private `helper` after the privacy rename (Int-typed)."""
    cands = [fn["name"] for fn in ir["functions"]
             if fn["name"].startswith("helper__m") and fn["returns"] == "Int"]
    assert len(cands) == 1, [fn["name"] for fn in ir["functions"]]
    return cands[0]


def _assert_renamed_apart(ir):
    names = sorted(fn["name"] for fn in ir["functions"])
    assert "helper" not in names, names
    assert len([n for n in names if n.startswith("helper__m")]) == 2, names


def _compile_and_get_lifted(tmp_path, a_src: str, expected: int):
    ir = compile_files([_write_modules(tmp_path, a_src, expected)])
    _assert_renamed_apart(ir)
    arms = _lifted_arms(ir)
    assert len(arms) == 1, [fn["name"] for fn in ir["functions"]]
    return ir, arms[0]


def test_issue_1913_exact_reproducer(tmp_path):
    """The issue's reproducer byte for byte: `var` + `for` in the arm, a private
    `helper` call in the OTHER arm. Pre-fix this raised
    `AttributeError: 'ForStmt' object has no attribute 'value'`."""
    ir, arm = _compile_and_get_lifted(
        tmp_path,
        "type K = One | Many\n"
        "fn helper(xs: List[Int]) -> Int { return xs.length() }\n"
        "pub fn total(k: K, xs: List[Int]) -> Int {\n"
        "  return match k {\n"
        "    One => helper(xs),\n"
        "    Many => {\n"
        "      var n = 0\n"
        "      for (x of xs) { n += x }\n"
        "      n\n"
        "    }\n"
        "  }\n"
        "}\n",
        3,
    )
    assert [s["step"] for s in arm["body"]] == ["let", "for", "return"]
    assert _test_command(ir, "py") == 0


def test_block_arm_for_renames_private_call_in_loop_body(tmp_path):
    """A `for` inside the arm whose body calls the private helper: pre-fix the
    `ForStmt` raised, and a walk that merely skipped unknown statements would
    leave `helper` bare (resolving to b.rvl's Str one)."""
    ir, arm = _compile_and_get_lifted(
        tmp_path,
        "type K = One | Many\n"
        "fn helper(xs: List[Int]) -> Int { return xs.length() }\n"
        "pub fn total(k: K, xs: List[Int]) -> Int {\n"
        "  return match k {\n"
        "    One => 0,\n"
        "    Many => {\n"
        "      var n = 0\n"
        "      for (x of xs) { n += helper([x]) }\n"
        "      n\n"
        "    }\n"
        "  }\n"
        "}\n",
        2,
    )
    assert _a_helper_name(ir) in _calls(arm["body"])
    assert "helper" not in _calls(arm["body"])
    assert _test_command(ir, "py") == 0


def test_block_arm_while_renames_private_call(tmp_path):
    """A `while` in the arm (`WhileStmt` has no `.value` either)."""
    ir, arm = _compile_and_get_lifted(
        tmp_path,
        "type K = One | Many\n"
        "fn helper(xs: List[Int]) -> Int { return xs.length() }\n"
        "pub fn total(k: K, xs: List[Int]) -> Int {\n"
        "  return match k {\n"
        "    One => 0,\n"
        "    Many => {\n"
        "      var n = 0\n"
        "      while (n < 2) { n = n + 1 }\n"
        "      helper(xs) + n\n"
        "    }\n"
        "  }\n"
        "}\n",
        4,
    )
    assert _a_helper_name(ir) in _calls(arm["body"])
    assert "helper" not in _calls(arm["body"])
    assert _test_command(ir, "py") == 0


def test_block_arm_if_renames_private_call(tmp_path):
    """An `if` in the arm (`IfStmt` has no `.value` either)."""
    ir, arm = _compile_and_get_lifted(
        tmp_path,
        "type K = One | Many\n"
        "fn helper(xs: List[Int]) -> Int { return xs.length() }\n"
        "pub fn total(k: K, xs: List[Int]) -> Int {\n"
        "  return match k {\n"
        "    One => 0,\n"
        "    Many => {\n"
        "      var n = 0\n"
        "      if (xs.length() > 0) { n = helper(xs) }\n"
        "      n\n"
        "    }\n"
        "  }\n"
        "}\n",
        2,
    )
    assert _a_helper_name(ir) in _calls(arm["body"])
    assert "helper" not in _calls(arm["body"])
    assert _test_command(ir, "py") == 0


def test_block_arm_assignment_renames_private_call(tmp_path):
    """An assignment statement in the arm, whose RHS calls the private helper."""
    ir, arm = _compile_and_get_lifted(
        tmp_path,
        "type K = One | Many\n"
        "fn helper(xs: List[Int]) -> Int { return xs.length() }\n"
        "pub fn total(k: K, xs: List[Int]) -> Int {\n"
        "  return match k {\n"
        "    One => 0,\n"
        "    Many => {\n"
        "      var n = 0\n"
        "      n = helper(xs)\n"
        "      n + 1\n"
        "    }\n"
        "  }\n"
        "}\n",
        3,
    )
    assert _a_helper_name(ir) in _calls(arm["body"])
    assert "helper" not in _calls(arm["body"])
    assert _test_command(ir, "py") == 0


def test_block_arm_var_only_keeps_local_shadow(tmp_path):
    """A `var`-only arm whose binder SHADOWS the private name: the walk must
    thread the binder so the tail stays the local, not the renamed private."""
    ir, arm = _compile_and_get_lifted(
        tmp_path,
        "type K = One | Many\n"
        "fn helper(xs: List[Int]) -> Int { return xs.length() }\n"
        "pub fn total(k: K, xs: List[Int]) -> Int {\n"
        "  return match k {\n"
        "    One => 0,\n"
        "    Many => {\n"
        "      var helper = xs.length()\n"
        "      helper\n"
        "    }\n"
        "  }\n"
        "}\n",
        2,
    )
    tail = arm["body"][-1]
    assert tail["step"] == "return"
    assert tail["expr"] == {"kind": "var", "name": "helper"}
    assert _calls(tail) == set()
    assert _test_command(ir, "py") == 0

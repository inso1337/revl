"""Issue #2509: the type-alias sweep must walk every statement kind inside a
`match` block arm (`for`, `if`, `while`, and blocks nested in them)."""

from __future__ import annotations

import importlib.util
import sys
import types as pytypes
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.lower import _resolve_type_aliases  # noqa: E402
from revl.parser import ExprBlockArm, ForStmt, LetStmt, parse_text  # noqa: E402

FOR_SRC = """type Count = Int

fn pick(n: Int) -> Int {
  return match n {
    _ => {
      var total: Count = 0
      for (x of [1, 2]) {
        total = total + x
      }
      total
    }
  }
}
"""

IF_SRC = """type Count = Int

fn pick(n: Int) -> Int {
  return match n {
    _ => {
      var total: Count = 0
      if (n == 0) {
        total = 3
      }
      total
    }
  }
}
"""

WHILE_SRC = """type Count = Int

fn pick(n: Int) -> Int {
  return match n {
    _ => {
      var total: Count = 0
      while (total < 3) {
        total = total + 1
      }
      total
    }
  }
}
"""

NESTED_SRC = """type Count = Int

fn pick(n: Int) -> Int {
  return match n {
    _ => {
      var acc: Count = 0
      for (x of [1, 2]) {
        var total: Count = x
        acc = acc + match x {
          _ => {
            var inner: Count = 0
            for (y of [1]) {
              var step: Count = y
              inner = inner + step
            }
            inner
          }
        }
      }
      acc
    }
  }
}
"""


@pytest.mark.parametrize("src", [FOR_SRC, IF_SRC, WHILE_SRC, NESTED_SRC],
                         ids=["for", "if", "while", "nested"])
def test_block_arm_statements_compile_with_an_alias_declared(src):
    compile_source(src)


def _run_py(source: str):
    spec = importlib.util.spec_from_file_location(
        "revl_python_emit_2509", ROOT / "backends" / "python" / "emit.py")
    emit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit)
    module = pytypes.ModuleType("revl_2509")
    sys.modules["revl_2509"] = module
    try:
        exec(compile(emit.emit(compile_source(source)), "<2509>", "exec"),
             module.__dict__)
    finally:
        sys.modules.pop("revl_2509", None)
    return module


def test_for_in_block_arm_runs_on_the_py_tier():
    assert _run_py(FOR_SRC).pick(0) == 3


def _first_arm_block(program):
    body = program.fn_decls[0].body[0].expr.arms[0][2]
    assert isinstance(body, ExprBlockArm)
    return body


def test_alias_in_annotation_nested_in_a_block_arm_for_is_substituted():
    program = parse_text(FOR_SRC, "t.rvl")
    _resolve_type_aliases(program, "t.rvl")
    block = _first_arm_block(program)
    assert block.stmts[0].type == "Int"


def test_alias_in_annotation_nested_two_block_arms_deep_is_substituted():
    program = parse_text(NESTED_SRC, "t.rvl")
    _resolve_type_aliases(program, "t.rvl")
    outer = _first_arm_block(program)
    loop = next(s for s in outer.stmts if isinstance(s, ForStmt))
    assert loop.body[0].type == "Int"
    inner_block = loop.body[1].value.right.arms[0][2]
    inner_loop = next(s for s in inner_block.stmts if isinstance(s, ForStmt))
    assert isinstance(inner_loop.body[0], LetStmt)
    assert inner_loop.body[0].type == "Int"


LET_ONLY_SRC = """type Count = Int

fn pick(n: Int) -> Int {
  return match n {
    _ => {
      let x: Count = n
      x
    }
  }
}
"""


def test_let_only_block_arm_annotation_is_substituted():
    # no loop: the old code walked only `.value`, so the annotation survived
    # and the checker saw `Count` where `Int` was expected
    compile_source(LET_ONLY_SRC)
    assert _run_py(LET_ONLY_SRC).pick(4) == 4

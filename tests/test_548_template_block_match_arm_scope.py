"""#548 item 8 — a string template `${...}` inside a BLOCK-bodied match arm can
now see the arm binding and the enclosing fn's parameters.

The gap (component-author review REVL-REVIEW-2026-09-06): a block-bodied match
arm (`=> { let x = …; expr }`) is lambda-lifted into a synthetic helper fn, and
every enclosing name the block *reads* becomes a helper parameter. The
free-name collector (`lower._collect_arm_names`) walked lists and dataclass
nodes but NOT tuples — and `Interp.parts` is a list of `("text", str)` /
`("expr", <ast>)` tuples, so the interpolated expression rode in a tuple slot
the walk never descended into. Names read only inside a `${...}` were therefore
never captured, the lifted helper missed the parameter, and the arm was refused
at lowering with "`v` is not declared in this function (G1)" — even though the
SAME reference outside a template, or the SAME template in an EXPRESSION arm,
compiled fine.

The fix collects names from tuples exactly as from lists. It is a pure
front-end capture fix in the shared IR lowering: no new IR node and no emitter
change, so every tier renders the corrected helper call with the support it
already has.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402

emit = backend_emitter("python")

BACKENDS = ["python", "typescript", "go", "java", "rust", "wasm"]

# the motivating shape: a block-bodied `Ok` arm binds `q`, then a template reads
# BOTH the arm binding `q` and the enclosing parameter `v`; the `Err` arm's
# template reads `v` alone. Before the fix, lowering refused this at `${v}`.
# `d` is a parameter so the `Err` arm (a zero divisor) is reachable at runtime.
_SRC = """
fn describe(v: Int, d: Int) -> Str {
  return match v.checked_div_trunc(d) {
    Ok(q) => {
      let msg = `${v} over ${d} is ${q}`
      msg
    },
    Err(_) => `cannot divide ${v} by zero`,
  }
}
"""


def _run_py(source, fn, *args):
    ns = {}
    exec(compile(emit.emit(compile_source(source)), "emitted.py", "exec"), ns)
    return ns[fn](*args)


def test_template_in_block_match_arm_compiles():
    # the exact reproduction from the review — was refused at lowering.
    ir = compile_source(_SRC)
    assert ir["ir_version"] == 3


def test_lifted_helper_captures_both_free_vars():
    # the lifted helper must carry BOTH the arm binding (`q`) and the enclosing
    # parameters (`v`, `d`) it reads — the names read only inside the template.
    ir = compile_source(_SRC)
    lifted = [f for f in ir["functions"] if f["name"].startswith("match_arm_")]
    assert len(lifted) == 1, ir["functions"]
    param_names = {p["name"] for p in lifted[0]["params"]}
    assert param_names == {"v", "d", "q"}, param_names


def test_template_in_block_match_arm_runs_on_py():
    # runtime proof: the emitted python threads `v`, `d` and `q` into the helper.
    assert _run_py(_SRC, "describe", 10, 2) == "10 over 2 is 5"
    # the Err arm, whose template reads the enclosing `v` alone, also runs.
    assert _run_py(_SRC, "describe", 7, 0) == "cannot divide 7 by zero"


def test_every_backend_emits():
    # the fix lives in shared lowering; every tier must still emit cleanly with
    # no new emit support. Some emitters return a str, some a {section: str}
    # mapping — either way the emit must succeed and be non-empty.
    ir = compile_source(_SRC)
    for backend in BACKENDS:
        out = backend_emitter(backend).emit(ir)
        assert out, backend


def test_template_reading_only_the_arm_binding_runs():
    # a narrower shape: the template reads ONLY the arm binding, no enclosing
    # parameter — still captured, still runs.
    src = """
    fn label(v: Int) -> Str {
      return match v.checked_div_trunc(2) {
        Ok(q) => {
          let s = `q=${q}`
          s
        },
        Err(_) => `none`,
      }
    }
    """
    assert _run_py(src, "label", 8) == "q=4"


def test_nested_template_in_block_arm_runs():
    # a template nested inside another template's interpolation, both reading
    # captured names — the tuple walk must descend through both levels.
    src = """
    fn describe(v: Int) -> Str {
      return match v.checked_div_trunc(2) {
        Ok(q) => {
          let s = `outer ${`inner ${v}/${q}`}`
          s
        },
        Err(_) => `none`,
      }
    }
    """
    assert _run_py(src, "describe", 10) == "outer inner 10/5"

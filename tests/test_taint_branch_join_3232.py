"""Issue #3232: a branch assignment overwrote a variable's taint instead of
joining it, so a value tainted on one path reached a sink as clean on the
other — `var p = t; if (c) { p = "x" }; emit logit(p)` compiled, though the
fall-through path still holds `t`.

The taint walker now walks each `if` arm on its own copy of the environment
and joins every binding at the merge point (the fall-through included), the
same join a match arm gets. Loops already joined through their fixpoint.
"""

from __future__ import annotations

import pytest

from revl import RevlError
from revl.compiler import compile_source

_PRELUDE = (
    "extern emission[web] fn fetch(u: Str) -> Untrusted[Str] = @py { return u }\n"
    "extern emission[shell] fn run(cmd: Trusted[Str]) -> Int = @py { return 0 }\n"
    "extern emission[payment.charge] fn charge(a: Str) -> Secret[Str] "
    "= @py { return a }\n"
    "extern emission[log] fn logit(m: Str) -> Int = @py { return 0 }\n"
)


def _agent(body: str) -> str:
    return (
        _PRELUDE
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(u: Str) {\n" + body + "\n      return 0\n    }\n"
        + "  }\n}\n"
    )


def _refuses(src: str, code: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "flow.rvl")
    err = excinfo.value
    assert getattr(err, "code", None) == code, (
        f"expected {code}, got {getattr(err, 'code', None)}: {err.message}")
    return err


# ===========================================================================
# Refused: the join keeps the taint of every path.
# ===========================================================================

def test_a_direct_sink_call_is_refused_the_control():
    err = _refuses(_agent(
        "      let t = emit fetch(u)\n"
        "      let x = emit run(t)"), "G9")
    assert "`run`" in err.message


def test_if_without_else_overwriting_a_tainted_var_is_refused_3232():
    err = _refuses(_agent(
        "      let t = emit fetch(u)\n"
        "      var p: Str = t\n"
        "      if (u == \"x\") { p = \"clean\" }\n"
        "      let x = emit run(p)"), "G9")
    assert "`run`" in err.message and "`p`" in err.message or "argument 1" in err.message


def test_if_else_with_a_clean_arm_is_refused_in_either_order_3232():
    for if_line in ("if (u == \"x\") { p = \"clean\" } else { p = t }",
                    "if (u == \"x\") { p = t } else { p = \"clean\" }"):
        src = _agent(
            "      let t = emit fetch(u)\n"
            "      var p: Str = t\n"
            f"      {if_line}\n"
            "      let x = emit run(p)")
        err = _refuses(src, "G9")
        assert "`run`" in err.message


def test_a_nested_if_overwriting_a_tainted_var_is_refused_3232():
    _refuses(_agent(
        "      let t = emit fetch(u)\n"
        "      var p: Str = t\n"
        "      if (u == \"x\") { if (u == \"y\") { p = \"clean\" } }\n"
        "      let x = emit run(p)"), "G9")


def test_a_loop_body_overwriting_a_tainted_var_is_refused_3232():
    _refuses(_agent(
        "      let t = emit fetch(u)\n"
        "      var p: Str = t\n"
        "      for (i of [1, 2]) { p = \"clean\" }\n"
        "      let x = emit run(p)"), "G9")


def test_a_secret_overwritten_in_a_branch_is_refused_at_the_log_sink_3232():
    err = _refuses(_agent(
        "      let t = emit charge(u)\n"
        "      var p: Str = t\n"
        "      if (u == \"x\") { p = \"clean\" }\n"
        "      let x = emit logit(p)"), "G-SECRET-FLOW")
    assert "`logit`" in err.message


# ===========================================================================
# Admitted: clean controls keep compiling.
# ===========================================================================

def test_a_clean_var_overwritten_in_a_branch_still_compiles():
    compile_source(_agent(
        "      var p: Str = \"clean\"\n"
        "      if (u == \"x\") { p = \"also clean\" }\n"
        "      let x = emit run(p)"), "clean.rvl")


def test_a_branch_that_only_reassigns_the_tainted_value_keeps_it_tainted_but_a_clean_path_compiles():
    # the join is sound, not blanket: when the tainted value never exists, the
    # sink call compiles regardless of the branch
    compile_source(_agent(
        "      var p: Str = u\n"
        "      if (u == \"x\") { p = \"clean\" } else { p = u }\n"
        "      let x = emit logit(p)"), "clean2.rvl")

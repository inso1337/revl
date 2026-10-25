"""Match exhaustiveness over an applied generic variant (issue #2926).

`type Mb[T] = Jj(T) | Nn` with `match m { Nn => 0 }` over an `Mb[Int]` was
admitted and failed at runtime with "non-exhaustive match", while the same
program over a non-generic `type Mb = Jj(Int) | Nn` was refused at compile time
with "non-exhaustive match: missing case `Jj`". Lowering looked the scrutinee
up in its type table by the whole applied spelling (`Mb[Int]`) and found
nothing, so it asked no exhaustiveness question at all.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402

MB = "type Mb[T] = Jj(T) | Nn\n\n"
EI = "type Ei[L, R] = Le(L) | Ri(R) | Ne\n\n"

REFUSED = {
    # the program in the issue: a let bound to a match expression
    "issue_let": (MB + """
fn f(m: Mb[Int]) -> Int {
  let r = match m { Nn => 0 }
  return r
}
""", "missing case `Jj`"),
    "return_match": (MB + """
fn f(m: Mb[Int]) -> Int {
  return match m {
    Jj(x) => x,
  }
}
""", "missing case `Nn`"),
    # a statement-block arm, the match used for its statements
    "block_arm": (MB + """
fn f(m: Mb[Int]) -> Int {
  let r = match m {
    Nn => {
      let z = 0
      z
    },
  }
  return r
}
""", "missing case `Jj`"),
    "nested_payload": (MB + """
fn f(m: Mb[Mb[Int]]) -> Int {
  return match m {
    Nn => 0,
  }
}
""", "missing case `Jj`"),
    "multi_param": (EI + """
fn f(e: Ei[Int, Str]) -> Int {
  return match e {
    Le(x) => x,
  }
}
""", "missing cases `Ri`, `Ne`"),
    # the unknown-arm refusal applies to the applied form too
    "unknown_arm": (MB + """
fn f(m: Mb[Int]) -> Int {
  return match m {
    Jj(x) => x,
    Nn => 0,
    Zz => 1,
  }
}
""", "`Zz` is not a case of `Mb[Int]`"),
}

ADMITTED = {
    "both_cases": MB + """
fn f(m: Mb[Int]) -> Int {
  return match m {
    Jj(x) => x,
    Nn => 0,
  }
}
""",
    "wildcard": MB + """
fn f(m: Mb[Int]) -> Int {
  let r = match m {
    Nn => 0,
    _ => 1,
  }
  return r
}
""",
    "nested_both_cases": MB + """
fn f(m: Mb[Mb[Int]]) -> Int {
  return match m {
    Jj(inner) => match inner {
      Jj(x) => x,
      Nn => 1,
    },
    Nn => 0,
  }
}
""",
    "multi_param_all_cases": EI + """
fn f(e: Ei[Int, Str]) -> Int {
  return match e {
    Le(x) => x,
    Ri(s) => 1,
    Ne => 0,
  }
}
""",
    "multi_param_wildcard": EI + """
fn f(e: Ei[Int, Str]) -> Int {
  return match e {
    Ri(s) => 1,
    _ => 0,
  }
}
""",
}


def _compile(tmp_path: Path, source: str) -> dict:
    path = tmp_path / "prog.rvl"
    path.write_text(source)
    return compile_files([str(path)])


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_partial_match_over_applied_generic_is_refused(tmp_path, name):
    source, message = REFUSED[name]
    with pytest.raises(RevlError) as excinfo:
        _compile(tmp_path, source)
    assert message in str(excinfo.value)


@pytest.mark.parametrize("name", sorted(ADMITTED))
def test_exhaustive_match_over_applied_generic_is_admitted(tmp_path, name):
    ir = _compile(tmp_path, ADMITTED[name])
    assert ir["ir_version"] == 3


def test_non_generic_twin_message_is_the_same(tmp_path):
    generic = REFUSED["issue_let"][0]
    plain = generic.replace("type Mb[T] = Jj(T) | Nn", "type Mb = Jj(Int) | Nn") \
                   .replace("Mb[Int]", "Mb")
    messages = []
    for source in (generic, plain):
        with pytest.raises(RevlError) as excinfo:
            _compile(tmp_path, source)
        messages.append(str(excinfo.value).split("\n")[0].split(": ", 1)[-1])
    assert messages[0] == messages[1]


def test_unknown_arm_hint_names_the_declared_head(tmp_path):
    """The hint points at the declaration, which is spelled `type Mb`."""
    with pytest.raises(RevlError) as excinfo:
        _compile(tmp_path, REFUSED["unknown_arm"][0])
    hint = excinfo.value.hint or ""
    assert "`type Mb`" in hint
    assert "`type Mb[Int]`" not in hint


#: A council bound to an action and a consumer of its `Aggregate[Str]` answer
#: that names both dissent arms but not `Agreed`.
AGGREGATE_MISSING_AGREED = """
model role edge   on_device
model role local2 on_device

service Review {
  fn propose(plan: Str) -> Str
  fn settle(answer: Aggregate[Str]) -> Int
}

model council Release {
  proposer  -> local2,
  adversary -> edge,
  aggregate unanimous quorum declared on_tie split
}

component Reviewer provides out: Review {
  route model on propose {
    confidential -> Release
  }
  provide out {
    fn propose(plan) = plan
    fn settle(answer) = match answer {
      Split(d)     => 0,
      Inquorate(d) => 0,
    }
  }
}
"""


def test_provided_aggregate_missing_agreed_is_refused(tmp_path):
    """The provided `Aggregate[T]` is a generic variant like any other, so the
    general rule now refuses a consumer that leaves `Agreed` unnamed."""
    with pytest.raises(RevlError) as excinfo:
        _compile(tmp_path, AGGREGATE_MISSING_AGREED)
    assert "non-exhaustive match: missing case `Agreed`" in str(excinfo.value)

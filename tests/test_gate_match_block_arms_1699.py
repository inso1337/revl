"""The self-host gate reads a block `match` arm in a component body.

Issue #1699. A `match` arm may be a statement block, `Pat => { let ...; tail }`;
in a component body the reference lowers it inline (`_lower_component_block_arm`,
lower.py), the `let`s in the arm's own scope. The gate's statement reader had
no such form: it skipped the statement at the end of its line and read the rest
of the block as statements of the method, so a program the reference admits was
refused G1 on the arm's names. The gate now reads the arm (`ba_stmt`,
selfhost/lower.rvl).

tests/fixtures/match_block_arms/ is the corpus: `ok_` admitted, `g1_` and `g4_`
refused under that tag. This file pins the reference's verdict per
document; tests/test_gate_reference_census.py holds the gate to the same
verdicts.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.diagnostics import classify
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "match_block_arms"

_NOPE = "`nope` is not a declared requirement of C"
_PUT = "call to emission `k.put` must be marked `emit` (G4)"

REFUSED = {
    "g1_after_arm": ("G1", _NOPE),
    "g1_undeclared_in_arm": ("G1", _NOPE),
    "g1_undeclared_in_tail": ("G1", _NOPE),
    "g4_unmarked_in_arm": ("G4", _PUT),
    "g4_unmarked_in_tail": ("G4", _PUT),
}
ADMITTED = (
    "ok_in_if_body", "ok_let_bound", "ok_multiline", "ok_nested",
    "ok_rebind_after_arm", "ok_rebind_inside_arm", "ok_rebind_outer_name",
    "ok_record_arm", "ok_returned", "ok_semicolon", "ok_two_lets",
)


def test_the_corpus_is_the_refusals_and_the_admissions():
    assert sorted(p.stem for p in CORPUS.glob("*.rvl")) == sorted(
        list(REFUSED) + list(ADMITTED))


@pytest.mark.parametrize("stem", sorted(REFUSED))
def test_the_reference_refuses_the_document(stem):
    with pytest.raises(RevlError) as excinfo:
        compile_source((CORPUS / f"{stem}.rvl").read_text(), f"{stem}.rvl")
    code, message = REFUSED[stem]
    # a G1 is coded by `classify`, the census's own reading of a refusal
    assert classify(excinfo.value).get("code") == code
    assert excinfo.value.message == message


@pytest.mark.parametrize("stem", ADMITTED)
def test_the_reference_admits_the_document(stem):
    assert compile_source((CORPUS / f"{stem}.rvl").read_text(), f"{stem}.rvl")


# The rebind walk steps over an arm, but not over the statement that holds it:
# binding `x` again after `let x = match ...` is the reference's G6. Kept out of
# the corpus because the formal model has no fact about the G6 rebind rule, so
# the document would widen formal/out_of_fragment_ledger.json; the gate's twin
# is the in-file program "a name the block-arm statement binds is a G6 rebind"
# in selfhost/lower.rvl.
REBIND_AFTER_BLOCK_STATEMENT = """service S { fn go(n: Int) -> Int }
component C provides s: S {
  provide s {
    fn go(n: Int) {
      let o = Some(n)
      let x = match o {
        Some(v) => {
          let z = v + 1
          z
        },
        None => 0
      }
      let x = 2
      return x
    }
  }
}"""


def test_the_reference_refuses_a_rebind_of_the_block_arm_statement_s_name():
    with pytest.raises(RevlError) as excinfo:
        compile_source(REBIND_AFTER_BLOCK_STATEMENT, "rebind.rvl")
    assert classify(excinfo.value).get("code") == "G6"
    assert excinfo.value.message == "`x` is already bound in `go`"

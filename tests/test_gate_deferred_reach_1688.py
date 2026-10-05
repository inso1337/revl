"""The self-host gate refuses a deferred extern reached outside `emit`.

Issue #1688. Item 400, extended by issue #1457, refuses a `deferred` emission
extern called in a `fn` or `test` body (named "inside an arrow" when it is),
and one passed as a function value in a `fn`/`test` body or a component. The
reference refused each under G4; `selfhost/lower.rvl` raised no objection,
which no corpus document showed. The gate now mirrors the refusal byte for
byte (`deferred_reach_refusal`), in the reference's fail-fast position, just
ahead of the host-acquire walk.

tests/fixtures/deferred_reach/ is the corpus: `g4_` refused, `ok_` admitted.
This file pins the reference's verdict per document;
tests/test_gate_reference_census.py holds the gate to the same verdicts.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "deferred_reach"

_CALL = ("`deferred` emission extern `deliver` cannot be called {route}; a "
         "fn/test body has no session commit for the deferral to fire at (G4)")
_VALUE = ("`deferred` emission extern `deliver` is passed as a function value "
          "in {where}; whoever calls the value fires it at once, with no "
          "session commit (G4)")

REFUSED = {
    "g4_call_in_fn_body": _CALL.format(route="in the body of fn `bill`"),
    "g4_call_in_arrow_in_fn_body": _CALL.format(
        route="inside an arrow in the body of fn `run`"),
    "g4_call_in_test_body": _CALL.format(route="in the body of test `fires`"),
    "g4_value_in_fn_body": _VALUE.format(where="the body of fn `run`"),
    "g4_value_in_component": _VALUE.format(where="component `Agent`"),
    "g4_value_in_test_body": _VALUE.format(where="the body of test `hands it on`"),
}
ADMITTED = ("ok_emit_step", "ok_emit_tail", "ok_plain_value_in_fn_body")


def test_the_corpus_is_the_refusals_and_the_admissions():
    assert sorted(p.stem for p in CORPUS.glob("*.rvl")) == sorted(
        list(REFUSED) + list(ADMITTED))


@pytest.mark.parametrize("stem", sorted(REFUSED))
def test_the_reference_refuses_the_document(stem):
    with pytest.raises(RevlError) as excinfo:
        compile_source((CORPUS / f"{stem}.rvl").read_text(), f"{stem}.rvl")
    assert excinfo.value.code == "G4"
    assert excinfo.value.message == REFUSED[stem]


@pytest.mark.parametrize("stem", ADMITTED)
def test_the_reference_admits_the_document(stem):
    assert compile_source((CORPUS / f"{stem}.rvl").read_text(), f"{stem}.rvl")

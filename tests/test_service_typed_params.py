"""A call through a provide method's service-typed parameter is a crossing.

Issue #1682, decided as option 1: a call through a service-typed parameter
inside a provide method is a crossing of that service's declared emission
scopes, judged locally. It needs the `emit` marker, it meets the approval
floor, and it must fit the method's own declared upper bound. Judging it at
the caller would make a method's safety depend on its callers, and refusing
the parameter would ban capability passing.

Before, all three rules missed it: `p.charge(n)` crossed `production.payment`
(which requires approval) unmarked, unapproved, and outside a `Till.go`
declared `emission[audit.log]`, and a caller passing a spawn-handle provision
into `p` reached it end to end.

tests/fixtures/service_typed_params/ is the corpus: `g4_` refused, `ok_`
admitted. tests/test_gate_reference_census.py holds the self-host gate to the
same verdicts.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "service_typed_params"

REFUSED = {
    "g4_param_unmarked": "call to emission `p.charge` must be marked `emit` (G4)",
    "g4_param_end_to_end": "call to emission `p.charge` must be marked `emit` (G4)",
    "g4_param_if_expression": "call to emission `charge` must be marked `emit` (G4)",
    "g4_param_marked_no_edge": (
        "crossing capability `production.payment` requires approval, but "
        "this `emit` carries no covering `with` edge"),
    "g4_param_upper_bound": (
        "`Till.go` is declared `emission[audit.log]`, but this implementation "
        "emits through `production.payment` (reaching `Pay.charge`)"),
    "g4_param_plain": (
        "`Till.go` is declared plain, but this implementation reaches "
        "`Pay.charge`"),
}
ADMITTED = ("ok_param_approved", "ok_param_upper_bound", "ok_param_non_emission",
            "ok_param_no_approval")


def _src(stem: str) -> str:
    return (CORPUS / f"{stem}.rvl").read_text()


def test_the_corpus_is_the_refusals_and_the_admissions():
    assert sorted(p.stem for p in CORPUS.glob("*.rvl")) == sorted(
        list(REFUSED) + list(ADMITTED))


@pytest.mark.parametrize("stem", sorted(REFUSED))
def test_a_crossing_through_a_service_typed_parameter_is_refused(stem):
    with pytest.raises(RevlError) as excinfo:
        compile_source(_src(stem), f"{stem}.rvl")
    assert excinfo.value.code == "G4"
    assert excinfo.value.message == REFUSED[stem]


@pytest.mark.parametrize("stem", ADMITTED)
def test_capability_passing_within_the_rules_is_admitted(stem):
    assert compile_source(_src(stem), f"{stem}.rvl")


def test_an_unapplied_arrow_parameter_is_still_left_to_the_application():
    """The arrow-parameter exclusion is unchanged: an arrow whose
    service-typed parameter is never given a provision still compiles."""
    src = _src("ok_param_approved").replace(
        "      emit p.charge(n) with a\n",
        "      let f = (q: Pay, k: Int) => q.charge(k)\n")
    assert compile_source(src, "t.rvl")

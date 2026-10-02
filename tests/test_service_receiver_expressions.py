"""A call through any receiver whose static type is a service is a crossing.

Issue #1681. #1509 resolved a call through a `let`-bound local holding a
provision, and a field or element read off one. A receiver written in place,
`(if c { w.pay } else { w.pay }).charge(n)`, a `match`, a record or list
literal read in place, is the same crossing, and neither the marker rule nor
the approval floor read it: unmarked it was admitted, marked it was refused
as "not declared `emission`". The resolver (`lower._service_receiver_decl`)
now reads any receiver expression whose static type is a service, except one
that depends on a binder it does not decide: an arrow parameter (decided at
the application) and a provide method's own service-typed parameter (issue
#1682).

The self-host gate also noted nothing for an `emit` STEP through such a
receiver, so it raised no objection where the provider upper bound refuses
it; `g4_upper_bound_*` pins that.

tests/fixtures/service_receiver_expressions/ is the corpus: `g4_` refused,
`ok_` admitted. tests/test_gate_reference_census.py holds the self-host gate
to the same verdicts.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "service_receiver_expressions"
SHAPES = ("if", "match", "record", "list")

MARKER = "call to emission `charge` must be marked `emit` (G4)"
APPROVAL = ("crossing capability `production.payment` requires approval, but "
            "this `emit` carries no covering `with` edge")


def _src(stem: str) -> str:
    return (CORPUS / f"{stem}.rvl").read_text()


def _refusal(stem: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(_src(stem), f"{stem}.rvl")
    return excinfo.value


def test_the_corpus_is_three_documents_per_shape_two_controls_and_the_bound():
    want = sorted([f"g4_{s}_unmarked" for s in SHAPES]
                  + [f"g4_{s}_marked" for s in SHAPES]
                  + [f"ok_{s}_approved" for s in SHAPES]
                  + ["ok_if_no_approval", "ok_arrow_param_expression_unapplied",
                     "g4_upper_bound_if_inline", "g4_upper_bound_if_local"])
    assert sorted(p.stem for p in CORPUS.glob("*.rvl")) == want


@pytest.mark.parametrize("shape", SHAPES)
def test_an_unmarked_crossing_through_the_receiver_is_refused(shape):
    err = _refusal(f"g4_{shape}_unmarked")
    assert (err.code, err.message) == ("G4", MARKER)


@pytest.mark.parametrize("shape", SHAPES)
def test_a_marked_crossing_through_the_receiver_meets_the_floor(shape):
    err = _refusal(f"g4_{shape}_marked")
    assert (err.code, err.message) == ("G4", APPROVAL)


@pytest.mark.parametrize("shape", SHAPES)
def test_the_marked_and_approved_crossing_is_admitted(shape):
    assert compile_source(_src(f"ok_{shape}_approved"), "t.rvl")


@pytest.mark.parametrize("stem", ["ok_if_no_approval",
                                  "ok_arrow_param_expression_unapplied"])
def test_the_controls_are_admitted(stem):
    assert compile_source(_src(stem), f"{stem}.rvl")


def test_a_method_parameter_receiver_is_left_to_its_own_rule():
    """A provide method's own service-typed parameter is issue #1682's, and a
    receiver that depends on one is not judged here."""
    src = ("service Pay { emission fn charge(cents: Int) -> Int }\n"
           "service Till { emission fn go(p: Pay, n: Int) -> Int }\n"
           "component Register provides till: Till {\n"
           "  provide till {\n"
           "    fn go(p, n) {\n"
           "      let x = (if (n > 0) { p } else { p }).charge(n)\n"
           "      return x\n"
           "    }\n  }\n}\n")
    assert compile_source(src, "t.rvl")


@pytest.mark.parametrize("stem", ["g4_upper_bound_if_inline",
                                  "g4_upper_bound_if_local"])
def test_a_step_through_the_receiver_meets_the_provider_upper_bound(stem):
    """A step crossing through a service-typed receiver is a host emission
    over the unnameable boundary, as every non-requirement step head is, so a
    scoped `emission[...]` bound refuses it. The self-host gate raised no
    objection to either document before this change."""
    err = _refusal(stem)
    assert (err.code, err.message) == ("G4", (
        "`Till.go` is declared `emission[production.payment]`, but this "
        "implementation emits through an unnameable host boundary (reaching "
        "`a host emission`)"))

"""A call through any receiver whose static type is a service is a crossing.

Issue #1681. #1509 resolved a call through a `let`-bound local holding a
provision, and a field or element read off one. A receiver written in place,
`(if c { w.pay } else { w.pay }).charge(n)`, a `match`, a record or list
literal read in place, is the same crossing, and neither the marker rule nor
the approval floor read it: unmarked it was admitted, marked it was refused
as "not declared `emission`". The resolver (`lower._service_receiver_decl`)
now reads any receiver expression whose static type is a service, except one
that depends on a binder it does not decide: an arrow parameter (decided at
the application). A provide method's own service-typed parameter is decided
since issue #1682.

A `match` arm written as a statement block lowers to a `do` node, which had no
type, so a block-arm receiver was not a service at all (issue #1729). The node
is typed by its tail now, read with the arm's own `let`s in scope.

The provider upper bound reads a crossing through such a receiver at the op's
declared scope (issue #1508): `ok_upper_bound_*` fits a covering bound and
`g4_upper_bound_*` does not.

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
SHAPES = ("if", "match", "match_block", "record", "list")

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
                     "g4_upper_bound_if_inline", "g4_upper_bound_if_local",
                     "ok_upper_bound_if_inline", "ok_upper_bound_if_local",
                     "g4_match_block_tail_local_unmarked"])
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


def test_a_method_parameter_receiver_is_judged_since_issue_1682():
    """A provide method's own service-typed parameter was left out here and
    decided by issue #1682: a call through it is a crossing judged in the
    method, so a receiver written in place over one is refused unmarked."""
    src = ("service Pay { emission fn charge(cents: Int) -> Int }\n"
           "service Till { emission fn go(p: Pay, n: Int) -> Int }\n"
           "component Register provides till: Till {\n"
           "  provide till {\n"
           "    fn go(p, n) {\n"
           "      let x = (if (n > 0) { p } else { p }).charge(n)\n"
           "      return x\n"
           "    }\n  }\n}\n")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    assert excinfo.value.message == MARKER


@pytest.mark.parametrize("stem", ["if_inline", "if_local"])
def test_a_step_through_the_receiver_is_read_at_the_ops_scope(stem):
    """Issue #1508: the provider upper bound reads a crossing through a
    service-typed receiver at the op's declared scope, as it reads a
    parameter's (#1682). A bound covering `production.payment` admits it; one
    that does not refuses it, naming the scope and the op."""
    assert compile_source(_src(f"ok_upper_bound_{stem}"), "t.rvl")
    err = _refusal(f"g4_upper_bound_{stem}")
    assert (err.code, err.message) == ("G4", (
        "`Till.go` is declared `emission[audit.log]`, but this implementation "
        "emits through `production.payment` (reaching `Pay.charge`)"))


def test_a_block_arm_whose_tail_is_its_own_let_is_the_same_crossing():
    """Issue #1729: the block arm binds the provision to a `let` of its own and
    yields it. The tail is typed with the arm's `let`s in scope."""
    err = _refusal("g4_match_block_tail_local_unmarked")
    assert (err.code, err.message) == ("G4", MARKER)

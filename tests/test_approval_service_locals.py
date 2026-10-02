"""A crossing through a service-typed local meets the marker rule and the floor.

Issue #1509. A spawn-handle provision can be held by a local in more ways than
the direct read and its plain alias: chosen by an `if`, stored in a record
field, stored in a list element, read back out of one. The marker rule read an
`if`-bound local as a crossing through its service type, but the approval floor
never resolved a call whose receiver is a local, so an approval-required
crossing went through with no edge. Through a record field or a list element
neither rule saw the crossing at all: unmarked, it was admitted with no marker
and no approval, and marked, it was refused as "not declared `emission`".

One resolver now answers both rules (`lower._service_receiver_decl`): the
receiver is rooted at a `let`-bound local and its static type is a service. An
arrow parameter is not such a local, because what flows into it is decided at
the application, so an arrow never applied to a provision still compiles.

tests/fixtures/approval_service_locals/ is the corpus: `g4_` is refused, `ok_`
is admitted. The handle, alias and applied-arrow spellings were closed by PR
#1519 and are pinned here beside the rest. tests/test_gate_reference_census.py
holds the self-host gate to the same verdicts.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "approval_service_locals"

APPROVAL = ("crossing capability `production.payment` requires approval, but "
            "this `emit` carries no covering `with` edge")


def _marker(spelled: str) -> str:
    return f"call to emission `{spelled}` must be marked `emit` (G4)"


REFUSED = {
    "g4_if_local_step": APPROVAL,
    "g4_if_local_value": APPROVAL,
    "g4_if_local_unmarked": _marker("t.charge"),
    "g4_record_field_step": APPROVAL,
    "g4_record_field_unmarked": _marker("r.p.charge"),
    "g4_record_alias_step": APPROVAL,
    "g4_list_element_step": APPROVAL,
    "g4_list_element_unmarked": _marker("charge"),
    "g4_handle_direct_value": APPROVAL,
    "g4_alias_step": APPROVAL,
    "g4_arrow_param_applied": APPROVAL,
}
ADMITTED = ("ok_if_local_step", "ok_record_field_step", "ok_record_alias_step",
            "ok_list_element_step", "ok_alias_step", "ok_if_local_no_approval",
            "ok_arrow_param_unapplied")
# each refusal's admitted spelling: the same crossing with the approval edge
TWIN = {
    "g4_if_local_step": "ok_if_local_step",
    "g4_if_local_value": "ok_if_local_step",
    "g4_if_local_unmarked": "ok_if_local_step",
    "g4_record_field_step": "ok_record_field_step",
    "g4_record_field_unmarked": "ok_record_field_step",
    "g4_record_alias_step": "ok_record_alias_step",
    "g4_list_element_step": "ok_list_element_step",
    "g4_list_element_unmarked": "ok_list_element_step",
    "g4_handle_direct_value": "ok_alias_step",
    "g4_alias_step": "ok_alias_step",
    "g4_arrow_param_applied": "ok_arrow_param_unapplied",
}


def _src(stem: str) -> str:
    return (CORPUS / f"{stem}.rvl").read_text()


def test_the_corpus_is_the_refusals_and_their_twins():
    assert sorted(p.stem for p in CORPUS.glob("*.rvl")) == sorted(
        list(REFUSED) + list(ADMITTED))
    assert set(TWIN) == set(REFUSED)
    assert set(TWIN.values()) <= set(ADMITTED)


@pytest.mark.parametrize("stem", sorted(REFUSED))
def test_a_crossing_through_a_service_typed_local_is_refused(stem):
    with pytest.raises(RevlError) as excinfo:
        compile_source(_src(stem), f"{stem}.rvl")
    assert excinfo.value.code == "G4"
    assert excinfo.value.message == REFUSED[stem]


@pytest.mark.parametrize("stem", ADMITTED)
def test_the_approved_or_unbound_spelling_is_admitted(stem):
    compile_source(_src(stem), f"{stem}.rvl")


def test_an_unmarked_crossing_through_a_record_field_is_refused_as_unmarked():
    """The (b) hole was a marker-rule hole first: the crossing was admitted
    with neither the marker nor the approval. With the floor inert (no
    `requires approval`), the marker rule alone still refuses it."""
    src = _src("g4_record_field_unmarked").replace(
        " requires approval =", " =")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    assert excinfo.value.message == _marker("r.p.charge")


def test_a_marked_crossing_through_a_record_field_is_an_emission():
    """Marked, the same call was refused as "not declared `emission`". The
    resolver reads the field's service type, so it is the emission it is."""
    src = _src("ok_record_field_step")
    assert compile_source(src, "t.rvl")

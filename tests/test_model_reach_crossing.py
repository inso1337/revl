"""The model reach fold reads the roles a component's crossings are placed on.

Issue #1193 slice 2, with issue #1451 on the same lines. Item 519 folds a
`model role`'s declared reach into the component that consults it, and refuses
a role reaching past the component (G-MODEL-PLACE). Before this change the fold
ran only for a component that wrote a `route model` block, so the same program
with the block deleted was admitted: deleting a declaration widened the
authority, and item 544's kernel check saw no `model_reach` row to read. A
crossing placed on a role by its `model.<role>` token (item 512 slice 4) is now
an edge of the product with or without the block.

The held set also reads every crossing in every position, as the spawn fold
does since issue #1562, so a component that crosses a model boundary only as a
value no longer looks like one that consults no model.

Issue #1451: a bare `emission` folds to an element of its SERVICE, which the
refusal rendered as the bare service name, so `reaches [Model]` against it
printed `Model` on both sides of the "but". It now names the unscoped emission
and the fix.

tests/fixtures/model_reach_crossing/ holds the corpus: `model_` is refused,
`ok_` is the admitted control. tests/test_gate_reference_census.py holds the
self-host gate to the same verdicts, byte for byte.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError
from revl.kernel_boundary import effective_from_model_reach

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "model_reach_crossing"
CODE = "G-MODEL-PLACE"
TAIL = (" - a component's effective ceiling is the pair's, so a model may not "
        "reach past the component that consults it (G-MODEL-PLACE)")

REFUSED = ("extern_block_value", "extern_no_block", "helper_no_block",
           "req_block", "req_no_block", "undeclared_no_block",
           "unscoped_emission")
ADMITTED = ("extern_block_value", "extern_no_block", "helper_no_block",
            "req_block", "req_no_block", "scoped_emission")

_CROSSES = "`Classifier` crosses `model.tool`, placed on model role `tool`"
_ROUTES = "`Classifier` routes `classify` (*) through model role `tool`"
MESSAGE = {
    "extern_block_value": (_ROUTES + ", which reaches `shell.exec`, but "
                           "`Classifier` holds only `*`" + TAIL),
    "extern_no_block": (_CROSSES + ", which reaches `shell.exec`, but "
                        "`Classifier` holds only `*`" + TAIL),
    "helper_no_block": (_CROSSES + ", which reaches `shell.exec`, but "
                        "`Classifier` holds only `*`" + TAIL),
    "req_block": (_ROUTES + ", which reaches `shell.exec`, but `Classifier` "
                  "holds only `model.tool`" + TAIL),
    "req_no_block": (_CROSSES + ", which reaches `shell.exec`, but "
                     "`Classifier` holds only `model.tool`" + TAIL),
    "undeclared_no_block": (_CROSSES + ", which reaches an unnameable host "
                            "boundary, but `Classifier` holds only "
                            "`model.tool`" + TAIL),
    # issue #1451's reproducer, pinned whole
    "unscoped_emission": (
        "`Classifier` routes `classify` (*) through model role `local`, which "
        "reaches `Model`, but `Classifier` holds only service `Model`'s "
        "unscoped emission (an unscoped emission has no token a `reaches "
        "[...]` list can name: give the `emission` methods of `Model` a "
        "scoped capability, such as `emission[model.complete]`, and reach "
        "that)" + TAIL),
}


def _src(stem: str) -> str:
    return (CORPUS / f"{stem}.rvl").read_text()


def _refusal(stem: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(_src(stem), f"{stem}.rvl")
    return excinfo.value


def test_the_corpus_is_the_refusals_and_their_controls():
    names = sorted(p.stem for p in CORPUS.glob("*.rvl"))
    assert names == sorted([f"model_{s}" for s in REFUSED]
                           + [f"ok_{s}" for s in ADMITTED])


@pytest.mark.parametrize("stem", REFUSED)
def test_a_role_reaching_past_its_component_is_refused(stem):
    err = _refusal(f"model_{stem}")
    assert err.code == CODE
    assert err.message == MESSAGE[stem]


@pytest.mark.parametrize("stem", ADMITTED)
def test_a_role_within_its_component_is_admitted(stem):
    ir = compile_source(_src(f"ok_{stem}"), f"ok_{stem}.rvl")
    assert ir["manifest"]["model_reach"], "an admitted pair is recorded"


@pytest.mark.parametrize("stem", ["req", "extern", "helper"])
def test_deleting_the_block_does_not_take_the_role_out(stem):
    """The fail-open itself: the verdict cannot depend on whether the
    `route model` block is written."""
    blocked = _src("model_req_block") if stem == "req" else _src(
        "model_extern_block_value")
    assert "route model on classify" in blocked
    err = _refusal(f"model_{stem}_no_block")
    assert err.code == CODE
    assert "route model" not in _src(f"model_{stem}_no_block").split(
        "component Classifier", 1)[1]
    assert "Leaving out the `route model` block does not take the role out " \
           "of the product" in err.hint


def test_a_crossing_row_is_recorded_and_the_kernel_reads_it():
    """An admitted no-block pair gets a `model_reach` row marked with the
    crossing, and item 544's consumer reads a non-empty ceiling off it, which
    is what it could not do while the component had no row at all."""
    ir = compile_source(_src("ok_req_no_block"), "ok_req_no_block.rvl")
    rows = ir["manifest"]["model_reach"]
    assert rows == [{
        "component": "Classifier", "action": "*", "origin": "*",
        "role": "tool", "residence": "on_device",
        "holds": ["model.tool"], "reaches": ["model.tool"],
        "effective": ["model.tool"], "attenuated": [],
        "reach_declared": True, "crossing": "model.tool",
    }]
    assert effective_from_model_reach("Classifier", ir["manifest"])


def test_a_block_row_carries_no_crossing_key():
    """Slice 1's record is byte-identical for a role the block names."""
    ir = compile_source(_src("ok_req_block"), "ok_req_block.rvl")
    assert [sorted(r) for r in ir["manifest"]["model_reach"]] == [sorted([
        "component", "action", "origin", "role", "residence", "holds",
        "reaches", "effective", "attenuated", "reach_declared"])]


def test_a_crossing_edge_cites_the_component_line():
    err = _refusal("model_req_no_block")
    line = _src("model_req_no_block").split("\n")[err.line - 1]
    assert line.startswith("component Classifier ")

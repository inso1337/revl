"""The spawn fold reads a model role's reach, not only the child's crossings.

Issue #1193 slice 3. Item 519 folds a model role's declared reach into the
component that consults it, and slice 2 (issue #1451) made that fold run for a
crossing placed on a role by its `model.<role>` token with or without a
`route model` block. The spawn attenuation fold was not reading the product:
it compared the child's OWN crossings against the spawner's held set, so a role
reaching past the child was invisible to it and a supervisor could spawn a
child that reached a boundary the supervisor does not hold - a real lineage
widening, and the exact fail-open direction G4 exists to refuse.

Before this change the corpus below admitted both `model_` programs:

    Supervisor holds ["model.complete"], spawns Worker,
    granted ["model.complete"], attenuated []

while `Worker`'s role `tool` reached `shell.exec`. The fix folds
`_strip_ceilings(_model_reach_caps(role))` per component into the surface
`_spawn_surface_closure` starts from, so the existing closure carries the
role's reach up the lineage and the existing monotone-shrinkage refusal fires
verbatim - no new code, no new message. The local base stays the child's own
crossings, so `holds` still names what the spawner actually holds.

tests/fixtures/model_reach_spawn/ holds the corpus: `model_` is refused under
G4, `ok_` is the admitted control. tests/test_gate_reference_census.py records
the self-host gate's half of it: the two `model_` documents are `false-admit/G4`
bypasses, named in `KNOWN_BYPASSES` and recorded in
`tools/gate_reference_census_baseline.json`, while the two `ok_` controls are
`agree-admit`. The gate half of this slice is a follow-up to the port of slice
2, so the census reports the divergence until it lands.
"""

from pathlib import Path

import pytest

from revl.compiler import compile_source
from revl.errors import RevlError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "model_reach_spawn"
CODE = "G4"
CATEGORY = "capability-attenuation"
TAIL = " — a spawn may narrow a child's capabilities, never widen them"
HINT = ("a spawned child's capability set must be covered by its spawner's "
        "(attenuation, item 66/294); `{spawner}` cannot pass down `shell.exec` "
        "it does not hold; add the matching `requires` to `{spawner}` so it "
        "holds what it grants, or narrow the capability on `{child}` (monotone "
        "shrinkage: narrowing is sound, widening is not)")

REFUSED = ("child_role_reach", "grandchild_role_reach")
ADMITTED = ("role_within_the_spawner", "spawner_holds_the_role_reach")
MESSAGE = {
    # the role's reach is reached by the child itself
    "child_role_reach": ("`Supervisor` spawns `Worker`, granting it "
                         "`shell.exec`, but `Supervisor` holds only "
                         "`model.complete`" + TAIL),
    # the same reach, one spawn edge further up
    "grandchild_role_reach": ("`Top` spawns `Mid`, granting it `shell.exec`, "
                              "but `Top` holds only `model.complete`" + TAIL),
}
SPAWNER = {"child_role_reach": "Supervisor", "grandchild_role_reach": "Top"}
CHILD = {"child_role_reach": "Worker", "grandchild_role_reach": "Mid"}
LINE = {"child_role_reach": 24, "grandchild_role_reach": 18}


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
def test_a_child_whose_role_reaches_past_the_spawner_is_refused(stem):
    err = _refusal(f"model_{stem}")
    assert err.code == CODE
    assert err.category == CATEGORY
    assert err.message == MESSAGE[stem]
    assert err.hint == HINT.format(spawner=SPAWNER[stem], child=CHILD[stem])
    # the refusal is the spawn statement's, not the model product's
    assert err.line == LINE[stem]


@pytest.mark.parametrize("stem", ADMITTED)
def test_a_role_reach_the_spawner_holds_is_admitted(stem):
    ir = compile_source(_src(f"ok_{stem}"), f"ok_{stem}.rvl")
    assert [i["granted"] for i in ir["manifest"]["instances"]], \
        "an admitted spawn is recorded"


def test_the_granted_set_records_the_role_reach_the_spawn_actually_passes():
    """The chain record, not just the verdict. `Worker`'s role `tool` reaches
    `shell.exec` and `Worker` holds it, so the spawn really does pass it down:
    before the fold the record claimed `granted` was `["model.complete"]` and
    `attenuated` `["shell.exec"]`, which was false - the child kept the
    capability. The fold makes the record name it."""
    ir = compile_source(_src("ok_spawner_holds_the_role_reach"),
                        "ok_spawner_holds_the_role_reach.rvl")
    assert ir["manifest"]["instances"] == [{
        "parent": "Supervisor", "child": "Worker",
        "holds": ["model.complete", "shell.exec"],
        "granted": ["model.complete", "shell.exec"],
        "attenuated": [], "line": 22,
    }]


def test_the_fold_does_not_move_the_verdict_when_the_spawner_holds_it():
    """The control for the record above: `Worker`'s own crossings are
    `model.complete`, which `Supervisor` holds, and the role's reach is
    `shell.exec`, which it also holds. Widening the comparison must not widen
    the verdict - the same shape with the missing `requires` is the refusal."""
    ir = compile_source(_src("ok_spawner_holds_the_role_reach"),
                        "ok_spawner_holds_the_role_reach.rvl")
    rows = ir["manifest"]["model_reach"]
    assert [r["component"] for r in rows] == ["Worker"]
    assert rows[0]["reaches"] == ["shell.exec"]
    assert rows[0]["attenuated"] == ["model.complete"]


def test_removing_the_route_model_arm_admits():
    """The fail-open direction, pinned: the refusal is caused by the role's
    reach and not by a role appearing in a spawn lineage. Delete the arm that
    places the crossing on `tool` and the same program admits - which is why
    leaving the arm out is not a way to narrow the spawn."""
    src = _src("model_child_role_reach")
    no_arm = src.replace("  route model on go { * -> tool }\n", "")
    assert no_arm != src
    assert "route model" not in no_arm.split("component Worker", 1)[1]
    ir = compile_source(no_arm, "no_arm.rvl")
    assert ir["manifest"]["instances"][0]["granted"] == ["model.complete"]


def test_the_model_product_admits_the_child_the_spawn_fold_refuses():
    """Slice 3 is a spawn rule, not a model rule. `Worker` holds `shell.exec`
    itself, so item 519's own fold admits it; only the lineage does not. Drop
    the spawner and the child compiles, with its `model_reach` row intact."""
    child = _src("model_child_role_reach").split("component Supervisor", 1)[0]
    ir = compile_source(child, "child.rvl")
    rows = ir["manifest"]["model_reach"]
    assert [r["component"] for r in rows] == ["Worker"]
    assert rows[0]["holds"] == ["model.complete", "shell.exec"]
    assert rows[0]["reaches"] == ["shell.exec"]
    assert rows[0]["effective"] == ["model.complete", "shell.exec"]

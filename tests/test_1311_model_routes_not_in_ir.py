"""Issue #1311: `route model` is checked and then dropped, and that is the
contract rather than a leak.

Two halves, and they are independent.

**The contract.** `docs/design/531-model-placement.md` section 4 says item 512
is a permission and not a scheduler, so the block contributes nothing to the
IR. `tests/test_model_placement_512.py::test_an_admitted_placement_writes_no_ir`
pins that by compiling the same program twice, once with the block and once
without, and comparing. That is a good test of "the block adds nothing" and a
weak test of "a consumer cannot read a route out of the artifact": it compares
two documents rather than looking at either one, so a section added to BOTH
compilations would satisfy it.

These tests look at the document. They scan a compiled composition for the
whole route vocabulary at every depth, and they assert the one fact a consumer
CAN have, the declared action set, which is the provide block's method names.
Together that is section 4.1's table, executable: what the artifact carries,
what it does not, and therefore what a seam holding only a linked composition
must be handed by value instead of deriving.

If a later slice does add a section (section 8's S4 or S5 are the two that
would justify one), these fail, and the note they name is where the reason
goes.

**The ruleset digest.** Section 4.2. `attest.RULESET_MODULES` is the set of
modules whose bytes `attest.ruleset_digest()` folds into the identity an
attestation publishes for the checker that admitted the composition. Its
membership rule is issue #989's: a module whose bytes move the set of programs
the frontend refuses is a rule. `model_route.py` is such a module twice over:
it raises item 512's own refusals, and `CEILING_ORIGINS` is read by `taint.py`
to decide whether item 514's value-level refusal fires at all. So is
`model_council.py`, which `lower.py` calls and which refuses under
`model_route.CODE`. The tests below pin membership, pin that membership is
load-bearing (the bytes really do move the digest, and the edit used really
does change what is refused), and pin that it is a digest input and not a
cited code.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError  # noqa: E402
from revl import attest  # noqa: E402
from revl import model_route as MR  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.parser import Parser  # noqa: E402

# A routed component with TWO actions, so "the routed action" and "an action
# the component declares" are different questions and a test cannot pass by
# confusing them. `classify` is routed; `other` is not.
ROUTED = """
model role local on_device
model role cloud off_device

service Answer {
  fn classify(text: Str) -> Str
  fn other(text: Str) -> Str
}

component Classifier provides out: Answer {
  route model on classify {
    confidential -> local,
    * -> cloud
  }
  provide out {
    fn classify(text) = text
    fn other(text) = text
  }
}
"""

# Section 3.2's reproducer, verbatim from the note: a `Secret[Str]` config
# field reaching a `model.complete` crossing from an action routed `* -> cloud`.
# The refusal is item 514's, decided by `model_route.CEILING_ORIGINS`.
CEILING = """
model role local on_device
model role cloud off_device

extern emission[model.complete] fn prompt(p: Secret[Str]) -> Int = @py { return 0 }

service Answer { emission fn summarize(d: Str) -> Int }

component Summarizer provides out: Answer {
  config { doc: Secret[Str] }
  route model on summarize { * -> cloud }
  provide out {
    fn summarize(d) {
      let r = prompt(config.doc)
      return 0
    }
  }
}
"""


def _strings(node):
    """Every string in the document, keys and values alike, at every depth.

    Keys as well as values because a section would arrive as a key
    (`modelRoutes`) whose contents are values, and a test that read only one
    of the two would miss half of what it is looking for.
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _strings(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _strings(item)


# ===========================================================================
# 1. What the artifact does not carry
# ===========================================================================

def test_the_linked_ir_carries_no_word_of_the_route_vocabulary():
    """The scan section 4.1's table is built from.

    Every token the surface of section 2 can produce: the two residences, the
    two role names the program declares, the origin an arm names, the catch-all
    and the spellings a section would plausibly be given. None of them appears
    anywhere in the compiled document.
    """
    ir = compile_source(ROUTED, "routed.rvl")
    blob = json.dumps(ir)
    for word in (*MR.RESIDENCES, *MR.CONFIDENTIALITY_ORIGINS,
                 "modelRoutes", "modelRoute", "model_route", "modelRoles",
                 "model_roles", "routeModel", "residence", "arms"):
        assert word not in blob, (
            f"{word!r} reached the linked IR. `route model` contributes no IR "
            f"(docs/design/531-model-placement.md section 4); if that changed "
            f"deliberately, section 4.1 is where the reason goes and all six "
            f"emitters owe it a handler or a refusal by name"
        )

    # The role names are the program's own identifiers, so check them as whole
    # strings rather than as substrings of a larger blob.
    flat = set(_strings(ir))
    assert "local" not in flat and "cloud" not in flat


def test_no_top_level_section_appeared_beside_the_four_the_document_has():
    """A section would arrive as a fifth top-level key. Naming the four the
    document has makes that a refusal here rather than a silent widening."""
    ir = compile_source(ROUTED, "routed.rvl")
    assert set(ir) == {"components", "ir_version", "manifest", "services"}


def test_the_component_entry_carries_only_its_provide_block():
    """The route is a component-prelude clause, so the component entry is where
    a section would most naturally land. Its body holds the provide block and
    nothing else."""
    comp = next(c for c in compile_source(ROUTED, "routed.rvl")["components"]
                if c["name"] == "Classifier")
    assert [node.get("step") for node in comp.get("body") or []] == ["provide"]


# ===========================================================================
# 2. What the artifact does carry, and what that is worth
# ===========================================================================

def test_the_declared_actions_are_derivable_from_the_ir():
    """The one fact a consumer holding a linked composition can have.

    `model_route._action_names` reads these off the AST to check that
    `route model on <action>` names an action that exists; the same names reach
    the IR through the provide block. A seam that checks an action against this
    set is checking that the action EXISTS, which is not a check that it is
    placed (section 4.1).
    """
    ir = compile_source(ROUTED, "routed.rvl")
    comp = next(c for c in ir["components"] if c["name"] == "Classifier")
    from_ir = {method["name"]
               for node in comp.get("body") or []
               if node.get("step") == "provide"
               for method in node.get("methods") or []}

    program = Parser(ROUTED, "routed.rvl").parse()
    ast_component = next(c for c in program.components
                         if c.name == "Classifier")
    assert from_ir == MR._action_names(ast_component) == {"classify", "other"}


def test_the_action_set_does_not_distinguish_the_routed_action():
    """Why the derivable fact is not a substitute for the route table.

    `classify` is routed and `other` is not. The IR's action set holds both and
    says nothing about which; the route table says exactly which. A consumer
    that treated the first as the second would report an unrouted action as
    placed, which is the fail-open direction this whole item refuses in.
    """
    program = Parser(ROUTED, "routed.rvl").parse()
    table = MR.check(program)
    assert set(table["Classifier"]) == {"classify"}

    ir = compile_source(ROUTED, "routed.rvl")
    comp = next(c for c in ir["components"] if c["name"] == "Classifier")
    from_ir = {method["name"]
               for node in comp.get("body") or []
               if node.get("step") == "provide"
               for method in node.get("methods") or []}
    assert from_ir - set(table["Classifier"]) == {"other"}


def test_the_only_producer_of_the_route_table_takes_a_parsed_program():
    """Why "by value" is the channel and not a preference.

    `check()` is the only thing that builds the table, and it takes a program.
    A consumer with no source cannot call it, so it must be handed the
    result. `shadow_promotion.ShadowPlan.route_table` is that hand-off, and it
    binds
    `check()`'s return value verbatim.
    """
    from revl import shadow_promotion

    program = Parser(ROUTED, "routed.rvl").parse()
    table = MR.check(program)
    assert table == {"Classifier": {"classify": {
        "confidential": {"role": "local", "residence": "on_device"},
        "*": {"role": "cloud", "residence": "off_device"},
    }}}
    assert "route_table" in shadow_promotion.ShadowPlan.__dataclass_fields__


# ===========================================================================
# 3. The ruleset digest covers the rule (section 4.2, on #989's rule)
# ===========================================================================

def test_the_placement_rule_modules_are_in_the_ruleset_the_digest_identifies():
    """The one-line form of the fix. Membership is what puts a module's bytes
    into the digest; absence is #989's drift on a second pair of modules."""
    assert "model_route" in attest.RULESET_MODULES
    assert "model_council" in attest.RULESET_MODULES


def _scratch_ruleset(tmp_path: Path, name: str) -> Path:
    """A byte-identical copy of the shipped ruleset that `_read_ruleset` can be
    pointed at, so the only variable in a comparison is the edit."""
    here = Path(attest.__file__).resolve().parent
    target = tmp_path / name
    target.mkdir()
    for module in attest.RULESET_MODULES:
        shutil.copyfile(here / f"{module}.py", target / f"{module}.py")
    return target


def _digest_of(monkeypatch, directory: Path):
    monkeypatch.setattr(attest, "__file__", str(directory / "attest.py"))
    monkeypatch.setattr(attest, "_ruleset_cache", None)
    return attest._read_ruleset()


def test_an_edit_to_the_ceiling_origins_moves_the_ruleset_digest(
        tmp_path, monkeypatch):
    """The exit criterion, stated as the comparison a verifier would make.

    The scratch copy reproduces the shipped digest, which is the self-check
    that stops this passing because the harness is broken. Then one line
    changes in the `model_route` copy and the digest must move with it.
    """
    shipped = attest.ruleset_digest()  # read before `__file__` is repointed
    before_dir = _scratch_ruleset(tmp_path, "before")
    after_dir = _scratch_ruleset(tmp_path, "after")

    copy = after_dir / "model_route.py"
    source = copy.read_text()
    edited = source.replace('CEILING_ORIGINS = ("confidential",)',
                            "CEILING_ORIGINS = ()")
    assert edited != source, "CEILING_ORIGINS moved; re-anchor this edit"
    copy.write_text(edited)

    before, cited_before = _digest_of(monkeypatch, before_dir)
    after, cited_after = _digest_of(monkeypatch, after_dir)

    assert before == shipped, (
        "the scratch copy must reproduce the shipped digest, or the comparison "
        "below proves nothing about the shipped ruleset"
    )
    assert before != after, (
        "an edit to `model_route.CEILING_ORIGINS` left the ruleset digest "
        "unchanged: `model_route` is not in `RULESET_MODULES` (#1311)"
    )
    assert cited_before == cited_after


def test_an_edit_to_the_council_rules_moves_the_ruleset_digest(
        tmp_path, monkeypatch):
    """The same for item 516's rule set, whose `check()` `lower.py` calls."""
    before_dir = _scratch_ruleset(tmp_path, "cbefore")
    after_dir = _scratch_ruleset(tmp_path, "cafter")
    copy = after_dir / "model_council.py"
    copy.write_text(copy.read_text() + "\n# rule change\n")

    before, _ = _digest_of(monkeypatch, before_dir)
    after, _ = _digest_of(monkeypatch, after_dir)
    assert before != after


def test_the_edit_that_moves_the_digest_is_one_that_changes_what_is_refused(
        monkeypatch):
    """Non-vacuity: `CEILING_ORIGINS` is not a constant nobody reads.

    With the shipped set, section 3.2's reproducer is refused under
    `G-MODEL-PLACE`. With `confidential` out of the set, the SAME program
    compiles. The refusal follows the set, which is why the set's bytes are a
    rule and belong in the digest.
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(CEILING, "ceiling.rvl")
    assert excinfo.value.code == "G-MODEL-PLACE"

    monkeypatch.setattr(MR, "CEILING_ORIGINS", ())
    compile_source(CEILING, "ceiling.rvl")


def test_the_set_the_digest_covers_is_the_one_the_refusal_reads():
    """The coupling, asserted rather than assumed: `taint.py` reads
    `model_route.CEILING_ORIGINS` and calls `model_route.admits`, so the module
    the digest covers is the module that decides. A refactor that copied either
    into `taint.py` would leave the digest covering the copy and not the rule,
    and this fails then."""
    taint_source = (Path(attest.__file__).resolve().parent
                    / "taint.py").read_text()
    assert "_mr.CEILING_ORIGINS" in taint_source
    assert "_mr.admits(" in taint_source
    assert "CEILING_ORIGINS = " not in taint_source

    assert MR.admits(None, "confidential", True).reason == "unrouted"
    assert MR.admits(None, "web", True).ok


def test_listing_them_adds_digest_inputs_without_adding_a_cited_code(
        monkeypatch):
    """Why this could be fixed by adding two strings.

    `discharged_guarantees` is derived from the `(Gn)` tags the listed modules
    cite. Neither file carries one: they refuse under `G-MODEL-PLACE` and
    `G-SECRET-FLOW`, which are not numbered tags, so listing them moves the
    digest and leaves the attested list alone, exactly as `retention` did. If a
    later change gives either file a tag, this says so rather than letting the
    two meanings of membership drift apart.
    """
    with_them = attest.discharged_guarantees()

    monkeypatch.setattr(
        attest, "RULESET_MODULES",
        tuple(m for m in attest.RULESET_MODULES
              if m not in ("model_route", "model_council")),
    )
    monkeypatch.setattr(attest, "_ruleset_cache", None)
    without_them = attest.discharged_guarantees()

    assert with_them == without_them

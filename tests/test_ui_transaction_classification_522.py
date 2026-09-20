"""The computer-use reversibility classification (roadmap item 522, issue
#1196, Slice 1 of docs/design/536-ui-transactions.md).

The item's exit has two halves. This file covers the half that Slice 1 lands:
A TRANSACTION MAY NOT CLAIM CLEANLINESS IT CANNOT HAVE.

Every computer-use verb carries a reversibility class, the class is
REGISTRY-OWNED (an author states what a verb is for, not that a click is
reversible), and the declaration's `compensate` slot has to agree with it:

  * `ui.click` (unknown) and `ui.download` (irreversible) may NOT declare
    `compensate`. The extern-declared `compensate` is the fact the residue and
    erase reports read to mark a crossing compensated (item 254), so admitting
    one would let a transaction over a step with no inverse print `no_residue`
    where the item's exit says it must report `uncompensated`.
  * `ui.text` (compensatable) MUST declare `compensate`. Its inverse exists
    and only the author can write it, so a missing one is residue the
    transaction could have avoided and cannot even name.
  * `screen.observe` and `ui.find` (reversible) are reads. Nothing is
    required and nothing is refused.

FAILURE DIRECTION. Both refusals fail CLOSED, and `unknown` is handled exactly
as `irreversible` for that reason: a transaction that treats an unclassified
step as reversible is the fail-open shape this repository's serious findings
share, where a claim outlives the thing meant to bound it.

NON-VACUITY, measured against this branch's base, `agent/1195-typed-computer-
use` at b8fea480. All FIVE refused programs below are parsed and ADMITTED
there, and refused here. The SIX controls are admitted on both trees, which is
what makes the refusals a measurement rather than a tautology. Running this
file there: 9 failed, 6 passed (the five refusals plus the four registry
tests, which have no `REVERSIBILITY` table to read). Running it here: 15
passed.

SCOPE, stated so the file is not read as more than it is. The check is at the
EXTERN DECLARATION only. A service method's `emission[...]` scope declares an
interface and not a crossing, and is deliberately untouched. Nothing here
implements a transaction, a phase list, or a confirmation gate; the design doc
records why the confirmation gate is not in this slice, with the measurement
behind that decision.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import ui_family  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402


def _parse(source: str):
    return Parser(source, "ui522.rvl").parse()


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        _parse(source)
    return excinfo.value


# ---------------------------------------------------------------- refusals
#
# Each of these programs parses and admits on a tree without this change.


def test_an_unknown_verb_may_not_declare_an_inverse() -> None:
    """`ui.click` actuates a control whose effect the application never
    published. A declared `compensate` is a cleanliness claim revl cannot
    honour, and it is refused rather than believed."""
    error = _refusal(
        "extern emission[ui.click] fn click(target: Str) compensate undo_click()\n"
        "  = @py { pass }\n")
    assert "is unknown" in error.message
    assert "may not declare `compensate`" in error.message
    assert "uncompensated" in (error.hint or "")


def test_an_irreversible_verb_may_not_declare_an_inverse() -> None:
    """`ui.download` lands a file on the host. Deleting it afterwards does not
    un-fetch it, and the fetch may already have been metered on the other
    side."""
    error = _refusal(
        "extern emission[ui.download] fn get(target: Str) -> Str\n"
        "  compensate delete_file() = @py { return \"\" }\n")
    assert "is irreversible" in error.message
    assert "may not declare `compensate`" in error.message


def test_a_compensatable_verb_must_declare_its_inverse() -> None:
    """`ui.text` generates key input into a named field. The inverse is
    restoring that field, which the author knows and revl does not, so the
    declaration is where it has to appear."""
    error = _refusal(
        "extern emission[ui.text] fn type_into(target: Str, s: Str)\n"
        "  = @py { pass }\n")
    assert "is compensatable" in error.message
    assert "must declare `compensate`" in error.message


def test_the_refusal_names_the_extern_and_the_declaration() -> None:
    """A refusal that does not say WHICH declaration is wrong makes an author
    grep. Both refusals quote the capability scope as written and the extern's
    own name."""
    error = _refusal(
        "extern emission[ui.download] fn fetch_invoice(t: Str) -> Str\n"
        "  compensate rm() = @py { return \"\" }\n")
    assert "`emission[ui.download]`" in error.message
    assert "`fetch_invoice`" in error.message
    assert "docs/design/536-ui-transactions.md" in (error.hint or "")


def test_the_refusal_classifies_as_a_g4_reversibility_finding() -> None:
    """G4 is `every mutation carries an inverse, or admits irreversibility`.
    A UI step that declares an inverse it does not have is that guarantee's
    own failure, so it carries that guarantee's code rather than a new one."""
    record = classify(_refusal(
        "extern emission[ui.click] fn c(t: Str) compensate u() = @py { pass }\n"))
    assert record["code"] == "G4"
    assert record["category"] == "reversibility"
    assert record["guarantee"] == (
        "every mutation carries an inverse, or admits irreversibility with `emit`")


# ---------------------------------------------------------------- controls
#
# These pass on a tree WITHOUT this change too. They are what makes the
# refusals above a measurement: the check is narrow, and a broad one that
# refused every UI declaration would fail here.


def test_a_compensatable_verb_with_its_inverse_is_admitted() -> None:
    _parse("extern emission[ui.text] fn type_into(target: Str, s: Str)\n"
           "  compensate restore() = @py { pass }\n")


def test_a_verb_with_no_inverse_is_admitted_when_it_claims_none() -> None:
    _parse("extern emission[ui.click] fn click(t: Str) -> Int = @py { return 0 }\n")
    _parse("extern emission[ui.download] fn get(t: Str) -> Str = @py { return \"\" }\n")


def test_a_reversible_verb_is_required_to_do_nothing() -> None:
    """A read changes no state the target owns, so the transaction needs
    nothing from it. Neither presence nor absence of a compensation is
    refused; over-reach here would make the classification noise."""
    _parse("extern emission[screen.observe] fn o(r: Str) -> Str = @py { return \"\" }\n")
    _parse("extern emission[ui.find] fn f(s: Str, n: Str) -> Str = @py { return \"\" }\n")


def test_an_ordinary_capability_is_untouched() -> None:
    """The check keys on the computer-use registry, not on the word
    `compensate`. An ordinary emission keeps both spellings."""
    _parse("extern emission[db.write] fn w(row: Str) compensate undo_w() = @py { pass }\n")
    _parse("extern emission[db.write] fn w2(row: Str) = @py { pass }\n")


def test_a_service_method_scope_is_not_a_declaration_site() -> None:
    """A service method declares an interface, not a crossing, and has no
    `compensate` slot to agree with. The obligation belongs to the extern that
    actually crosses; this test exists so a later widening is a deliberate
    decision rather than an accident."""
    _parse("service S { emission[ui.text] fn go(t: Str, s: Str) }\n")
    _parse("service S2 { emission[ui.click] fn go(t: Str) }\n")


def test_a_pure_extern_carries_no_capability_scope_to_classify() -> None:
    _parse("extern pure fn helper(x: Str) -> Str = @py { return x }\n")


# ------------------------------------------------------- the registry itself


def test_every_admissible_verb_has_a_reversibility_class() -> None:
    """A verb with no class would default to `not checked`, which is the
    fail-open default this item exists to remove. Exact equality, not
    membership: a verb added to `ui_family.VERBS` without a class fires
    here."""
    assert sorted(ui_family.REVERSIBILITY) == ui_family.spellings()
    for token, cls in ui_family.REVERSIBILITY.items():
        assert cls in ui_family.CLASSES, token


def test_every_class_states_what_the_system_does_with_it() -> None:
    """The classification is only useful if each class names an OBLIGATION. A
    class with no stated rule is a label."""
    assert sorted(ui_family.OBLIGATION) == sorted(ui_family.CLASSES)
    for cls, rule in ui_family.OBLIGATION.items():
        assert rule.strip(), cls
    assert ui_family.NO_INVERSE == {
        ui_family.IRREVERSIBLE, ui_family.UNKNOWN}


def test_confirm_required_is_a_raised_class_and_slice_1_does_not_raise() -> None:
    """No verb is BORN `confirm-required`: it is the class a token is RAISED
    to by the operator's existing approval authority, and Slice 1 does not
    implement the raise (design doc section 6). This test is the tripwire for
    that: a later slice that assigns the class without landing the raise fires
    here rather than shipping a class nothing acts on."""
    assert ui_family.CONFIRM_REQUIRED in ui_family.CLASSES
    assert ui_family.CONFIRM_REQUIRED not in set(ui_family.REVERSIBILITY.values())


def test_a_capability_parameter_cannot_change_the_class() -> None:
    """Item 294 narrows a token with a valuation. A valuation that changed the
    reversibility class would be the author-side opt-out the registry exists
    to prevent, so the lookup strips parameters."""
    assert ui_family.reversibility("ui.click") == ui_family.UNKNOWN
    assert ui_family.reversibility('ui.click(host="a")') == ui_family.UNKNOWN
    assert ui_family.reversibility("db.write") is None
    assert ui_family.reversibility("ui.drag") is None
    assert ui_family.teardown_refusal("db.write", "emission", "w", True) is None

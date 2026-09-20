"""The reserved computer-use capability namespace (roadmap item 521, issue
#1195, Slice 1 of docs/design/532-typed-computer-use.md).

The item's exit has two halves. This file covers the first one and the
enumeration half of the second:

1. AN UNSCOPED VERB IS REFUSED. `emission[ui]` names the whole GUI surface.
   revl's headline guarantee is that the irreversible reach of a compiled
   program is ENUMERATED (G8), and a boundary that means "whatever the GUI
   permits" is not enumerable whatever it is labelled, so it is refused at
   admission rather than admitted and annotated `*`. The two siblings of that
   refusal are here too, and both fail CLOSED: an undeclared verb
   (`ui.drag`) and a fallback-ladder rung (`ui.click.pixel`), neither of
   which Slice 1 admits.

2. `revl audit` ENUMERATES EVERY UI EMISSION CLASS A PROGRAM CAN REACH. A UI
   verb is an ordinary capability-scoped emission, so the existing G8 audit
   reach prints it with no new reporting code. The audit test here is the
   evidence for that claim rather than an assertion of it.

NON-VACUITY. Every refusal test below fails on a tree without
`src/revl/ui_family.py`: `emission[ui]`, `emission[ui.drag]` and
`emission[ui.click.pixel]` all parse and admit there. The controls
(`test_declared_ui_verbs_are_admitted`, `test_ordinary_capability_untouched`,
`test_secret_binding_is_a_selector_not_a_declaration`) pass on BOTH trees,
which is what makes the failing ones a measurement rather than a tautology.

SCOPE, stated so the file is not read as more than it is. The check is at the
DECLARATION site only: an extern classification scope and a service method's
emission scope. A `secret K for C` binding and a `capability <glob>` policy
rule are SELECTORS over a token, not a statement of reach, and they are
deliberately untouched. Nothing here claims anything about what a host adapter
does at execution time; that is the computer-use substrate's (roadmap item
539, upstream inso1337/revl-harness#11).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source, ui_family  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.policy import component_reach, evaluate, parse_policy  # noqa: E402


def _parse(source: str):
    return Parser(source, "ui521.rvl").parse()


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        _parse(source)
    return excinfo.value


# --------------------------------------------------------------- the refusals


@pytest.mark.parametrize("root", ["ui", "screen"])
def test_unscoped_root_is_refused_by_name(root: str) -> None:
    """`emission[ui]` / `emission[screen]`: the act-on-anything verb."""
    error = _refusal(
        f"extern emission[{root}] fn act(target: Str) = @py {{ pass }}\n")
    assert f"`emission[{root}]` names the whole GUI surface" in error.message
    assert "not an enumerable boundary" in error.message
    # the refusal cites the guarantee it defends, and the argument behind it
    assert error.code == "G8"
    assert error.category == "boundary"
    assert "G8" in (error.hint or "")
    assert "roadmap item 521" in (error.hint or "")
    assert ui_family.DESIGN in (error.hint or "")
    # and it enumerates the nearest allowed space rather than describing it
    for spelling in ui_family.spellings():
        assert f"`{spelling}`" in (error.hint or "")


def test_the_refusal_quotes_the_classification_the_author_wrote() -> None:
    """A witnessed UI mutation is refused with `witnessed[...]` in the message,
    not a generic `emission[...]`, so the author reads back their own line."""
    error = _refusal(
        "extern witnessed[ui] fn act(target: Str) -> Str = @py { return \"\" }\n")
    assert "`witnessed[ui]` names the whole GUI surface" in error.message


def test_an_undeclared_verb_is_refused() -> None:
    """The verb set is CLOSED. An invented token narrows nothing and escapes
    every policy rule written against the real one, so it fails closed."""
    error = _refusal(
        "extern emission[ui.drag] fn drag(target: Str) = @py { pass }\n")
    assert error.message == "`ui.drag` is not a declared computer-use verb"
    assert error.code == "G8"
    assert "CLOSED" in (error.hint or "")


def test_a_fallback_ladder_rung_is_refused_until_its_check_exists() -> None:
    """Slice 1 admits only the ladder's top rung, a semantic target. A
    selector or pixel rung is refused rather than admitted ahead of the check
    that bounds it: admitting the spelling first is the fail-open shape."""
    error = _refusal(
        "extern emission[ui.click.pixel] fn click(target: Str) = @py { pass }\n")
    assert "descends the computer-use fallback ladder" in error.message
    assert "not admissible yet" in error.message
    assert error.code == "G8"


def test_a_service_method_scope_is_checked_too() -> None:
    """A service declaration is an upper bound on its providers' effects, so
    an unscoped UI bound there would be an unscoped UI bound everywhere."""
    error = _refusal("service Ui { emission[ui] fn act(target: Str) }\n")
    assert "names the whole GUI surface" in error.message
    assert error.code == "G8"


def test_the_refusal_classifies_as_a_g8_boundary_finding() -> None:
    """`revl explain`/`--json` must see the guarantee, not just the text."""
    record = classify(_refusal("extern emission[ui] fn a() = @py { pass }\n"))
    assert record["code"] == "G8"
    assert record["category"] == "boundary"
    assert record["guarantee"] == "the boundary surface is enumerable"


# ------------------------------------------------------------------- controls
#
# These pass on a tree WITHOUT this change too. They are what makes the
# refusals above a measurement: the check is narrow, and a broad one that
# refused everything would fail here.


@pytest.mark.parametrize("token", sorted(ui_family.spellings()))
def test_declared_ui_verbs_are_admitted(token: str) -> None:
    # A `compensatable` verb (item 522) must carry its inverse at the
    # declaration, so the fixture spells one. Both shapes parse on a tree
    # without item 522's check as well, which keeps this a control.
    inverse = ("compensate undo_v()"
               if ui_family.reversibility(token) == ui_family.COMPENSATABLE
               else "")
    _parse(f"extern emission[{token}] fn v(target: Str) {inverse}"
           f" = @py {{ pass }}\n")


def test_ordinary_capability_untouched() -> None:
    _parse("extern emission[db.write] fn w(row: Str) = @py { pass }\n")
    _parse("service S { emission[db.write, bus] fn go(row: Str) }\n")


def test_secret_binding_is_a_selector_not_a_declaration() -> None:
    """`secret K for ui` binds a secret to a token; it does not declare that
    anything reaches the GUI. The declaration-site check does not run here,
    and this test exists so a later widening of the check is a deliberate
    decision rather than an accident."""
    _parse("secret Token for ui\n")
    _parse("secret Token for ui.click\n")


# ------------------------------------------------------- the registry itself


def test_the_verb_registry_matches_the_item() -> None:
    """The five emission classes item 521 names. `ui.text` is what the issue's
    sketch called `ui.type`: `type` is a reserved keyword, so `ui.type` cannot
    be spelled in a dotted capability token at the declaration site OR in a
    `secret K for C` binding, and a verb that can be declared but not selected
    by the surfaces that bound it is worse than a verb with another name."""
    assert ui_family.spellings() == [
        "screen.observe", "ui.click", "ui.download", "ui.find", "ui.text",
    ]
    with pytest.raises(RevlError):
        _parse("extern emission[ui.type] fn t(target: Str) = @py { pass }\n")


def test_every_verb_names_an_emission_class() -> None:
    """A verb with no stated class is a verb list entry, which is the thing
    the item says a design without the binding degenerates into."""
    for root, verbs in ui_family.VERBS.items():
        assert root in ui_family.ROOTS
        for verb, emission_class in verbs.items():
            assert emission_class.strip()


# --------------------------------------- the enumeration half of the exit
#
# A CUA worker: observe the screen, resolve a semantic target from what was
# observed, actuate it. `ui_download` is DECLARED but never called, so it is
# on the extern table and NOT on the reach: the audit reports what a component
# can reach, not what the file mentions.

# The target record is item 521 slice 4's: a computer-use program that
# declares `ui.find` or an actuation verb must carry it, so the fixture below
# is the post-slice-4 spelling of the same program. Nothing in THIS file
# measures the record - it is here because the program would not compile
# without it, which is exactly what slice 4 set out to make true.
CUA_WORKER = """
type UiTarget = {
  application: Str
  window: Str
  role: Str
  name: Str
  evidence: Str
  action: Str
  session: Str
  bounds: Str
  expiry: Int
  confirm: Bool
}

extern emission[screen.observe] fn screen_observe(region: Str) -> Str
  = @py { return "" }
extern emission[ui.find] fn ui_find(seen: Str, name: Str) -> UiTarget
  = @py { return None }
extern emission[ui.click] fn ui_click(target: UiTarget) -> Int = @py { return 0 }
extern emission[ui.download] fn ui_download(target: UiTarget) -> Str
  = @py { return "" }

service Worker { emission fn approve(region: Str) -> Int }

component Billing provides worker: Worker {
  provide worker {
    fn approve(region) {
      let seen = emit screen_observe(region)
      let target = emit ui_find(seen, "Approve")
      return emit ui_click(target)
    }
  }
}
"""


@pytest.fixture(scope="module")
def cua_audit():
    return audit_report(compile_source(CUA_WORKER, "cua_worker.rvl"))


def test_audit_enumerates_every_reached_ui_emission_class(cua_audit) -> None:
    """The item's exit, first half: `revl audit` over a CUA program enumerates
    every UI emission class it can reach. Exact equality, not membership: a
    missing token is reach an operator cannot see, and an extra one is a
    policy rule aimed at a crossing that does not happen."""
    assert {r.token for r in component_reach(cua_audit, "Billing")} == {
        "screen.observe", "ui.find", "ui.click",
    }


def test_a_declared_but_unreached_verb_is_not_reach(cua_audit) -> None:
    tokens = {r.token for r in component_reach(cua_audit, "Billing")}
    assert "ui.download" not in tokens


def test_a_policy_rule_selects_a_ui_verb_by_its_declared_token(cua_audit) -> None:
    """Because a UI verb is an ordinary capability token, the existing
    `capability <glob>` machinery bounds it with no new policy surface: an
    allow-list that omits `ui.click` refuses, and one that names it admits."""
    assert evaluate(
        parse_policy("component Billing may reach screen.observe, ui.find, ui.click"),
        cua_audit) == []
    assert evaluate(
        parse_policy("component Billing may reach screen.observe, ui.find"),
        cua_audit) != []


def test_only_the_reserved_roots_are_checked() -> None:
    assert ui_family.is_reserved("ui")
    assert ui_family.is_reserved("screen.observe")
    assert not ui_family.is_reserved("db.write")
    assert not ui_family.is_reserved("uix.click")
    assert ui_family.refusal("db.write", "emission") is None

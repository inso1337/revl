"""The computer-use taint classes (roadmap item 521, issue #1195, Slice 2).

Slice 1 reserved the namespace and made an unscoped UI verb a G8 refusal.
This is the slice that makes the item's own premise enforceable rather than
stated: *every UI target is untrusted input, never a trusted reference*.

WHAT WAS WRONG, MEASURED ON `origin/main` AT `dfecba2a` BEFORE THIS LANDED.
`revl.taint._SOURCE_CLASS_SCOPES` was `{web, net, fs, model, input}` and
`_SINK_CLASS_SCOPES` was `{shell, exec, terminal, policy}`. Neither reserved
root was in either set, so with `taint_strict` on:

  * a `screen.observe` return flowing into a `shell` sink ADMITTED, and the
    same program with the author's `-> Untrusted[Str]` written out was refused
    under G9. Remove the qualifier, change nothing else, and it compiled. The
    containment was therefore opt-in, which is the one thing item 249's derived
    classes exist to prevent: sink-ness and source-ness come from the side that
    grants the authority, never from the author, because a classification an
    author can lower is a classification a careless author lowers;
  * a UI actuation admitted an untrusted argument from ANY origin - `web` into
    `ui.click` compiled too. `ui` was not a sink at all, so even an explicitly
    `Untrusted[Str]` value reaching a click was admitted.

THE FAILURE DIRECTION OF THE REPAIR. This widens what is refused, and every
program it newly refuses was previously admitted, so the direction is stated
here rather than implied: an author who relied on the old behaviour now sees a
G9 refusal naming the origin (`screen`) and the position (`a UI actuation`),
and the repair is the one item 249 already ships - a declared `endorse[screen]`
with a reason, or a `verified fn` returning `Trusted[T]`. There is no qualifier
that turns it back off, which is the point.

NON-VACUITY, measured on `dfecba2a` with this file's `_OLD` programs. Four
programs admitted before this slice and are refused by it; one control admits
on both trees; one program was already refused and still is, with a changed
origin label. The numbers are in `test_non_vacuity_the_four_programs_that_flipped`.

SCOPE. Nothing here admits a rung token: `ui.click.pixel` is still refused at
the declaration site by slice 1, and slice 3 owns the prefix-closure check that
replaces that refusal. What this file does pin is that the taint roles resolve
by longest PREFIX, so the rung tokens slice 3 admits cannot arrive carrying a
weaker role than the verb they descend from.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import ui_family  # noqa: E402
from revl.admit_profile import AdmissionProfile  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.policy import TAINT_FOLD_ORIGINS, parse_policy  # noqa: E402
from revl.taint import (  # noqa: E402
    _SINK_CLASS_SCOPES, _SOURCE_CLASS_SCOPES, ORIGIN_CLASSES,
)


def _strict() -> AdmissionProfile:
    """The profile that turns the item-249 derived sinks and sources on, and
    nothing else. `AdmissionProfile.untrusted_author` always sets it."""
    return AdmissionProfile(taint_strict=True)


# The family, declared once. `ui.text` carries its item-522 `compensate`
# because a compensatable verb without one is a G4 refusal and would mask the
# G9 answer this file is measuring.
# Slice 4's target record. Every signature below carries it because slice 4
# refuses the bare-string spelling outright, so this file measures the taint
# classes on the program the language now admits rather than on the one it
# admitted when slice 2 landed. The taint question is unchanged by that: the
# ROLE is read off the declared capability token, never off the argument type,
# which is why swapping `Str` for `UiTarget` moves no verdict in this file.
TARGET_RECORD = (
    'type UiTarget = {\n'
    '  application: Str\n'
    '  window: Str\n'
    '  role: Str\n'
    '  name: Str\n'
    '  evidence: Str\n'
    '  action: Str\n'
    '  session: Str\n'
    '  bounds: Str\n'
    '  expiry: Int\n'
    '  confirm: Bool\n'
    '}\n'
)

FAMILY = (
    TARGET_RECORD
    + 'extern emission[screen.observe] fn screen_observe(region: Str) -> Str\n'
    '  = @py { return "" }\n'
    'extern emission[ui.find] fn ui_find(seen: Str, name: Str) -> UiTarget\n'
    '  = @py { return None }\n'
    'extern emission[ui.click] fn ui_click(target: UiTarget) -> Int\n'
    '  = @py { return 0 }\n'
    'extern emission[ui.text] fn ui_text(target: UiTarget, s: Str)\n'
    '  compensate restore_field() = @py { return }\n'
    'extern emission[ui.download] fn ui_download(target: UiTarget) -> Str\n'
    '  = @py { return "" }\n'
    'fn restore_field() { }\n'
)


# A target minted OUTSIDE the screen, for the one measurement that needs its
# first argument clean. `ui.text`'s derivation is all-arguments (a capability
# token carries no parameter roles), so a target that came from `ui.find`
# would taint argument 1 too and the refusal would name it instead of the
# value - measuring the target, which two other tests already measure, rather
# than the key input, which only this one does.
_PURE_TARGET = (
    'extern pure fn a_target(field: Str) -> UiTarget = @py { return None }\n'
)


def _component(body: str, returns: str = "Int", sig: str = "Int") -> str:
    return (
        FAMILY
        + f"service Worker {{ emission fn act(region: Str) -> {sig} }}\n"
        "component Billing provides worker: Worker {\n"
        "  provide worker {\n"
        f"    fn act(region) {{\n{body}    }}\n"
        "  }\n"
        "}\n"
    )


def _refusal(src: str, profile=None) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "ui_taint_521.rvl", profile=profile)
    return excinfo.value


# --------------------------------------------------- the two class additions

def test_screen_is_a_source_class() -> None:
    assert "screen" in _SOURCE_CLASS_SCOPES


def test_ui_is_a_sink_class() -> None:
    assert "ui" in _SINK_CLASS_SCOPES


def test_screen_is_an_origin_class() -> None:
    """`_origin_of` resolves an origin by the token's dotted head, so a class
    that is a source scope but not an origin class would mint the literal token
    `screen.observe` - an origin no source-class test, no `route model` arm and
    no `<origin>-taint` policy rule matches."""
    assert "screen" in ORIGIN_CLASSES


def test_screen_is_an_admit_able_taint_fold_origin() -> None:
    """`approval.static_taint` INTERSECTS a component's recorded taint with
    `TAINT_FOLD_ORIGINS`, so an origin missing from that set is an origin
    silently dropped from the taint an auto-approve decision is made against: a
    screen-tainted crossing would have compared as clean. Adding it is
    fail-closed in every direction - the crossing now prompts unless an
    operator writes the rule, and the unknown-taint floor is a larger set."""
    assert "screen" in TAINT_FOLD_ORIGINS
    rule = parse_policy(
        "component a may auto-approve kv.get admitting screen-taint"
    ).auto_approve_rules[0]
    assert rule.admitting == frozenset({"screen"})


# ------------------------------------------------- the headline G9 refusals

SCREEN_TO_CLICK = _component(
    "      let seen = emit screen_observe(region)\n"
    '      let target = emit ui_find(seen, "Approve")\n'
    "      return emit ui_click(target)\n")


def test_an_observed_target_reaching_a_click_is_refused() -> None:
    """The item's premise, enforced: a target discovered by looking at pixels
    is untrusted input, so the actuation refuses it. NO qualifier appears
    anywhere in the program - the whole point is that the author did not have
    to remember."""
    assert "Untrusted" not in SCREEN_TO_CLICK
    error = _refusal(SCREEN_TO_CLICK, _strict())
    assert classify(error)["code"] == "G9"
    assert "screen" in error.message
    assert "a UI actuation" in error.message
    assert "ui_click" in error.message


def test_the_refusal_survives_the_author_removing_the_qualifier() -> None:
    """The measured gap, in one assertion: the program with the author's
    explicit `Untrusted[Str]` and the program without it now reach the SAME
    verdict. Before this slice the second one compiled."""
    with_qualifier = SCREEN_TO_CLICK.replace(
        "fn screen_observe(region: Str) -> Str",
        "fn screen_observe(region: Str) -> Untrusted[Str]")
    assert with_qualifier != SCREEN_TO_CLICK
    for src in (SCREEN_TO_CLICK, with_qualifier):
        assert classify(_refusal(src, _strict()))["code"] == "G9"


def test_a_screen_read_reaching_a_shell_sink_is_refused_unqualified() -> None:
    """The other half of the same gap, and the one PR #1275 measured: the sink
    is `shell`, which was ALREADY a derived sink class. What was missing was
    the source side, so the flow was clean at its head."""
    src = (
        'extern emission[screen.observe] fn screen_observe(region: Str) -> Str\n'
        '  = @py { return "" }\n'
        'extern emission[shell] fn run(cmd: Str) = @py { return }\n'
        "service Ops { emission fn go(region: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn go(region) { let seen = emit screen_observe(region)"
        "  emit run(seen) } }\n"
        "}\n"
    )
    error = _refusal(src, _strict())
    assert classify(error)["code"] == "G9"
    assert "a shell command" in error.message
    assert "screen" in error.message


def test_an_untrusted_value_of_any_origin_is_refused_at_a_click() -> None:
    """`ui` is a sink class, not a screen-specific one. A fetched value reaching
    an actuation is the same refusal: a UI actuation is a position where the
    value IS the authority, in the same sense as a shell string.

    The fetched value goes STRAIGHT to the click, with no `ui.find` between.
    That is not incidental: `ui.find` is itself a source, so routing a web value
    through it would have relabelled the origin `screen` and this test would
    have measured the wrong thing."""
    src = _component(
        "      let page = emit fetch(region)\n"
        "      return emit ui_click(page)\n").replace(
        "fn restore_field() { }\n",
        "fn restore_field() { }\n"
        'extern emission[web] fn fetch(url: Str) -> UiTarget\n'
        '  = @py { return None }\n')
    error = _refusal(src, _strict())
    assert classify(error)["code"] == "G9"
    assert "web" in error.message
    assert "a UI actuation" in error.message


# ----------------------------------------- which arguments are sinks, and why

def test_the_value_typed_into_a_field_is_a_sink() -> None:
    """`ui.text` generates KEY INPUT, which is not inert text: a newline
    submits, a tab moves focus, a shortcut is a command. Observed content
    reaching it chooses what happens and not only what is written.

    ARGUMENT 2, the value, not the target - and that is the honest limit rather
    than a preference. A capability token carries no parameter roles (item 294's
    parameters narrow the capability, they do not name the parameters), so revl
    cannot tell `ui.text`'s target from its value and the derivation is
    all-arguments, exactly as `shell` already is. Per-parameter precision
    remains available only through an explicit `Trusted[T]` annotation, which is
    author-side and therefore never the derivation."""
    src = _component(
        "      let seen = emit screen_observe(region)\n"
        '      let field = a_target("amount")\n'
        "      emit ui_text(field, seen)\n"
        "      return 0\n").replace(
        "fn restore_field() { }\n",
        "fn restore_field() { }\n" + _PURE_TARGET)
    error = _refusal(src, _strict())
    assert classify(error)["code"] == "G9"
    assert "argument 2" in error.message
    assert "ui_text" in error.message


def test_the_target_of_a_download_is_a_sink() -> None:
    """`ui.download` names what gets fetched onto the host. An untrusted name
    there is the file-on-the-host decision made by the screen."""
    src = _component(
        "      let seen = emit screen_observe(region)\n"
        '      let t = emit ui_find(seen, "Receipt")\n'
        "      return emit ui_download(t)\n", sig="Str")
    error = _refusal(src, _strict())
    assert classify(error)["code"] == "G9"
    assert "ui_download" in error.message


def test_find_is_a_source_and_not_a_sink() -> None:
    """The one verb the head rule gets wrong, and the reason the roles are
    registry-owned. `ui.find` consumes observed content and produces a claim
    (design §5, "it sits on both sides"). If its arguments were sinks, the
    family's canonical observe-find-click program could not be written without
    endorsing the observation BEFORE anything had looked at it, which is the
    opposite of what the item wants: a target stays `Untrusted` all the way to
    the actuation, and the endorsement belongs at the actuation, where an
    operator can see what is being claimed about it."""
    src = _component(
        "      let seen = emit screen_observe(region)\n"
        '      return emit ui_find(seen, "Approve")\n', sig="UiTarget")
    compile_source(src, "find_only.rvl", profile=_strict())  # must not raise


def test_a_resolved_target_is_itself_untrusted() -> None:
    """`ui.find` being a source is what makes the refusal above hold: without
    it, a `ui_find` that ignored its arguments would launder the observation
    and hand a clean target to the click. The origin it mints is `screen`, not
    a second name for the same provenance."""
    assert ui_family.source_origin("ui.find") == "screen"
    assert ui_family.source_origin("screen.observe") == "screen"
    assert ui_family.source_origin("ui.click") is None


# ----------------------------------------------------- the declassifier works

def test_a_declared_endorsement_admits_the_same_program() -> None:
    """The refusal is repairable, and the repair is the one item 249 already
    ships: a declared `endorse[screen]` slot with a mandatory reason, which is
    on the audit surface and forbiddable by policy. An endorsement revl cannot
    check is an endorsement it must not silently perform, so the author states
    it at the actuation."""
    src = (
        FAMILY
        + "service Worker { emission endorse[screen] fn act(region: Str) -> Int }\n"
        "component Billing provides worker: Worker {\n"
        "  provide worker {\n"
        "    fn act(region) {\n"
        "      let seen = emit screen_observe(region)\n"
        '      let target = emit ui_find(seen, "Approve")\n'
        '      let ok = endorse[screen](target, reason = "operator reviewed it")\n'
        "      return emit ui_click(ok)\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    compile_source(src, "endorsed.rvl", profile=_strict())  # must not raise


# ------------------------------------------------------ the controls, and byte
# -------------------------------------------------------- identity without the
# -------------------------------------------------------------- strict profile

def test_a_non_ui_sink_over_the_same_shape_is_unaffected() -> None:
    """The design's named control: a `db.write` crossing over the same shape
    must be untouched, or the measurement would only say that strict mode
    refuses things."""
    src = _component(
        "      let seen = emit screen_observe(region)\n"
        '      return emit db_write(seen)\n', sig="Int").replace(
        "fn restore_field() { }\n",
        "fn restore_field() { }\n"
        "extern emission[db.write] fn db_write(row: Str) -> Int"
        " = @py { return 0 }\n")
    compile_source(src, "control.rvl", profile=_strict())  # must not raise


def test_the_landed_audit_program_still_compiles_without_strict_mode() -> None:
    """The derived classes are profile-gated, exactly as every other Slice D
    class is, so a program compiled without `taint_strict` is unchanged. This
    is the program `tests/test_ui_capability_namespace_521.py` audits; if this
    file made it refuse, the item's enumeration exit would have been traded for
    its taint exit."""
    compile_source(SCREEN_TO_CLICK, "cua_worker.rvl")  # must not raise


# ------------------------------------------------ the registry is exhaustive

def test_every_admissible_spelling_carries_exactly_one_taint_role() -> None:
    """An unclassified verb would fall back to the head rule, which is the
    fail-open default this registry exists to remove. Exhaustiveness is a test
    and not a convention."""
    assert set(ui_family.TAINT_ROLES) == set(ui_family.spellings())
    for token, roles in ui_family.TAINT_ROLES.items():
        assert len(roles) == 1, token
        assert roles <= {ui_family.SOURCE, ui_family.SINK}, token


def test_a_rung_token_inherits_its_verbs_role_by_prefix() -> None:
    """Forward-safety for slice 3. A rung is a strictly WEAKER way to name the
    same target - a selector may match a different control that satisfies it, a
    pixel always hits something - so a rung must never carry a weaker taint role
    than the verb it descends from. Resolution is by longest registered prefix,
    so admitting the spelling in `refusal()` cannot silently drop the role.

    These tokens are NOT admissible today: slice 1 refuses them at the
    declaration site and slice 3 owns the check that replaces that refusal.
    What is pinned here is which way they fall if they are ever admitted."""
    assert ui_family.is_taint_sink("ui.click.selector")
    assert ui_family.is_taint_sink("ui.click.pixel")
    assert ui_family.is_taint_sink("ui.text.pixel")
    assert not ui_family.is_taint_sink("ui.find.selector")
    assert ui_family.source_origin("ui.find.selector") == "screen"
    # still refused at the declaration site, by slice 1's own message
    assert "not admissible yet" in ui_family.refusal(
        "ui.click.pixel", "emission")[0]


def test_an_unresolvable_ui_token_falls_to_the_sink_side() -> None:
    """Not a live path - the verb set is closed - but the default has to be the
    side that refuses, because it decides where an unrecognised spelling lands
    if one ever reaches the derivation through an ambient manifest."""
    assert ui_family.is_taint_sink("ui.drag")
    assert not ui_family.is_taint_sink("db.write")
    assert not ui_family.is_taint_sink("shell")


def test_a_parameterised_token_keeps_its_role() -> None:
    """item 294: a narrowed `ui.click(app="Billing")` is the same operation. A
    valuation that could change a taint role would be an author-side opt-out,
    which is the shape the derived classes exist to remove."""
    assert ui_family.is_taint_sink('ui.click(app="Billing")')
    assert ui_family.source_origin('screen.observe(app="Billing")') == "screen"


# -------------------------------------------------------------- non-vacuity

_OLD_ADMITTED = "the four programs that compiled on dfecba2a"


def test_non_vacuity_the_four_programs_that_flipped() -> None:
    """Measured, not asserted in prose. On `origin/main` at `dfecba2a` these
    four compiled under `taint_strict`; here each is a G9 refusal. The fifth
    entry is the control, which compiles on both trees - without it the
    measurement would only say that strict mode refuses things."""
    flipped = [
        SCREEN_TO_CLICK,
        SCREEN_TO_CLICK.replace(
            "fn screen_observe(region: Str) -> Str",
            "fn screen_observe(region: Str) -> Untrusted[Str]"),
        _component(
            "      let page = emit fetch(region)\n"
            "      return emit ui_click(page)\n").replace(
            "fn restore_field() { }\n",
            "fn restore_field() { }\n"
            'extern emission[web] fn fetch(url: Str) -> UiTarget\n'
            '  = @py { return None }\n'),
        _component(
            "      let seen = emit screen_observe(region)\n"
            '      let field = a_target("amount")\n'
            "      emit ui_text(field, seen)\n"
            "      return 0\n").replace(
            "fn restore_field() { }\n",
            "fn restore_field() { }\n" + _PURE_TARGET),
    ]
    assert len(flipped) == 4
    for src in flipped:
        assert classify(_refusal(src, _strict()))["code"] == "G9"


# ------------------------------------------------------------ the cross-PR fact
#
# Item 525's demo (PR #1275) asserts, under the label `MEASURED GAP`, that a
# screen read with the author's qualifier REMOVED still admits. This slice is
# what makes that false. The demo is not on this branch, so the assertion below
# skips where it does not apply and names the reason where it does, rather than
# letting a landed demo go on asserting a gap that has been closed.

_DEMO_DIR = ROOT / "demo" / "legacy_enterprise"


@pytest.mark.skipif(not _DEMO_DIR.is_dir(),
                    reason="item 525's demo (issue #1200, PR #1275) is not on "
                           "this tree; the assertion applies once it lands")
def test_the_flagship_demo_no_longer_asserts_the_measured_gap() -> None:
    stale = [p.name for p in sorted(_DEMO_DIR.glob("*.py"))
             if "MEASURED GAP" in p.read_text()]
    assert not stale, (
        f"{stale}: item 525's demo still labels the screen-taint gap MEASURED, "
        "but item 521 slice 2 closed it - a screen read with no qualifier now "
        "refuses under G9 at a shell sink and at a UI actuation. The demo step "
        "must assert the refusal for BOTH the qualified and the unqualified "
        "program (see test_the_refusal_survives_the_author_removing_the_"
        "qualifier above) instead of asserting an admission.")

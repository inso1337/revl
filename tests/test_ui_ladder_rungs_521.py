"""The computer-use fallback ladder (roadmap item 521, issue #1195, Slice 3).

This is the item's own OPEN DECISION, in the issue's words: the ladder is a
typed action, then a DOM or accessibility selector, then a pixel-level action,
and the question is how far down is admissible at run time under a declared
policy, "since an unbounded ladder is the same as no guarantee".

Design 532 §4.2 settles it by making a RUNG PART OF THE CAPABILITY TOKEN:

    ui.click              rung 0, a semantic target
    ui.click.selector     rung 1
    ui.click.pixel        rung 2

Descending is crossing a DIFFERENT declared boundary, not retrying the same
one, which is what turns "the ladder descended" from an adapter's internal
state into a fact on the audit surface.

WHY THE DEPTH HAS TO BE BOUNDED, in §4.1's terms, because "typed APIs are
nicer" is not a reason. Rung 0 fails CLOSED by nature: an identity the
application publishes either resolves or errors. Rungs 1 and 2 fail OPEN by
nature: a selector matches a different control that satisfies it, and a pixel
always hits something. Both lower rungs therefore fail by the ACTION
SUCCEEDING ON THE WRONG THING, which is why the rung set is closed and why a
rung is spellable only on a verb that acts on a target.

THE TWO BLOCKERS SLICES 1 AND 2 RECORDED, and how they were cleared.

  1. "Slice 1's hook is `parser._capability_list`, which sees ONE token with
     no component context, while prefix-closure is a per-component property
     over a SET of tokens: it needs a refusal site in the reach/boundary layer
     that does not exist yet."

     Cleared by running the check over the ASSEMBLED IR at the end of
     `lower._check_and_lower`, through `policy.component_reach` - the same
     reach `revl audit` prints and every `capability <glob>` rule selects
     over. Not a second reach definition: a component that declares
     `ui.click` on an extern it never calls is not reaching a semantic target,
     and `test_a_declared_but_unreached_semantic_verb_does_not_close_a_rung`
     measures exactly that.

  2. "§9's obligation fires here: `selfhost/lower.rvl` must port the check or
     decline the program by a named marker, which carries a crate
     regeneration."

     Cleared by a third FRONTIER TABLE axis in `tools/build_gate_crate.py`,
     derived from `ui_family.ROOTS`, so the native gate answers
     `outside_frontier` with a reason on any source carrying a reserved
     capability namespace. Both builders were regenerated and
     `tests/test_gate_crate_admit.py` drives it from the consumer side. The
     registry half is pinned here by
     `test_the_reserved_roots_are_a_declared_frontier_gap`; the wire half
     belongs to that file, which is the one that shells cargo.

     What this discharges and what it does not: the gate now DECLINES BY NAME
     instead of agreeing by silence, which is what §9 asks for. It is not a
     port. `selfhost/lower.rvl` still runs none of item 521's checks, and
     `test_the_selfhost_gate_does_not_decide_a_ui_program` in
     `tests/test_ui_target_binding_521.py` keeps measuring that.

NON-VACUITY. On `4cfc8f32` every rung token was refused at the declaration
site, so no program in this file could be written at all: the four admitted
ladders here do not compile there and the four refusals there are the wrong
refusal (slice 1's blanket "not admissible yet", not a prefix-closure
verdict). `test_non_vacuity_the_ladder_was_not_writable` states it as the set.

WHAT IS NOT CLAIMED, and 532 §4.3 is emphatic about it: nothing here says an
agent loop TRIES rung 0 first. A route condition can only check the ordering
of a PLAN, and what walks the ladder is a loop, so a plan can name the rungs
in order and the loop can still take the third first. revl bounds the reach.
It does not claim the order, and an implied ordering guarantee would be worse
than no ladder at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import ui_family  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.policy import (  # noqa: E402
    approval_admission, component_reach, evaluate, parse_policy,
)


# ------------------------------------------------------------------ fixtures

RECORD = ui_family.target_record_shape() + "\n"

OBSERVE = (
    'extern emission[screen.observe] fn obs(r: Str) -> Str = @py { return "" }\n'
    'extern emission[ui.find] fn find(s: Str, n: Str) -> UiTarget\n'
    '  = @py { return None }\n'
)
CLICK = ('extern emission[ui.click] fn click(t: UiTarget) -> Int\n'
         '  = @py { return 0 }\n')
SELECTOR = ('extern emission[ui.click.selector] fn click_sel(t: UiTarget) -> Int\n'
            '  = @py { return 0 }\n')
PIXEL = ('extern emission[ui.click.pixel] fn click_px(t: UiTarget) -> Int\n'
         '  = @py { return 0 }\n')

HEAD = ('service Worker { emission fn approve(region: Str) -> Int }\n'
        'component Billing provides worker: Worker {\n'
        '  provide worker {\n'
        '    fn approve(region) {\n'
        '      let seen = emit obs(region)\n'
        '      let t = emit find(seen, "Approve")\n')
TAIL = '    }\n  }\n}\n'


def _program(externs: str, body: str) -> str:
    return RECORD + OBSERVE + externs + HEAD + body + TAIL


def _refusal(src: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "ui_ladder_521.rvl")
    return excinfo.value


def _admits(src: str) -> dict:
    return compile_source(src, "ui_ladder_521.rvl")


FULL_LADDER = _program(
    CLICK + SELECTOR + PIXEL,
    "      let a = emit click(t)\n"
    "      let b = emit click_sel(t)\n"
    "      return emit click_px(t)\n")

PIXEL_DRIVER = _program(
    CLICK + PIXEL, "      return emit click_px(t)\n")

SEMANTIC_ONLY = _program(CLICK, "      return emit click(t)\n")


# ---------------------------------------------------------- the rung registry

def test_the_rung_set_is_closed_and_each_rung_records_its_failure() -> None:
    """A rung names HOW a target is bound, and each carries its own failure
    direction. An invented rung is a depth an auditor cannot grade and a
    `capability ui.*.pixel` rule cannot select, which is the same argument the
    closed VERB set rests on one level up."""
    assert set(ui_family.RUNGS) == {"selector", "pixel"}
    assert set(ui_family.RUNG_DEPTH) == set(ui_family.RUNGS)
    for rung, why in ui_family.RUNGS.items():
        assert why.strip(), rung


def test_the_depths_are_the_designs_and_a_semantic_token_is_depth_zero(
) -> None:
    assert ui_family.RUNG_DEPTH == {"selector": 1, "pixel": 2}
    assert ui_family.depth_of("ui.click") == 0
    assert ui_family.depth_of("ui.click.selector") == 1
    assert ui_family.depth_of("ui.click.pixel") == 2


def test_a_token_outside_the_family_is_depth_zero() -> None:
    """The answer that makes `max(depth_of(t) for t in reach)` safe over a
    mixed capability set, which is how an auditor reads a component's depth
    off the reach with no new reporting code."""
    assert ui_family.depth_of("db.write") == 0
    assert ui_family.depth_of("shell") == 0


def test_a_rung_resolves_to_its_verb_and_its_shallower_spelling() -> None:
    assert ui_family.verb_of("ui.click.pixel") == "ui.click"
    assert ui_family.verb_of('ui.click.pixel(host="billing.internal")') == "ui.click"
    assert ui_family.prefix_of("ui.click.pixel") == "ui.click"
    assert ui_family.prefix_of("ui.click") is None
    assert ui_family.rung_of("ui.click.pixel") == "pixel"
    assert ui_family.rung_of("ui.click") is None
    assert ui_family.verb_of("db.write") is None


def test_rungs_are_spellable_only_on_the_verbs_that_act_on_a_target() -> None:
    """A rung is a way of naming a target FOR AN ACTUATION, and §4.1 states
    both lower rungs' failure direction as the ACTION succeeding on the wrong
    thing. `screen.observe` has no target. `ui.find` produces one, so a rung
    there would name the substrate's resolution strategy, which §4.3 and §7
    put outside what revl claims."""
    assert ui_family.runged_verbs() == ui_family.TARGET_CONSUMERS
    for verb in ui_family.TARGET_CONSUMERS:
        for rung in ui_family.RUNGS:
            assert ui_family.refusal(f"{verb}.{rung}", "emission") is None
    for verb in ("ui.find", "screen.observe"):
        message, _ = ui_family.refusal(f"{verb}.pixel", "emission")
        assert "does not act on a target" in message


# ------------------------------------------------- the declaration-site bounds

def test_an_invented_rung_is_refused() -> None:
    error = _refusal(
        "extern emission[ui.click.zoom] fn c(t: Str) = @py { pass }\n")
    assert "not a declared rung" in error.message
    assert "CLOSED" in (error.hint or "")
    assert error.code == "G8"


def test_a_fourth_level_is_refused() -> None:
    """An unbounded ladder is the same as no guarantee, which is the item's own
    sentence. A fourth level would be a depth with no failure direction written
    down for it."""
    error = _refusal(
        "extern emission[ui.click.pixel.exact] fn c(t: Str) = @py { pass }\n")
    assert "deeper than the computer-use fallback ladder goes" in error.message
    assert error.code == "G8"


def test_the_declaration_site_does_not_check_prefix_closure() -> None:
    """Stated as a scope fact rather than left to be inferred. Slice 1's hook
    sees ONE token with no component context, and prefix-closure is a property
    of a SET. An `extern emission[ui.click.pixel]` on its own is a well-formed
    declaration; whether a COMPONENT may reach it is the other check."""
    assert ui_family.refusal("ui.click.pixel", "emission") is None
    _admits(RECORD
            + "extern emission[ui.click.pixel] fn c(t: UiTarget) -> Int\n"
              "  = @py { return 0 }\n")


# ------------------------------------------------------ the prefix-closure rule

def test_a_component_reaching_both_rungs_is_admitted() -> None:
    _admits(FULL_LADDER)


def test_a_component_reaching_a_pixel_and_no_semantic_target_is_refused(
) -> None:
    """§4.2(4). A program that can reach pixels but not semantic targets has no
    ladder, it has a pixel driver, and the ordering claim is vacuous for it."""
    error = _refusal(PIXEL_DRIVER)
    assert ("component `Billing` reaches `ui.click.pixel` without reaching "
            "`ui.click`") == error.message
    assert "pixel driver" in (error.hint or "")
    assert error.code == "G8"
    assert classify(error)["code"] == "G8"


def test_a_component_reaching_a_selector_and_no_semantic_target_is_refused(
) -> None:
    """The rule is about the rung ONE step shallower being present, not about
    the pixel specifically. A selector is already the fail-open direction: it
    matches a different control that satisfies it."""
    error = _refusal(_program(CLICK + SELECTOR,
                              "      return emit click_sel(t)\n"))
    assert "reaches `ui.click.selector` without reaching `ui.click`" \
        in error.message


def test_a_declared_but_unreached_semantic_verb_does_not_close_a_rung(
) -> None:
    """The load-bearing consequence of checking over the G8 REACH rather than
    over the declaration list, and the reason the check could not live at the
    declaration site. `ui.click` is declared here on an extern the component
    never calls, so the component cannot reach a semantic target: it is a pixel
    driver with a semantic verb in its file. The audit already reports what a
    component can reach rather than what its file mentions (slice 1's §6), and
    this rule reads the same set."""
    error = _refusal(PIXEL_DRIVER)
    assert "extern emission[ui.click]" in PIXEL_DRIVER
    assert "without reaching `ui.click`" in error.message


def test_a_semantic_only_component_is_untouched() -> None:
    """The control. A component that never descends the ladder is not affected
    by a rule about descending it."""
    ir = _admits(SEMANTIC_ONLY)
    assert {r.token for r in component_reach(audit_report(ir), "Billing")} == {
        "screen.observe", "ui.find", "ui.click"}


def test_a_program_with_no_rung_token_runs_no_boundary_walk() -> None:
    """Inertness, stated as the property the pre-scan buys: the check costs one
    loop over the extern list for every program in the tree that declares no
    rung, and the `_boundary` walk runs only past it."""
    from revl import lower

    walked = []
    import revl.boundary as boundary
    original = boundary._boundary

    def spy(ir):
        walked.append(1)
        return original(ir)

    boundary._boundary = spy
    try:
        lower._check_ui_rung_prefix_closure(
            __import__("revl.parser", fromlist=["Parser"]).Parser(
                SEMANTIC_ONLY, "x.rvl").parse(), {}, "x.rvl")
    finally:
        boundary._boundary = original
    assert walked == []


def test_a_narrowed_semantic_token_closes_a_rung() -> None:
    """item 294's parameters NARROW a capability, so a component holding
    `ui.click(host="billing.internal")` still holds a semantic token. Refusing here
    would make a narrowing an author wrote to reduce authority into a reason
    their ladder stopped closing, which is the wrong incentive to put on the
    one feature that reduces reach."""
    _admits(_program(
        'extern emission[ui.click(host="billing.internal")] fn click(t: UiTarget) -> Int\n'
        '  = @py { return 0 }\n' + PIXEL,
        "      let a = emit click(t)\n"
        "      return emit click_px(t)\n"))


# -------------------------------------------- what a rung inherits, and why

def test_a_rung_inherits_its_verbs_taint_role() -> None:
    """Slice 2 left this for slice 3 by resolving `taint_roles` by longest
    registered PREFIX. A rung is a strictly weaker way to name the same
    target, so it must never carry a weaker taint role than the verb it
    descends from; an exact-match table would have given it NO role, which is
    the fail-open direction reached by adding a spelling in a different file."""
    assert ui_family.is_taint_sink("ui.click.pixel")
    assert ui_family.is_taint_sink("ui.text.selector")
    assert ui_family.is_taint_sink("ui.download.pixel")


def test_a_rung_inherits_its_verbs_reversibility_class() -> None:
    """The same argument on item 522's table, which is exact-match by nature.
    `ui.click.pixel` is `ui.click` reached by a weaker naming, so it cannot be
    more reversible than it; a rung with NO class is one `teardown_refusal`
    reads as "not a computer-use verb" and lets past."""
    assert ui_family.reversibility("ui.click.pixel") == ui_family.UNKNOWN
    assert ui_family.reversibility("ui.text.pixel") == ui_family.COMPENSATABLE
    assert ui_family.reversibility("ui.download.selector") == \
        ui_family.IRREVERSIBLE


def test_a_rung_carries_its_verbs_compensation_obligation() -> None:
    """Measured through a compile rather than at the table, because the table
    being right and the refusal reading it are two facts."""
    error = _refusal(
        RECORD + "extern emission[ui.download.pixel] fn get(t: UiTarget) -> Str\n"
        "  compensate drop_it() = @py { return \"\" }\n"
        "fn drop_it() { }\n")
    assert "may not declare `compensate`" in error.message


def test_a_rung_carries_its_verbs_target_obligation() -> None:
    """Slice 4's signature check resolves the verb first, so a rung cannot be
    the spelling that escapes the target binding. Without it, declaring
    `emission[ui.click.pixel]` would have been a way to take a bare string
    target again."""
    error = _refusal(
        RECORD + "extern emission[ui.click.pixel] fn c(t: Str) -> Int\n"
        "  = @py { return 0 }\n")
    assert "`emission[ui.click]` acts on a target" in error.message


# ----------------------------------------------- what the audit and policy see

def test_the_audit_reach_prints_the_deepest_rung() -> None:
    """§4.2(2): the depth is on the audit surface for free, because the audit
    prints declared capability tokens and the rung is part of the token. No
    new reporting code, which is the whole reason a rung is spelled into the
    token rather than carried beside it."""
    reach = {r.token for r in
             component_reach(audit_report(_admits(FULL_LADDER)), "Billing")}
    assert reach == {"screen.observe", "ui.find", "ui.click",
                     "ui.click.selector", "ui.click.pixel"}
    assert max(ui_family.depth_of(t) for t in reach) == 2
    assert max(ui_family.depth_of(t) for t in
               {r.token for r in component_reach(
                   audit_report(_admits(SEMANTIC_ONLY)), "Billing")}) == 0


def test_an_allow_list_bounds_the_depth() -> None:
    """A rung is an ordinary capability token, so the existing `may reach`
    machinery bounds the ladder with no new policy surface: an allow-list that
    stops at the semantic verb refuses a component that reaches a pixel."""
    audit = audit_report(_admits(FULL_LADDER))
    named = ("component Billing may reach screen.observe, ui.find, ui.click, "
             "ui.click.selector, ui.click.pixel")
    assert evaluate(parse_policy(named), audit) == []
    stops_at_rung_0 = ("component Billing may reach screen.observe, ui.find, "
                       "ui.click")
    assert evaluate(parse_policy(stops_at_rung_0), audit) != []


@pytest.mark.parametrize("rule", [
    "capability ui.*.pixel requires approval",
    "capability ui.click.pixel requires approval",
])
def test_an_approval_rule_selects_a_rung_by_glob(rule: str) -> None:
    """§4.2(2)'s other half, and the one the design names as this slice's
    oracle. The operator writes the rule against the token, and the item-246
    machinery selects it with nothing added.

    Checked through `policy.approval_admission`, which is the function that
    enforces the rule. `policy.evaluate` does NOT run it on this tree, and that
    is item 522's finding rather than a gap here: PR #1287 reports that the
    gate was reachable only from `revl.mcp.session`, so the static surface read
    clean while the session refused, and moves `revl audit --policy` onto the
    same gate."""
    ir = _admits(FULL_LADDER)
    assert [v.kind for v in approval_admission(parse_policy(rule), ir)] == \
        ["approval"]


def test_an_approval_rule_over_another_namespace_does_not_select_a_rung(
) -> None:
    """The control for the two above: without it they would only say that the
    approval gate refuses things."""
    ir = _admits(FULL_LADDER)
    assert approval_admission(
        parse_policy("capability db.* requires approval"), ir) == []


# ----------------------------------------------------- the self-host obligation

def test_the_reserved_roots_are_a_declared_frontier_gap() -> None:
    """Design 532 §9's obligation, discharged by a NAMED DECLINE rather than by
    a port.

    The generator derives a third frontier axis from `ui_family.ROOTS`, so the
    native gate answers `outside_frontier` with a reason on any source carrying
    a reserved capability namespace instead of returning no objection. This is
    the registry half; the wire half is
    `tests/test_gate_crate_admit.py::test_a_construct_outside_the_frontier_is_
    declined_not_admitted`, which is the file that shells cargo.

    The table empties itself when the port lands: it is
    `ui_family.ROOTS - selfhost/lower.rvl::reserved_capability_root`, so the
    roots a ported check names leave it in the same wave and the drift gate
    makes that visible."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_gate_crate_ladder_521", ROOT / "tools" / "build_gate_crate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tables = module.frontier_tables()
    assert tables["capability_roots"] == sorted(ui_family.ROOTS)
    assert "src/revl/ui_family.py" in module.DIGEST_INPUTS
    generated = (ROOT / "crates" / "revl-gate" / "src" / "frontier.rs").read_text(
        encoding="utf-8")
    for root in ui_family.ROOTS:
        assert f'"{root}",' in generated, root


# ------------------------------------------------------------- non-vacuity

def test_non_vacuity_the_ladder_was_not_writable() -> None:
    """On `4cfc8f32` `parser._capability_list` refused EVERY token deeper than
    a verb, so none of the four ladders below could be written at all: the two
    admitted ones did not compile, and the two refused ones carried slice 1's
    blanket "not admissible yet" rather than a prefix-closure verdict. The
    measurement is therefore two-sided, which a file that only added refusals
    would not be."""
    admitted = [FULL_LADDER, SEMANTIC_ONLY]
    refused = [PIXEL_DRIVER,
               _program(CLICK + SELECTOR, "      return emit click_sel(t)\n")]
    for src in admitted:
        _admits(src)
    for src in refused:
        error = _refusal(src)
        assert error.code == "G8"
        assert "without reaching" in error.message
        assert "not admissible yet" not in error.message
    assert len(admitted) == 2 and len(refused) == 2

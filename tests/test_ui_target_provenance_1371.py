"""Where an actuated computer-use target came from (roadmap item 521, issue
#1371, docs/design/565-ui-target-binding.md §7).

Slice 4 (PR #1325) landed the `UiTarget` record and two signature
obligations: `ui.find` returns one, an actuation takes one. §7 then recorded
what that does not claim - "revl checks the signature, not the dataflow
between two crossings. A program may resolve a target and act on a different
one" - and named slice 2's taint discipline as what bounds it.

THE MEASUREMENT THAT MOVES THIS, taken on `fc0d84ce`. The bound runs the
wrong way round. `ui.find` is a taint SOURCE and `ui.click` is an
all-arguments SINK, so under `taint_strict`:

    resolve a target, then click it                     REFUSED  (G9)
    click a `UiTarget` record literal written in-line   ADMITTED

and the second is admitted under EVERY profile. A forged target carries no
origin, so there is nothing on it to refuse; taint bounds what a value is
DERIVED FROM and cannot bound a value derived from nothing. The discipline
named as the bound refuses the honest program and admits the forged one.
`test_the_discipline_named_as_the_bound_refuses_the_honest_program_only`
below is that measurement, kept as a test so the inversion cannot come back
unnoticed.

Nine more spellings of the same program were admitted on `fc0d84ce`,
including one where a SECOND COMPONENT builds the target and hands it over a
service, and one where a `pure` extern mints it. They are in
`_ADMITTED_ON_FC0D84CE` and each is a refusal here.

WHAT THIS CLOSES. The invariant, stated as the property rather than as the
three refusals: in an admitted program, every `UiTarget` originates in a
target-producing crossing. `lower._check_ui_target_provenance` holds it by
refusing the CONSTRUCTION rather than the flow to a particular use, so
completeness is not a property of a walk - there is no dataflow position to
miss. That placement is deliberate and issue #1327 is why: a walk that named
three statement kinds missed seven spellings of the same crossing, every miss
in the fail-open direction.

WHAT IT DOES NOT CLOSE, and issue #1371 says so itself. Not that the target
is the one resolved for THIS step: a program that resolves two targets and
acts on the second acted on a target it resolved, and that program still
admits. Not that the resolution is still fresh. Both need the substrate that
actually resolves a target (item 539, upstream inso1337/revl-harness#11).

THE ONE ENTRY POINT LEFT OPEN BY NAME, measured in
`test_a_config_injected_target_is_admitted_and_that_is_the_stated_cut`: a
component `config` field typed `UiTarget`. An operator supplying a target
through the environment contract is the same authority that granted the
component `ui.click`, and it is not the program writing authority for itself.
Two fixtures in this repository now enter a target that way, because an
activation body cannot bind an emission result at all and an activation body
is the only place a computer-use crossing is confirmable today.

WHY NOT IN THE TRANSACTION UNIT. `ui_transaction` already computes
`targetResolvedBy` for each actuating step (item 522 slice 5, issue #1370),
and it is `null` for the forged click and `"ui_find"` for the honest one - so
the report can already see the difference and issues no verdict on it. It
cannot be promoted to one as it stands: `targetResolvedBy` is also `null` for
a HONEST target passed through a local `fn`, measured in
`test_the_report_field_cannot_be_promoted_to_a_verdict`, so refusing on it
would refuse correct programs. That is the measured reason this lands in the
type and capability layer instead.

SCOPE. No step executes: no `@py` body in this file is ever run, and revl
still drives no desktop (design 532 §7).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import ui_family, ui_transaction  # noqa: E402
from revl.admit_profile import AdmissionProfile  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402


# ------------------------------------------------------------------ fixtures

RECORD = """type UiTarget = {
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
"""

# The family in its post-slice-4 spelling. `ui.text` carries its item-522
# `compensate` because a compensatable verb without one is a G4 refusal and
# would mask the G8 answer this file measures.
FAMILY = (
    'extern emission[screen.observe] fn screen_observe(region: Str) -> Str\n'
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

# Every field the registry names, with a plausible value for each. Nothing
# looked at a screen to produce any of them: `evidence` names a screen nobody
# read and `expiry` is an instant nobody measured.
LITERAL = ('{ application: "Billing", window: "Invoice", role: "button", '
           'name: "Approve", evidence: "sha256:0", action: "click", '
           'session: "s", bounds: "0,0,1,1", expiry: 0, confirm: false }')


def _worker(body: str, extra: str = "") -> str:
    return (
        RECORD + FAMILY + extra
        + 'service Worker { emission fn approve(region: Str) -> Int }\n'
        'component Billing provides worker: Worker {\n'
        '  provide worker {\n'
        f'    fn approve(region) {{\n{body}    }}\n'
        '  }\n'
        '}\n'
    )


#: The control: a target is resolved and the target resolved is the one acted
#: on. Every program below is this one with one thing changed, so a refusal
#: here would make the rest measure nothing.
CANONICAL = _worker(
    '      let seen = emit screen_observe(region)\n'
    '      let target = emit ui_find(seen, "Approve")\n'
    '      return emit ui_click(target)\n')

#: THE REPRODUCER. It resolves one target and acts on another: `good` is the
#: Cancel button the program looked at, `forged` is a record the program wrote
#: for itself, and the click lands on `forged`. Admitted on `fc0d84ce` under
#: both profiles.
RESOLVE_ONE_ACT_ON_ANOTHER = _worker(
    '      let seen = emit screen_observe(region)\n'
    '      let good = emit ui_find(seen, "Cancel")\n'
    f'      let forged = {LITERAL}\n'
    '      return emit ui_click(forged)\n')


def _strict() -> AdmissionProfile:
    return AdmissionProfile(taint_strict=True)


def _refusal(src: str, profile=None) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "ui_provenance_1371.rvl", profile=profile)
    return excinfo.value


def _admits(src: str, profile=None) -> dict:
    return compile_source(src, "ui_provenance_1371.rvl", profile=profile)


def _click_step(src: str) -> dict:
    """The `ui.click` step of the one transaction plan that has one."""
    for plan in ui_transaction.plans(_admits(src)):
        for step in plan["steps"]:
            if step["extern"] == "ui_click":
                return step
    raise AssertionError("no ui_click step in any plan")


# ------------------------------------------------------------- the reproducer

def test_the_canonical_program_still_admits() -> None:
    """The control. A target resolved by `ui.find` and acted on by `ui.click`
    is the program this family exists for, and nothing here narrows it."""
    _admits(CANONICAL)


def test_resolving_one_target_and_acting_on_another_is_refused() -> None:
    """Issue #1371's own sentence, as a program: it resolves the Cancel button
    and clicks a target it wrote itself, with the resolution left in the body
    so the program reads like one that looked before it acted.

    Admitted on `fc0d84ce` under every profile. Refused here, at the LITERAL
    rather than at the click, because that is where the target enters the
    program and it is the line the author has to change."""
    error = _refusal(RESOLVE_ONE_ACT_ON_ANOTHER)
    assert classify(error)["code"] == "G8"
    assert "record literal" in error.message
    assert "nothing resolved" in error.message


def test_the_discipline_named_as_the_bound_refuses_the_honest_program_only(
) -> None:
    """Design 565 §7 names slice 2's taint discipline as what bounds the race.
    Measured, it bounds the opposite program.

    Under `taint_strict` the CANONICAL program is refused (a target carries
    the `screen` origin and every argument of `ui.click` is a sink), and on
    `fc0d84ce` the forged one was admitted under that same profile. A literal
    is not untrusted data - nobody looked at a screen to write it - so there
    is no origin on it to refuse.

    The second half is asserted through `ui_family` rather than through a
    compile, because on this tree the forged program no longer reaches the
    taint pass: it is refused before it, which is the point."""
    honest = _refusal(CANONICAL, _strict())
    assert classify(honest)["code"] == "G9"
    assert "untrusted value (screen)" in honest.message

    # The half that made the forged program clean: a literal has no origin, so
    # the taint pass has nothing to say about it.
    assert ui_family.source_origin("ui.find") == "screen"
    assert ui_family.is_taint_sink("ui.click")
    forged = _refusal(RESOLVE_ONE_ACT_ON_ANOTHER, _strict())
    assert classify(forged)["code"] == "G8", (
        "the forged program is refused for its PROVENANCE, not for its taint: "
        "if this ever reports G9 the taint pass has started seeing literals "
        "and this file's argument needs remeasuring")


# ---------------------------------------------- the nine spellings that moved

#: Every program admitted on `fc0d84ce` that acts on a target no crossing
#: resolved, one per way of getting the target there. The list is the
#: measurement `test_every_spelling_that_was_admitted_is_refused` replays.
_ADMITTED_ON_FC0D84CE = {
    "the literal inline in the call": _worker(
        f'      return emit ui_click({LITERAL})\n'),
    "the literal let-bound and renamed twice": _worker(
        f'      let a = {LITERAL}\n'
        '      let b = a\n'
        '      let c = b\n'
        '      return emit ui_click(c)\n'),
    "a mutable binding rebound from a resolution to a literal": _worker(
        '      let seen = emit screen_observe(region)\n'
        '      var t = emit ui_find(seen, "Approve")\n'
        f'      t = {LITERAL}\n'
        '      return emit ui_click(t)\n'),
    "a helper fn that returns one": _worker(
        '      return emit ui_click(forge())\n',
        extra=f'fn forge() -> UiTarget {{ return {LITERAL} }}\n'),
    "text typed into a target nothing resolved": _worker(
        f'      emit ui_text({LITERAL}, "1000")\n'
        '      return 0\n'),
    "a file downloaded from a target nothing resolved": _worker(
        f'      let f = emit ui_download({LITERAL})\n'
        '      return 0\n'),
    "a pure extern minting the record": _worker(
        '      return emit ui_click(mint())\n',
        extra='extern pure fn mint() -> UiTarget = @py { return None }\n'),
    "the resolved target renamed before the click": _worker(
        '      let seen = emit screen_observe(region)\n'
        '      let cancel = emit ui_find(seen, "Cancel")\n'
        '      return emit ui_click({ cancel | name = "Approve" })\n'),
    # The one a single-body check could not have seen: the target is built in
    # a DIFFERENT component and handed over a service.
    "a second component building it and handing it over a service": (
        RECORD + FAMILY
        + 'service Worker { emission fn approve(region: Str) -> Int }\n'
        'service Resolver { emission fn pick(n: Str) -> UiTarget }\n'
        'component Fake provides resolver: Resolver {\n'
        '  provide resolver {\n'
        f'    fn pick(n) {{\n      return {LITERAL}\n    }}\n'
        '  }\n'
        '}\n'
        'component Billing requires resolver: Resolver '
        'provides worker: Worker {\n'
        '  provide worker {\n'
        '    fn approve(region) {\n'
        '      let t = emit resolver.pick("Approve")\n'
        '      return emit ui_click(t)\n'
        '    }\n'
        '  }\n'
        '}\n'),
}


@pytest.mark.parametrize("label", sorted(_ADMITTED_ON_FC0D84CE))
def test_every_spelling_that_was_admitted_is_refused(label: str) -> None:
    """Nine programs, one per route a target took into an actuation without
    being resolved. All nine compiled on `fc0d84ce`; all nine are G8 here,
    under the default profile and not behind one."""
    error = _refusal(_ADMITTED_ON_FC0D84CE[label])
    assert classify(error)["code"] == "G8", label
    assert classify(error)["category"] == "boundary", label


def test_the_nine_spellings_are_nine_distinct_programs() -> None:
    """A list whose entries collapsed to the same source would report nine
    measurements and take one."""
    assert len(set(_ADMITTED_ON_FC0D84CE.values())) == 9


# ------------------------------------------------------- the three refusals

def test_a_target_built_as_a_record_literal_is_refused() -> None:
    """CONSTRUCTED. The refusal names what the literal cannot carry - an
    observation behind the evidence hash - rather than only that it is
    refused, and it says out loud that taint does not catch this, because an
    author who has read design 565 §7 would otherwise expect it to."""
    error = _refusal(_worker(f'      return emit ui_click({LITERAL})\n'))
    assert "built as a record literal" in error.message
    assert "ui.find" in error.hint
    assert "forged target is cleaner than a real one" in error.hint


def test_an_extern_that_returns_a_target_must_declare_the_resolution() -> None:
    """MINTED. A second host boundary handing back the record is the way
    around the rule rather than a use of it, and it is refused from the
    DECLARATION table: no body has to be read for it, so no walk can miss it.

    The refusal fires on the declaration even where the program never calls
    it. An extern is an authority the component holds, and this one is the
    authority to produce a target without declaring a screen read."""
    src = _worker(
        '      let seen = emit screen_observe(region)\n'
        '      let t = emit ui_find(seen, "Approve")\n'
        '      return emit ui_click(t)\n',
        extra='extern pure fn mint() -> UiTarget = @py { return None }\n')
    error = _refusal(src)
    assert classify(error)["code"] == "G8"
    assert "extern `mint` returns `UiTarget`" in error.message
    assert "emission[ui.find]" in error.message


def test_the_producer_itself_is_not_refused_for_returning_a_target() -> None:
    """The obvious way to get MINTED wrong. `ui.find` returns the record by
    obligation (slice 4's own signature check demands it), so a rule written
    as "no extern returns a target" would refuse the one extern that must."""
    _admits(CANONICAL)
    assert "ui.find" in ui_family.TARGET_PRODUCERS


def test_a_resolved_target_may_not_be_renamed_before_the_actuation() -> None:
    """REBOUND. The ten registry fields are one binding: an update keeps the
    Cancel button's evidence hash and points the click at Approve, which is
    the check-to-use race written in one expression."""
    error = _refusal(_worker(
        '      let seen = emit screen_observe(region)\n'
        '      let cancel = emit ui_find(seen, "Cancel")\n'
        '      return emit ui_click({ cancel | name = "Approve" })\n'))
    assert classify(error)["code"] == "G8"
    assert "`name` is rewritten" in error.message
    assert ui_family.TARGET_FIELD_BINDS["name"][:20] in error.hint


@pytest.mark.parametrize("field", sorted(ui_family.TARGET_FIELDS))
def test_every_registry_field_is_refused_when_rewritten(field: str) -> None:
    """Not one special case for `name`: the whole binding travels together, so
    `confirm = false` before a click (item 522 owns the gate, the target
    carries the fact) is refused by the same rule as `evidence`."""
    value = {"Str": '"x"', "Int": "1", "Bool": "false"}[
        ui_family.TARGET_FIELDS[field]]
    error = _refusal(_worker(
        '      let seen = emit screen_observe(region)\n'
        '      let t = emit ui_find(seen, "Approve")\n'
        f'      return emit ui_click({{ t | {field} = {value} }})\n'))
    assert classify(error)["code"] == "G8", field
    assert f"`{field}` is rewritten" in error.message, field


def test_a_field_the_registry_does_not_name_may_still_be_updated() -> None:
    """Design 565 §3's floor, held. An extra field is the author's own and
    carries no authority, so refusing an update of it would be a false refusal
    with no soundness gain - the same argument that admits the extra field on
    the record in the first place."""
    record = RECORD.replace("  confirm: Bool\n",
                            "  confirm: Bool\n  note: Str\n")
    src = (record + FAMILY
           + 'service Worker { emission fn approve(region: Str) -> Int }\n'
           'component Billing provides worker: Worker {\n'
           '  provide worker {\n'
           '    fn approve(region) {\n'
           '      let seen = emit screen_observe(region)\n'
           '      let t = emit ui_find(seen, "Approve")\n'
           '      return emit ui_click({ t | note = "retry 1" })\n'
           '    }\n'
           '  }\n'
           '}\n')
    _admits(src)
    assert "note" not in ui_family.TARGET_FIELDS


# --------------------------------------------------- the scope, stated and met

def test_a_program_that_never_actuates_may_build_a_target() -> None:
    """The check is gated on a declared target CONSUMER, not on the family.
    A program that resolves targets and acts on none has no authority at
    stake, and design 565 §3's floor argument says not to refuse where there
    is none."""
    src = (RECORD
           + 'extern emission[screen.observe] fn screen_observe(r: Str) -> Str\n'
           '  = @py { return "" }\n'
           'extern emission[ui.find] fn ui_find(s: Str, n: Str) -> UiTarget\n'
           '  = @py { return None }\n'
           'service Worker { emission fn look(region: Str) -> Int }\n'
           'component Billing provides worker: Worker {\n'
           '  provide worker {\n'
           '    fn look(region) {\n'
           f'      let t = {LITERAL}\n'
           '      return 1\n'
           '    }\n'
           '  }\n'
           '}\n')
    _admits(src)


def test_a_target_passed_through_a_helper_fn_still_admits() -> None:
    """The false refusal this placement avoids. A target resolved by `ui.find`
    and handed to the actuation through a local `fn` is an ordinary program,
    and nothing here traces it - it does not have to, because the refusal is
    on the CONSTRUCTION and a value that was never constructed came from a
    crossing."""
    _admits(_worker(
        '      let seen = emit screen_observe(region)\n'
        '      let t = emit ui_find(seen, "Approve")\n'
        '      return emit ui_click(pick(t))\n',
        extra='fn pick(t: UiTarget) -> UiTarget { return t }\n'))


def test_a_program_that_resolves_two_targets_and_acts_on_the_second_admits(
) -> None:
    """Stated rather than left implicit, because issue #1371's title reads
    like this program is the defect. It is not: both targets were resolved, so
    the program acted on a target it resolved. What is NOT checked is that it
    is the target resolved for THIS step, and that needs a resolved handle a
    phase boundary carries (item 539), which this does not promise."""
    _admits(_worker(
        '      let seen = emit screen_observe(region)\n'
        '      let a = emit ui_find(seen, "Cancel")\n'
        '      let b = emit ui_find(seen, "Approve")\n'
        '      return emit ui_click(b)\n'))


def test_a_config_injected_target_is_admitted_and_that_is_the_stated_cut(
) -> None:
    """The one entry point left open, measured rather than described.

    A component `config` field typed `UiTarget` is a target the operator
    supplied through the environment contract (item 350). It is not a
    resolution and this does not pretend it is; what it is not is the program
    writing authority for itself, and the operator supplying a target is the
    same authority that granted the component `ui.click`.

    It is load-bearing, not hypothetical: an activation body cannot bind an
    emission result (`let t = emit ...` there is a G6 refusal), and an
    activation body is the only place an `Approval[C]` can be minted today, so
    a confirmable computer-use crossing can only act on a target that entered
    the component some other way. `tests/test_ui_transaction_phases_522.py`
    enters it this way for exactly that reason.

    The failure direction is OPEN, deliberately, and this test is where it is
    written down. Closing it means refusing a `UiTarget` config field
    outright, which today would make a confirmable UI crossing unwritable."""
    src = (RECORD
           + 'extern emission[ui.click] fn actuate(t: UiTarget) '
           '= @py { return None }\n'
           'service Ops { fn ping() -> Int }\n'
           'component Clicker provides ops: Ops {\n'
           '  config { target: UiTarget }\n'
           '  emit actuate(config.target)\n'
           '  provide ops { fn ping() = 1 }\n'
           '}\n')
    _admits(src)


# ------------------------------------------------ why not the transaction unit

def test_the_report_field_cannot_be_promoted_to_a_verdict() -> None:
    """`ui_transaction` already records `targetResolvedBy` per actuating step
    (item 522 slice 5, issue #1370). For a forged click it is `null` and for
    an honest one it is the producer's name, so the report can see the
    difference and issues no verdict on it.

    It cannot simply become one. `targetResolvedBy` is `null` for an HONEST
    target that reached the actuation through a local `fn` as well, so a
    refusal keyed on it would refuse correct programs. That is the measured
    reason the race is closed in the type and capability layer instead, and it
    is why this file changes no line of `ui_transaction.py`.

    Both programs bind the click to a name rather than writing it in return
    position. PR #1412 taught the plan's walk to see a tail-position crossing
    (issue #1327), so either spelling works here now; the bound form is kept
    because it is the one that reads the same on both sides of that landing,
    and a fixture that depended on the walk's width would be measuring #1412
    rather than this file's claim."""
    honest = _click_step(_worker(
        '      let seen = emit screen_observe(region)\n'
        '      let t = emit ui_find(seen, "Approve")\n'
        '      let done = emit ui_click(t)\n'
        '      return done\n'))
    assert honest["targetResolvedBy"] == "ui_find"

    through = _click_step(_worker(
        '      let seen = emit screen_observe(region)\n'
        '      let t = emit ui_find(seen, "Approve")\n'
        '      let done = emit ui_click(pick(t))\n'
        '      return done\n',
        extra='fn pick(t: UiTarget) -> UiTarget { return t }\n'))
    assert through["targetResolvedBy"] is None, (
        "if the plan now traces a target through a callee, a refusal keyed on "
        "`targetResolvedBy` becomes writable and this file's placement "
        "argument should be remeasured")


# --------------------------------------------------------------- the registry

def test_every_origin_carries_a_reason_and_a_refusal() -> None:
    """The three words are a registry like `OBLIGATION` and `RUNGS`: an origin
    with no recorded reason is one nobody has to justify next time the list is
    edited, and the refusals quote the reason."""
    assert set(ui_family.TARGET_ORIGINS) == {
        ui_family.CONSTRUCTED, ui_family.REBOUND, ui_family.MINTED}
    for origin, why in ui_family.TARGET_ORIGINS.items():
        assert why.strip(), origin
        answer = ui_family.target_origin_refusal(origin, "name")
        assert answer is not None, origin
        message, hint = answer
        assert message.strip() and hint.strip(), origin
        assert "565-ui-target-binding.md" in hint, origin


def test_an_origin_outside_the_registry_refuses_nothing() -> None:
    """The caller passes a word this module owns. An unknown one returning a
    refusal would be a diagnostic with no rule behind it."""
    assert ui_family.target_origin_refusal("resolved", "x") is None


def test_the_refusals_name_the_producer_from_the_registry() -> None:
    """Item 274: a refusal names the nearest allowed space, and it renders it
    from the table rather than spelling it, so the diagnostic and the registry
    cannot drift."""
    for origin in ui_family.TARGET_ORIGINS:
        _message, hint = ui_family.target_origin_refusal(origin, "name")
        assert ui_family.TARGET_PRODUCERS[0] in hint, origin


# ------------------------------------------------------------ the self-host

@pytest.fixture(scope="module")
def selfhost_admit():
    """`selfhost/lower.rvl`'s `admit_src`, compiled by revl and executed
    through the python backend - the same harness `test_selfhost_lower.py`
    uses, and the same fixture `test_ui_target_binding_521.py` carries.

    Duplicated rather than shared, for the reason slice 4's copy exists at
    all: it is module-scoped because it compiles the self-host front end, and
    a fixture moved to `conftest.py` to serve two files would put that compile
    on the path of every test in the directory."""
    import importlib.util
    import types as _types

    from revl import compile_files

    ir = compile_files([str(ROOT / "selfhost" / "lower.rvl")])
    spec = importlib.util.spec_from_file_location(
        "pyemit_ui_provenance_1371", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = _types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace: dict = {}
        exec(compile(module.emit(ir), "selfhost_lower.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace["admit_src"]


def test_the_selfhost_gate_still_declines_the_namespace_by_name(
        selfhost_admit) -> None:
    """Does this need a self-host port? NO, and the reason is already landed
    rather than argued here.

    Design 565 §8 measured that `selfhost/lower.rvl` runs none of item 521's
    checks and that `admit_src` answers "" - no objection - on a program the
    reference refuses, which is agreement by silence. Slice 3 discharged that
    at the GATE instead of by a port: `tools/build_gate_crate.py`'s
    `capability_roots` frontier axis, derived from `ui_family.ROOTS`, makes
    the native gate answer `outside_frontier` for any source carrying `ui.` or
    `screen.`. A fourth reference refusal over the same namespace is covered
    by the same decline, so it adds no agreement obligation: there is no tag
    and no message for the two sides to disagree about, because the self-host
    side issues neither.

    This test keeps measuring the silence on the new refusal specifically, and
    fails by name the day `admit_src` grows a verdict there - at which point
    the port becomes real work and the message has to agree."""
    assert selfhost_admit(RESOLVE_ONE_ACT_ON_ANOTHER) == "", (
        "the self-host gate now has a verdict on a computer-use program; if "
        "that is a port of the item-521 checks, this test should assert the "
        "tag and the message rather than the silence (design 565 §8)")
    assert ui_family.ROOTS == ("screen", "ui")

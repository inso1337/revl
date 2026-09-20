"""The origin ceiling on the model role (roadmap item 514, issue #1188).

The executable spec for the VALUE half of model placement, slice S2 of
`docs/design/531-model-placement.md`. Item 512 checked what an author WRITES:
an arm spelling `confidential -> cloud` is refused. This checks what a program
DOES: a value whose taint already carries a confidentiality origin, reaching a
`model.*` crossing from an action whose `route model` block does not place that
origin on the device.

WHAT THIS FIRES ON, AND WHY IT IS NOT ALREADY COVERED. Item 256's disclosure
fence refuses a `confidential` value at any crossing whose receiver did not
declare `Secret[T]`. The residue it ADMITS is a crossing that did declare one,
and that declaration is a statement about what the receiver may RECEIVE and
says nothing whatever about where the model behind it runs. `_ADMITTED_TODAY`
below is that residue, and it compiled on the tree before this change.

These programs are written INLINE rather than dropped in `examples/` or
`tests/fixtures/`, for the reason `tests/test_model_placement_512.py` gives:
both directories are census corpus roots and `selfhost/parser.rvl` does not
parse `route model` yet, so an admitting fixture in either would become a
`false-reject` census entry. The self-host port is slice 3.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402

# Spelled here rather than imported from `revl.model_route`, so this module
# still COLLECTS against a tree that does not have the value-side rule. That is
# what makes the non-vacuity run readable: on the tree without this change the
# controls pass and every ceiling test fails, instead of the whole file
# erroring at import and proving nothing. `test_the_module_constants_match` is
# the tie-back that stops the two drifting.
CODE = "G-MODEL-PLACE"
SECRET_CODE = "G-SECRET-FLOW"
MODEL_SCOPE = "model"
CONFIDENTIALITY_ORIGINS = ("confidential", "secret")
CEILING_ORIGINS = ("confidential",)


# --------------------------------------------------------------------------
# The programs. One component, one confidential config field, one model
# crossing, and the route block as the only thing that varies.
# --------------------------------------------------------------------------

# A model crossing that DECLARES a `Secret[T]` receiver: item 256 admits a
# confidential argument here, which is precisely the seam item 514 closes.
_RECEIVER = ("extern emission[model.complete] fn prompt(p: Secret[Str]) -> Int "
             "= @py { return 0 }\n")
# The same crossing with no declared receiver: item 256 refuses it upstream, so
# it is the control that shows the two rules do not overlap.
_BARE = ("extern emission[model.complete] fn bare(p: Str) -> Int "
         "= @py { return 0 }\n")
# A crossing that is NOT a model call. The ceiling must not touch it.
_FS = ("extern emission[fs.write] fn store(p: Secret[Str]) -> Int "
       "= @py { return 0 }\n")

_ROLES = "model role local on_device\nmodel role cloud off_device\n"


def _program(route="", call="prompt", externs=_RECEIVER, body=None,
             extra="", roles=_ROLES):
    body = body or "      let r = %s(config.doc)\n" % call
    return (
        roles + externs + extra
        + "service Answer { emission fn summarize(d: Str) -> Int }\n"
        + "component Summarizer provides out: Answer {\n"
        + "  config { doc: Secret[Str] }\n"
        + route
        + "  provide out {\n"
        + "    fn summarize(d) {\n" + body + "      return 0\n"
        + "    }\n  }\n}\n")


# The gap, in one program: the confidential config field reaches a model
# crossing in an action routed `* -> cloud`. `*` does not cover a
# confidentiality origin, so nothing places this value, and before this change
# nothing said so.
_ADMITTED_TODAY = _program("  route model on summarize { * -> cloud }\n")

# The CONTROL. The same program with the origin named and placed on the device.
# It compiles on this tree AND on the tree without this change, so a red here
# is the harness and not the feature.
_CONTROL = _program("  route model on summarize { confidential -> local }\n")

# The second control: no `route model` block anywhere. The program declared no
# placement at all, which is the state of the world before item 512, so the
# ceiling has nothing to be above and item 256's fence is the whole rule. This
# is a SCOPE STATEMENT, not an oversight - see the module docstring of
# `revl.model_route`.
_UNROUTED = _program(roles="")


def _refuses(src, code=CODE, filename="ceiling.rvl"):
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, filename)
    err = excinfo.value
    assert getattr(err, "code", None) == code, (
        f"expected {code}, got {getattr(err, 'code', None)}: {err.message}")
    return err


def _admits(src, filename="ceiling.rvl"):
    return compile_source(src, filename)


# ==========================================================================
# 1. The exit test: a confidential value reaching an off-device placement.
# ==========================================================================

def test_a_confidential_value_reaching_a_cloud_routed_action_is_refused():
    """The roadmap's exit test, on a VALUE. `* -> cloud` is not a sentence that
    places a confidential input, and the program that relies on it does not
    compile."""
    _refuses(_ADMITTED_TODAY)


def test_the_diagnostic_names_the_origin_and_the_role():
    """The exit test's own words: the diagnostic names the origin and the
    role."""
    err = _refuses(_ADMITTED_TODAY)
    assert "confidential" in err.message
    assert "cloud" in err.message


def test_the_diagnostic_names_the_action_and_the_residence():
    """And what makes it fixable without reading the design note: which action
    carried the crossing, and what the role's declared residence is."""
    err = _refuses(_ADMITTED_TODAY)
    assert "summarize" in err.message
    assert "off_device" in err.message


def test_the_diagnostic_names_the_crossing_and_its_capability():
    """A refusal that named only the action would leave an author with several
    crossings guessing. It names the one that carried the value."""
    err = _refuses(_ADMITTED_TODAY)
    assert "prompt" in err.message
    assert "model.complete" in err.message


def test_the_hint_says_what_the_star_arm_does_not_cover():
    """The repair, and the definition the repair rests on."""
    err = _refuses(_ADMITTED_TODAY)
    assert "`*`" in err.hint
    assert "confidentiality origins" in err.hint


def test_the_refusal_carries_the_taint_path():
    """The value's own chain, so the author can see WHICH confidential value
    arrived, not merely that one did."""
    err = _refuses(_ADMITTED_TODAY)
    assert "config.doc" in err.hint


# ==========================================================================
# 2. The controls. These compile with and without the change.
# ==========================================================================

def test_a_confidential_value_routed_on_device_is_admitted():
    """The one admitting path: an arm names the origin and its role is declared
    `on_device`. Passes on BOTH trees, which is what makes it a control."""
    _admits(_CONTROL)


def test_a_component_that_routes_nothing_is_unmoved():
    """A program that declares no placement anywhere is the state of the world
    before item 512, and item 514 does not change it. Recorded as a test so the
    scope limit is visible rather than implied by silence."""
    _admits(_UNROUTED)


def test_a_non_confidential_value_is_not_refused_by_the_ceiling():
    """The ceiling is a CONFIDENTIALITY ceiling. An ordinary untrusted document
    reaching a cloud-routed action is the normal case and must still compile -
    a rule that refused it would be over-refusing, not fail-closed."""
    src = _program(
        "  route model on summarize { * -> cloud }\n",
        externs=(_RECEIVER
                 + "extern emission[model.complete] fn ask(p: Untrusted[Str]) "
                   "-> Int = @py { return 0 }\n"
                 + "extern emission[web.get] fn fetch(u: Str) -> Untrusted[Str] "
                   "= @py { return u }\n"),
        body="      let page = fetch(d)\n      let r = ask(page)\n")
    _admits(src)


def test_a_non_model_crossing_in_a_routed_action_is_unmoved():
    """A confidential value reaching a `fs.write` crossing that declares a
    receiver is item 472's and item 256's business, not this rule's. The
    ceiling is keyed to the `model.*` capability scope."""
    _admits(_program("  route model on summarize { * -> cloud }\n",
                     call="store", externs=_RECEIVER + _FS))


# ==========================================================================
# 3. The other two refusing verdicts.
# ==========================================================================

def test_an_unrouted_action_in_a_routed_component_is_refused():
    """The fail-open shape this item exists to remove. The author declared that
    placement is a property of this component; an action that escaped the
    declaration has no placement, and an undetermined placement refuses."""
    src = (
        _ROLES + _RECEIVER
        + "service Answer { emission fn summarize(d: Str) -> Int\n"
          "                 emission fn draft(d: Str) -> Int }\n"
        + "component Summarizer provides out: Answer {\n"
        + "  config { doc: Secret[Str] }\n"
        + "  route model on draft { confidential -> local }\n"
        + "  provide out {\n"
        + "    fn summarize(d) {\n      let r = prompt(config.doc)\n"
          "      return 0\n    }\n"
        + "    fn draft(d) { return 0 }\n"
        + "  }\n}\n")
    err = _refuses(src)
    assert "no `route model` placement" in err.message
    assert "summarize" in err.message


def test_the_off_device_verdict_refuses_a_value_even_if_the_arm_were_admitted():
    """`check()` refuses writing `confidential -> cloud`, so the value-level
    `off_device` verdict is unreachable through the surface today. It is a unit
    test rather than a program because the belt and the braces are the point: a
    later slice that admitted such an arm must not thereby admit the value."""
    from revl import model_route

    arms = {"confidential": {"role": "cloud", "residence": "off_device"}}
    verdict = model_route.admits(arms, "confidential", routed_component=True)
    assert not verdict.ok
    assert verdict.reason == "off_device"
    assert verdict.role == "cloud"


def test_admits_has_exactly_one_admitting_path_for_a_confidential_value():
    """The table, stated as a test. Every row but one refuses, and the one that
    admits names the origin AND places it on the device."""
    from revl import model_route

    on_device = {"role": "local", "residence": "on_device"}
    off_device = {"role": "cloud", "residence": "off_device"}
    rows = [
        # (arms, routed_component, expected reason)
        ({"confidential": on_device}, True, "ok"),
        ({"confidential": off_device}, True, "off_device"),
        ({"*": off_device}, True, "unplaced"),
        ({"*": on_device}, True, "unplaced"),
        ({"web": on_device}, True, "unplaced"),
        (None, True, "unrouted"),
        (None, False, "ok"),  # the component declared no placement at all
    ]
    for arms, routed, expected in rows:
        verdict = model_route.admits(arms, "confidential", routed)
        assert verdict.reason == expected, (arms, routed, verdict)
        assert verdict.ok == (expected == "ok"), (arms, routed, verdict)


# ==========================================================================
# 4. It does not contradict item 256 or item 512.
# ==========================================================================

def test_an_undeclared_receiver_still_cites_the_secret_flow_guarantee():
    """Item 256 refuses a confidential value at a crossing with no declared
    receiver, upstream of the ceiling. The two rules do not race: the
    G-SECRET-FLOW refusal stays, and it is the one an author sees."""
    _refuses(_program("  route model on summarize { confidential -> local }\n",
                      call="bare", externs=_RECEIVER + _BARE),
             code=SECRET_CODE)


def test_the_bound_key_re_entry_item_256_admits_is_not_refused_by_the_ceiling():
    """Item 256 section 4b ADMITS one crossing for a bound provider key: a
    re-entry into the same bound capability's own extern body, which is the
    provider making its own call. The ceiling must not take that away - a
    G-MODEL-PLACE refusal here would contradict a landed guarantee, which is
    why `CEILING_ORIGINS` holds `confidential` alone. Measured: this program
    compiles on the tree without this change, and must keep compiling."""
    src = (
        _ROLES
        + "secret openai_key for model.complete\n"
        + "extern emission[model.complete] fn complete(p: Str) -> Str "
          "= @py { return p }\n"
        + "extern emission[model.complete] fn echo(p: Str) -> Int "
          "= @py { return 0 }\n"
        + "service Answer { emission fn summarize(d: Str) -> Int }\n"
        + "component Summarizer provides out: Answer {\n"
        + "  route model on summarize { * -> cloud }\n"
        + "  provide out {\n"
        + "    fn summarize(d) {\n"
          "      let k = complete(d)\n      let x = echo(k)\n      return 0\n"
          "    }\n  }\n}\n")
    _admits(src)


def test_a_bound_secret_arm_is_still_refused_by_the_declaration_half():
    """The declaration half is unaffected. An ARM naming the `secret` origin is
    still refused, and still cites the guarantee that already forbids it."""
    src = (
        _ROLES
        + "secret openai_key for model.complete\n"
        + "extern emission[model.complete] fn complete(p: Str) -> Str "
          "= @py { return p }\n"
        + "service Answer { emission fn summarize(d: Str) -> Int }\n"
        + "component Summarizer provides out: Answer {\n"
        + "  route model on summarize { secret -> local }\n"
        + "  provide out {\n"
        + "    fn summarize(d) { return 0 }\n  }\n}\n")
    _refuses(src, code=SECRET_CODE)


def test_a_declared_endorse_still_downgrades_the_value():
    """The audited declassification is not closed off. An `endorse[confidential]`
    at a declared slot clears the origin, so the value that reaches the crossing
    is no longer confidential and the ceiling has nothing to place."""
    src = (
        _ROLES + _RECEIVER
        + "extern emission[model.complete] fn ask(p: Str) -> Int "
          "= @py { return 0 }\n"
        + "service Answer { emission endorse[confidential] fn summarize(d: Str)"
          " -> Int }\n"
        + "component Summarizer provides out: Answer {\n"
        + "  config { doc: Secret[Str] }\n"
        + "  route model on summarize { * -> cloud }\n"
        + "  provide out {\n"
        + "    fn summarize(d) {\n"
          "      let c = endorse[confidential](config.doc, reason = \"cleared\")\n"
          "      let r = ask(c)\n      return 0\n    }\n  }\n}\n")
    _admits(src)


# ==========================================================================
# 5. The reach survives a seam.
# ==========================================================================

def test_the_ceiling_follows_the_value_through_a_helper_fn():
    """The crossing is very often one hop in from the action that carries the
    route block. A helper `fn` declares no placement of its own, so without the
    signature reach the ceiling would stop at the seam and refuse nothing."""
    src = (
        _ROLES + _RECEIVER
        + "fn relay(p: Secret[Str]) -> Int { return prompt(p) }\n"
        + "service Answer { emission fn summarize(d: Str) -> Int }\n"
        + "component Summarizer provides out: Answer {\n"
        + "  config { doc: Secret[Str] }\n"
        + "  route model on summarize { * -> cloud }\n"
        + "  provide out {\n"
        + "    fn summarize(d) {\n      let r = relay(config.doc)\n"
          "      return 0\n    }\n  }\n}\n")
    err = _refuses(src)
    assert "confidential" in err.message


def test_the_helper_fn_seam_control_is_admitted_when_the_origin_is_placed():
    """The same program with the origin named and placed on the device. The
    pair is what shows the seam test measures the ceiling and not the seam."""
    src = (
        _ROLES + _RECEIVER
        + "fn relay(p: Secret[Str]) -> Int { return prompt(p) }\n"
        + "service Answer { emission fn summarize(d: Str) -> Int }\n"
        + "component Summarizer provides out: Answer {\n"
        + "  config { doc: Secret[Str] }\n"
        + "  route model on summarize { confidential -> local }\n"
        + "  provide out {\n"
        + "    fn summarize(d) {\n      let r = relay(config.doc)\n"
          "      return 0\n    }\n  }\n}\n")
    _admits(src)


def test_a_confidential_parameter_of_the_action_is_placed_too():
    """The value does not have to be a config field. A `Secret[T]` parameter of
    the routed operation itself carries the same origin and meets the same
    ceiling."""
    src = (
        _ROLES + _RECEIVER
        + "service Answer { emission fn summarize(d: Secret[Str]) -> Int }\n"
        + "component Summarizer provides out: Answer {\n"
        + "  route model on summarize { * -> cloud }\n"
        + "  provide out {\n"
        + "    fn summarize(d) {\n      let r = prompt(d)\n"
          "      return 0\n    }\n  }\n}\n")
    _refuses(src)


# ==========================================================================
# 6. Additivity, and the tie-back to the module.
# ==========================================================================

def test_an_admitted_program_is_byte_identical_to_one_with_no_route_block():
    """Item 512 contributes nothing to the IR and item 514 contributes nothing
    either: it only ever refuses. An admitted program's document must therefore
    equal the same program with the roles and the block deleted."""
    import json

    with_route = compile_source(_CONTROL, "ceiling.rvl")
    without = compile_source(_UNROUTED, "ceiling.rvl")
    assert json.dumps(with_route, sort_keys=True) == json.dumps(
        without, sort_keys=True)


def test_a_program_with_no_model_capability_is_untouched():
    """A component with a confidential value, a route block and no `model.*`
    crossing at all has nothing for the ceiling to fire at."""
    src = _program("  route model on summarize { * -> cloud }\n",
                   call="store", externs=_FS)
    _admits(src)


def test_the_module_constants_match():
    """The tie-back. This file spells its constants locally so it collects on a
    tree without the rule; this is what stops the two drifting apart."""
    from revl import model_route

    assert model_route.CODE == CODE
    assert model_route.MODEL_SCOPE == MODEL_SCOPE
    assert tuple(model_route.CONFIDENTIALITY_ORIGINS) == CONFIDENTIALITY_ORIGINS
    assert tuple(model_route.CEILING_ORIGINS) == CEILING_ORIGINS


def test_model_crossing_of_reads_the_declared_capability():
    """A crossing is a model call because of the capability the granting side
    declared, never because of its name. The `retention.persistence_sink_of`
    discipline, on this module's scope."""
    from revl import model_route

    assert model_route.model_crossing_of(["model.complete"]) == "model.complete"
    assert model_route.model_crossing_of(["fs.write"]) is None
    assert model_route.model_crossing_of([]) is None
    assert model_route.model_crossing_of(None) is None
    # `modelling.x` is not a `model` scope: the head is matched whole.
    assert model_route.model_crossing_of(["modelling.x"]) is None

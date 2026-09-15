"""The class-(c) approval gate as an intent refinement — item 470, stage 1.

`docs/design/470-intent-refinement.md` §4 stage 1: the gate compares an
operator's standing grant against a crossing with `_grant_covers` /
`_grant_within`, and routing that comparison through `intent.refine` is what
makes an AMOUNT-SCOPED approval expressible against a stated intent. This file
is that stage's coverage.

The hole it closes has one shape on both predicates: the grant is supposed to be
a DECLARATION held across time, and `_mint_grant` erased every ceiling parameter
out of the stored spelling — translating `calls=N` into `remainingUses` and
dropping `size=`/`time=` with nothing left behind. What the operator stated did
not outlive the mint, so both predicates keyed to it failed OPEN:

  * the FIND path admitted a crossing declaring any budget at all, including one
    the grant never bounded, because `cap_order.covers` reads a parameter bound
    only on the narrow side as "free on the wider side, so it only narrows" —
    true for a resource, backwards for a ceiling, where a bigger number is wider;
  * the REVOKE path could not match the very spelling the grant was minted with:
    `revoke_standing_grant(capability='model.complete(calls=3)')` compared the
    unerased spelling against the erased grant, matched nothing, and returned a
    typed `{"revoked": true, "count": 0}` while the grant went on auto-approving.

Both refusal directions are stated where they are tested. On the find path an
unproven crossing REFUSES, and refusing means it prompts for a single-use
approval — the gate never admits on a comparison it could not make. On the
revoke path the direction is the opposite one: a grant that is not retired keeps
auto-approving, so retiring too FEW is the fail-open direction, and the ceiling
dimension is read inclusively there (see `_grant_within`).

The end-to-end flow runs through the live cordis-py composition, exactly as
test_parameterized_grants.
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl import intent  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.mcp.approval import ApprovalRequired  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the approval gate is proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh`",
)

# Two `fs.write(path="/tmp")` crossings that differ ONLY in the budget they
# declare (one states none, one states `size="10MB"`), plus a bare
# `model.complete` for the metered-ceiling and revoke cases. The pair is the
# whole point: on the find path a grant must be comparable against what the
# crossing says it will spend, and a crossing that says nothing is not a
# crossing that spends nothing.
_SOURCE = (
    'extern emission[fs.write(path="/tmp")] fn wr_plain(sink: Str, msg: Str)'
    " = @py {\n    with open(sink, 'a') as f: f.write('plain:' + msg + '\\n')\n"
    "    return\n}\n"
    'extern emission[fs.write(path="/tmp", size="10MB")]'
    " fn wr_big(sink: Str, msg: Str)"
    " = @py {\n    with open(sink, 'a') as f: f.write('big:' + msg + '\\n')\n"
    "    return\n}\n"
    "extern emission[model.complete] fn complete(sink: Str, msg: Str)"
    " = @py {\n    with open(sink, 'a') as f: f.write('ml:' + msg + '\\n')\n"
    "    return\n}\n"
    "service Ops {\n"
    "  emission fn a_plain(sink: Str, msg: Str)\n"
    "  emission fn a_big(sink: Str, msg: Str)\n"
    "  emission fn a_model(sink: Str, msg: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn a_plain(sink, msg) { emit wr_plain(sink, msg) }\n"
    "    fn a_big(sink, msg) { emit wr_big(sink, msg) }\n"
    "    fn a_model(sink, msg) { emit complete(sink, msg) }\n"
    "  }\n"
    "}\n"
)

_BASE = compile_source(_SOURCE, "470_approval_gate.rvl")

_PLAIN = 'fs.write(path="/tmp")'
_BIG = 'fs.write(path="/tmp", size="10MB")'
_SMALL = 'fs.write(path="/tmp", size="1MB")'


def _session():
    session = Session()
    session.approval_policy = "auto"
    session.load(copy.deepcopy(_BASE), record=True)
    return session


def _lines(sink: str) -> list:
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


def _fires(session, op, sink) -> bool:
    """Whether the crossing was AUTO-APPROVED by a standing grant. A refusal
    here is `ApprovalRequired`: the gate falls back to a single-use prompt, which
    is what "refused" means on this path."""
    try:
        session.call("ops", op, [sink, "x"])
    except ApprovalRequired:
        return False
    return True


# ---------------------------------------------------------------------------
# The mint keeps the declaration it erases
# ---------------------------------------------------------------------------

@needs_cordis
def test_the_mint_keeps_the_ceiling_it_erases_from_the_valuation():
    """Erasure is a STORAGE decision, not a decision to forget. The stored
    valuation is still the resource-only cone (item 294's property, unchanged),
    and what the operator stated now survives beside it."""
    session = _session()
    session.mint_standing_grant(capability=_BIG, uses=3)
    entry = session._grants[-1]
    assert entry["capability"] == _PLAIN          # the cone, ceiling erased
    assert entry["declaredCeilings"] == {"size": 10485760}

    session.mint_standing_grant(capability=_PLAIN, uses=3)
    # a grant that stated no ceiling states none: the empty declaration, which
    # is the narrow reading and not a spelling of "unconstrained".
    assert session._grants[-1]["declaredCeilings"] == {}


# ---------------------------------------------------------------------------
# The find path: an amount-scoped grant, and the budget it never bounded
# ---------------------------------------------------------------------------

@needs_cordis
def test_an_amount_scoped_grant_covers_a_crossing_within_its_ceiling(sink):
    """NON-VACUITY, and the case stage 1 exists to make expressible: a grant
    stating `size="10MB"` auto-approves the crossing that declares `size="10MB"`.
    Passes on main too — the point is that the tightening below refuses because
    the amount does not refine, not because the dimension refuses everything."""
    session = _session()
    session.mint_standing_grant(capability=_BIG, uses=3)
    assert _fires(session, "a_big", sink)
    assert _lines(sink) == ["big:x"]
    assert session._grants_consumed == 1


@needs_cordis
def test_a_grant_below_the_crossings_budget_refuses_it(sink):
    """The amount-scoped refusal. A grant stating `size="1MB"` does not cover a
    crossing declaring `size="10MB"`: 10MB is not within 1MB.

    Direction: the crossing cannot be SHOWN to refine the grant, so it is
    refused, and refused means it prompts for a single-use approval. The gate
    never admits a spend it could not compare. On main this fired silently — the
    1MB the operator stated was dropped at mint and bounded nothing."""
    session = _session()
    session.mint_standing_grant(capability=_SMALL, uses=3)
    assert not _fires(session, "a_big", sink)
    assert _lines(sink) == []
    assert session._grants_consumed == 0


@needs_cordis
def test_a_grant_bounding_no_amount_refuses_a_crossing_that_states_one(sink):
    """A ceiling the grant does not state is not a free one. A grant on the bare
    `/tmp` cone does not cover the crossing that declares `size="10MB"`, because
    the grant bounded no quantity and so has authorized no spend.

    Direction: refused, hence prompted. This is the `covers` asymmetry the
    refinement replaces — `cap_order.covers` read the crossing's `size=` as a
    parameter free on the wider side and therefore narrowing, which is true of a
    resource and backwards for a ceiling, where a bigger number is wider."""
    session = _session()
    session.mint_standing_grant(capability=_PLAIN, uses=3)
    assert not _fires(session, "a_big", sink)
    assert session._grants_consumed == 0


@needs_cordis
def test_a_grant_bounding_an_amount_refuses_a_crossing_that_states_none(sink):
    """The other omission, refused in the other direction: a grant stating
    `size="10MB"` does not cover the crossing that declares no budget at all. An
    unstated spend is not a spend within the ceiling.

    Direction: refused, hence prompted. Unknown is not permitted — the asymmetry
    slice 1 of item 470 was built to hold."""
    session = _session()
    session.mint_standing_grant(capability=_BIG, uses=3)
    assert not _fires(session, "a_plain", sink)
    assert session._grants_consumed == 0


@needs_cordis
def test_a_resource_only_grant_is_unchanged(sink):
    """NON-VACUITY / additivity. A grant that states no ceiling still covers its
    whole resource cone exactly as `cap_order.covers` did: the refinement reduces
    to the object dimension when neither side states an amount. Passes on main
    and on the branch."""
    session = _session()
    session.mint_standing_grant(capability=_PLAIN, uses=3)
    assert _fires(session, "a_plain", sink)
    assert _fires(session, "a_plain", sink)
    assert _lines(sink) == ["plain:x", "plain:x"]
    assert session._grants_consumed == 2


@needs_cordis
def test_the_metered_ceiling_is_compared_by_the_counter_and_not_twice(sink):
    """NON-VACUITY for `calls`. It is METERED here: the mint translates it into
    `remainingUses` and `_consume_grant` spends it, so the counter IS its
    comparison and it is erased from both sides of the refinement. Feeding it to
    the ceiling dimension as well would compare one bound by two rules and let
    the weaker one win. Three crossings fire, the fourth prompts — item 294's
    shipped property, unchanged."""
    session = _session()
    grant = session.mint_standing_grant(capability="model.complete(calls=3)")
    assert grant["remainingUses"] == 3
    assert session._grants[-1]["capability"] == "model.complete"
    for _ in range(3):
        assert _fires(session, "a_model", sink)
    assert not _fires(session, "a_model", sink)
    assert session._grants_consumed == 3


# ---------------------------------------------------------------------------
# The refusal names the intent it violated
# ---------------------------------------------------------------------------

@needs_cordis
def test_the_refusal_names_the_declared_ceiling():
    """Item 470's exit criterion is a refusal that names THE INTENT IT VIOLATED,
    and a bare predicate carries none, so the finding is built first and the
    boolean derived from it. `_grant_refusal` is that finding."""
    session = _session()
    session.mint_standing_grant(capability=_SMALL, uses=3)
    grant = session._grants[-1]

    refusal = session._grant_refusal(grant, _BIG)
    assert refusal is not None
    assert refusal.violation is intent.Violation.CEILING
    assert refusal.dimension == "ceilings"
    assert refusal.declared == "size=1048576"
    assert refusal.requested == "size=10485760"
    assert "above the declared ceiling" in refusal.message

    # and it is None exactly when the predicate admits
    assert session._grant_refusal(grant, _PLAIN) is not None
    assert not session._grant_covers(grant, _BIG)


def test_an_uncomparable_spelling_is_refused_not_widened():
    """Fail-closed and additive: a spelling `cap_order` cannot parse matches only
    a byte-identical one and is never widened into a covering match. No
    composition needed — this is the predicate alone."""
    session = Session()
    grant = {"capability": "fs.write(path=", "declaredCeilings": {}}
    assert session._grant_covers(grant, "fs.write(path=")
    assert not session._grant_covers(grant, 'fs.write(path="/tmp")')
    refusal = session._grant_refusal(grant, 'fs.write(path="/tmp")')
    assert refusal is not None
    assert refusal.violation is intent.Violation.EXTRA_CAPABILITY


# ---------------------------------------------------------------------------
# The revoke path: an operator can retire by what they granted
# ---------------------------------------------------------------------------

@needs_cordis
def test_a_grant_is_revoked_by_the_spelling_it_was_minted_with():
    """The asymmetry the erasure left behind. `model.complete(calls=3)` mints a
    grant stored as bare `model.complete`; revoking with that IDENTICAL spelling
    compared the unerased text against the erased grant, matched nothing, and
    reported a typed `count: 0` — a clean no-op the operator reads as consent
    withdrawn while the grant keeps auto-approving.

    Direction: on this path retiring too FEW is the fail-open direction, so the
    revoke spelling's ceiling is compared against what the grant declared rather
    than against a parameter the stored cone can no longer bind."""
    session = _session()
    minted = session.mint_standing_grant(capability="model.complete(calls=3)")

    out = session.revoke_standing_grant(capability="model.complete(calls=3)")
    assert out["count"] == 1
    assert out["requestIds"] == [minted["requestId"]]
    entry = session._grants[-1]
    assert entry["revoked"] and entry["consumed"]


@needs_cordis
def test_a_revoke_by_the_stated_size_retires_the_grant_that_stated_it():
    """The same asymmetry on an UNMETERED ceiling, which has no `remainingUses`
    to fall back on: the `size="10MB"` a grant was minted with is the spelling an
    operator would reach for to retire it, and on main it retired nothing."""
    session = _session()
    minted = session.mint_standing_grant(capability=_BIG, uses=3)
    out = session.revoke_standing_grant(capability=_BIG)
    assert out["count"] == 1
    assert out["requestIds"] == [minted["requestId"]]


@needs_cordis
def test_a_bare_revoke_still_retires_a_grant_that_states_a_ceiling():
    """NON-VACUITY, and the control for the direction hazard. Revoking the bare
    cone retires its whole sub-cone, INCLUDING a grant that states a ceiling the
    revoke spelling does not mention. Reading that unstated ceiling as a bound —
    which is what `refine` does on the find path, correctly — would have made a
    bare revoke retire nothing the moment a grant stated an amount, which is the
    fail-open direction here. Passes on main and on the branch."""
    session = _session()
    a = session.mint_standing_grant(capability=_BIG, uses=3)
    b = session.mint_standing_grant(capability="model.complete(calls=3)")

    out = session.revoke_standing_grant(capability=_PLAIN)
    assert out["requestIds"] == [a["requestId"]]        # the fs.write cone only

    out = session.revoke_standing_grant(capability="model.complete")
    assert out["requestIds"] == [b["requestId"]]


@needs_cordis
def test_a_revoke_below_a_grants_stated_ceiling_leaves_it_alone():
    """A revoke names a SUB-CONE, and a grant stating a WIDER amount is not in
    it: revoking `size="1MB"` does not retire the `size="10MB"` grant. The
    revoke stays a narrowing operation on the ceiling dimension too, so an
    operator cannot retire more than they named by understating the amount."""
    session = _session()
    session.mint_standing_grant(capability=_BIG, uses=3)
    out = session.revoke_standing_grant(capability=_SMALL)
    assert out["count"] == 0
    assert not session._grants[-1]["revoked"]


@needs_cordis
def test_the_resource_subcone_revoke_is_unchanged():
    """NON-VACUITY / additivity for the revoke path: with no ceiling on either
    side the predicate is `covers` with the roles swapped, exactly as before —
    the sibling cone survives."""
    session = _session()
    session.mint_standing_grant(capability=_PLAIN, uses=1)
    out = session.revoke_standing_grant(capability="model.complete")
    assert out["count"] == 0
    out = session.revoke_standing_grant(capability=_PLAIN)
    assert out["count"] == 1


# ---------------------------------------------------------------------------
# The refusal reaches the prompt (issue #1098)
# ---------------------------------------------------------------------------
#
# The finding above stops one call short of the operator. `_grant_refusal`
# builds the whole `intent.Refusal` naming the declaration the crossing
# violated, `_grant_covers` reads it as a bool, and the text was dropped before
# anything rendered it: an operator whose standing grant was exceeded got an
# ordinary single-use prompt with nothing saying they had already granted
# something narrower and that this request falls outside it.
#
# `_find_standing_grant` now writes those findings onto the ticket it is about
# to raise (`standingGrantRefusals`, capability -> sentence), which is the same
# dict every surface hands the operator (`approval.two_step_payload`). These
# tests pin the TEXT on that ticket, so they fail if the refusal object is ever
# reduced back to a bare boolean at the coverage predicate.


def _prompt(session, op, sink) -> dict:
    """The ticket the gate raises when nothing standing covers the crossing —
    the prompt the operator reads. `_fires` is this same call read as a bool;
    this is the object it throws away, which is the shape of the bug."""
    with pytest.raises(ApprovalRequired) as caught:
        session.call("ops", op, [sink, "x"])
    return caught.value.ticket


@needs_cordis
def test_the_prompt_names_the_grant_and_how_the_crossing_exceeds_it(sink):
    """The exit criterion. An operator holding `fs.write(path="/tmp",
    size="1MB")` who is re-asked for a `size="10MB"` crossing reads, ON THE
    PROMPT, what they granted and how this request exceeds it.

    Fails on main: the ticket carries no such field, because the refusal was
    built and discarded one call earlier."""
    session = _session()
    session.mint_standing_grant(capability=_SMALL, uses=3)

    ticket = _prompt(session, "a_big", sink)
    notes = ticket["standingGrantRefusals"]
    assert list(notes) == ticket["classCCapabilities"]
    note = notes['fs.write(path="/tmp",size=10485760)']

    # what they granted — in the spelling they granted it in, ceiling included.
    # The STORED capability has the ceiling erased out of it, so naming that
    # would name a narrower declaration than the one the operator made.
    assert 'fs.write(path="/tmp",size=1048576)' in note
    assert "grant:1:" in note
    # how this request exceeds it
    assert 'fs.write(path="/tmp",size=10485760)' in note
    assert "above the declared ceiling `size=1048576`" in note
    assert "Reduce the spend or widen the declared ceiling" in note
    assert _lines(sink) == []            # still refused, still nothing fired


@needs_cordis
def test_the_prompt_carries_the_refusal_object_itself_not_a_restatement(sink):
    """The anti-regression the exit criterion names: this fails if
    `_grant_covers` goes back to returning a bare bool.

    The sentence on the prompt CONTAINS `str(refusal)` — the `intent.Refusal`
    that `_grant_refusal` builds, rendered in `errors.RevlError`'s
    message-plus-hint shape. A predicate that decided coverage without building
    that object would have nothing to put here, and a hand-written restatement
    beside the kernel would drift from it. So the assertion is identity of text
    with the kernel's own finding, not a keyword match."""
    session = _session()
    session.mint_standing_grant(capability=_SMALL, uses=3)
    grant = session._grants[-1]

    refusal = session._grant_refusal(grant, _BIG)
    assert isinstance(refusal, intent.Refusal)   # not a bool

    ticket = _prompt(session, "a_big", sink)
    assert str(refusal) in ticket["standingGrantRefusals"][
        'fs.write(path="/tmp",size=10485760)']


@needs_cordis
def test_the_unstated_ceiling_refusal_reaches_the_prompt_too(sink):
    """The other refusal direction, and the one an operator is least likely to
    work out unaided: the grant bounded no quantity, so it has authorized no
    spend, and the crossing declaring `size="10MB"` is outside it. The prompt
    says which dimension, not just that the answer was no."""
    session = _session()
    session.mint_standing_grant(capability=_PLAIN, uses=3)

    note = _prompt(session, "a_big", sink)["standingGrantRefusals"][
        'fs.write(path="/tmp",size=10485760)']
    assert 'you hold a standing grant for `fs.write(path="/tmp")`' in note
    assert "a ceiling the declared intent does not state" in note


@needs_cordis
def test_a_prompt_with_no_grant_behind_it_carries_no_refusal(sink):
    """CONTROL, passes on main and on the branch. The field is not a fixture of
    every class-(c) prompt: an operator who granted NOTHING is not told they
    exceeded something, so the sentence stays a fact about their own grants."""
    session = _session()
    ticket = _prompt(session, "a_big", sink)
    assert "standingGrantRefusals" not in ticket


@needs_cordis
def test_a_grant_whose_uses_ran_out_states_no_coverage_refusal(sink):
    """CONTROL for HONESTY, passes on main and on the branch. `remainingUses`
    exhaustion is a liveness fact, not a coverage one: the grant did cover this
    crossing and is simply spent. Saying "this crossing is not within your
    grant" there would be false, so only grants that are live on every other
    axis and fail on coverage alone contribute a sentence (`_live_grant_for`)."""
    session = _session()
    session.mint_standing_grant(capability="model.complete(calls=1)")
    assert _fires(session, "a_model", sink)
    assert "standingGrantRefusals" not in _prompt(session, "a_model", sink)


@needs_cordis
def test_the_refusal_field_does_not_move_the_ticket_hash(sink):
    """ADDITIVITY. The ticket hash is the outstanding-ticket key and the ledger
    binding, so the explanation lands AFTER `build_ticket` computed it — the
    same discipline `resourceScopeRefusals` follows. The identical crossing
    hashes the same whether or not an exceeded grant put a sentence on it."""
    bare = _prompt(_session(), "a_big", sink)

    session = _session()
    session.mint_standing_grant(capability=_SMALL, uses=3)
    explained = _prompt(session, "a_big", sink)

    assert "standingGrantRefusals" in explained
    assert explained["hash"] == bare["hash"]


@needs_cordis
def test_a_covering_grant_still_auto_approves_and_explains_nothing(sink):
    """NON-VACUITY for the admit path. Nothing about the DECISION moved: the
    covered crossing fires, spends its use, and raises no ticket at all, so
    there is no prompt for a refusal to reach."""
    session = _session()
    session.mint_standing_grant(capability=_BIG, uses=3)
    assert _fires(session, "a_big", sink)
    assert _lines(sink) == ["big:x"]
    assert session._grants_consumed == 1


def test_the_granted_spelling_is_rendered_from_the_kept_declaration():
    """`_grant_spelling` alone — no composition needed. The mint erases the
    ceiling out of the stored valuation and keeps it on `declaredCeilings`, and
    the prompt has to put the two back together to name what the operator
    stated. An unparseable spelling falls back to the stored text rather than
    raising, the same additive fallback both coverage predicates keep."""
    session = Session()
    assert session._grant_spelling(
        {"capability": _PLAIN, "declaredCeilings": {"size": 1048576}}
    ) == 'fs.write(path="/tmp",size=1048576)'
    assert session._grant_spelling(
        {"capability": "model.complete", "declaredCeilings": {"calls": 3}}
    ) == "model.complete(calls=3)"
    # no declaration kept: the stored cone is what was stated
    assert session._grant_spelling(
        {"capability": _PLAIN, "declaredCeilings": {}}) == _PLAIN
    assert session._grant_spelling({"capability": _PLAIN}) == _PLAIN
    # unparseable, and a token that cannot carry parameters at all
    assert session._grant_spelling(
        {"capability": "fs.write(path=",
         "declaredCeilings": {"size": 1}}) == "fs.write(path="
    assert session._grant_spelling(
        {"capability": "*", "declaredCeilings": {"calls": 2}}) == "*"

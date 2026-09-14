"""The WINDOW, the SAMPLE FLOOR and the DENOMINATOR — roadmap item 473, issue
#825, slice 3 of `docs/design/473-slo-contracts.md`.

The design note states the gap this file closes in one sentence:

    An SLO is a statistical claim over a window, and the landed surface has no
    window. `p95_latency: 250ms` with nothing else is not falsifiable in either
    direction: one slow call in a year breaches it, and so does every call.

So an entry grows three optional qualifiers, and they are written beside the
target in the SOURCE rather than passed as a runtime flag, because a target and
the window it is measured over are one promise and splitting them lets an
operator quietly change what the author promised:

    slo {
      p95_latency: 250ms over 20s min 21,
      success_rate: 99.5 over 1h min 10 of activations
    }

What this file pins:

  * the SURFACE — parsed per datum, order-free, each qualifier at most once,
    every domain refusal a target that could never hold; `of` accepted only on
    a rate, and only naming a member of a closed denominator registry;
  * the IR — a third conditional key, `slo_window`, with only the members the
    datum wrote, so a block that declares no qualifier emits the pre-slice
    document byte for byte;
  * the MEASUREMENT — a declared window genuinely narrows the population a
    verdict is taken over (the load-bearing test: the same run BREACHES over
    its whole population and HOLDS over the declared window), a declared floor
    decides when it is tighter than the structural one, and `success_rate`
    becomes measurable over a denominator the author DECLARED;
  * the HONEST REFUSALS — a window over a population that carries no timestamp
    is `insufficient` naming the record that would have to be stamped, never a
    whole-run verdict wearing a window's name; and a denominator this tree
    counts without an outcome to divide by it is `unmeasurable` naming the
    missing numerator rather than substituting the population that does have
    one;
  * the RECEIPT — the declared window is inside the signed body, and every
    top-level member of a freshly signed receipt is covered by its signature.

The NON-VACUITY controls run throughout and each of them passes both before and
after this slice: a block with no qualifier produces the IR and the verdicts it
produced before, a rate with no `of` reads exactly as it read before, and a
compliant generation is sealed with a verifiable receipt, keeps serving and
writes no latch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import slo  # noqa: E402
from revl.composition import resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import (SLO_DENOMINATORS,  # noqa: E402
                         SLO_TAKES_DENOMINATOR, Parser)

KEY = b"an-slo-signing-key-for-the-window-slice"

BILLING = """
service Billing {
  emission[net(time="1s", requests=100)] fn charge(account: Str, amount: Int) -> Bool
}

component BillingSvc provides billing: Billing {
  provide billing {
    fn charge(account, amount) = true
  }
}
"""

CONSUMER = """
service Checkout {
  emission fn pay(account: Str) -> Bool
}

component CheckoutSvc requires billing: Billing provides checkout: Checkout {
  provide checkout {
    fn pay(account) = emit billing.charge(account, 100)
  }
}
"""


def project(tmp_path: Path, slo_block: str) -> Path:
    """A two-row composition whose `slo` clause is `slo_block` verbatim."""
    (tmp_path / "billing.rvl").write_text(BILLING)
    (tmp_path / "consumer.rvl").write_text(CONSUMER)
    doc = tmp_path / "base.rvl"
    doc.write_text(f"""
composition base {{
  {slo_block}
  row @checkout from "consumer.rvl" provides checkout
  row @billing from "billing.rvl" provides billing
}}
""")
    return doc


def parse_composition(body: str):
    return Parser(f"composition c {{ {body} }}", "t.rvl").parse().compositions[0]


def hop(ts: float, seconds: float, component: str = "CheckoutSvc") -> dict:
    """One recorded, revl-measured model hop at trace time `ts`."""
    return {"event": "emit", "component": component, "gen": 1, "ts": float(ts),
            "llm": {"latencyProvenance": "revl-measured-bracket",
                    "latencySeconds": seconds}}


def decisions(n: int, seconds: float) -> list:
    """`n` durable `model-decision` records — the per-call population, which
    carries NO timestamp: stamping it is a durable-format change under
    `WAL_VERSION` and this slice does not make one."""
    return [{"record": "model-decision", "component": "CheckoutSvc",
             "stepIndex": i, "outcome": "validated",
             "llm": {"latencyProvenance": "revl-measured-bracket",
                     "latencySeconds": seconds}}
            for i in range(n)]


def lifecycle(component: str, gen: int, start: float, end: float, *,
              failed: bool = False) -> list:
    """One closed activation: the load/withdraw pair `metrics._lifecycles`
    pairs, with the transition `metrics._is_failed` reads."""
    return [
        {"event": "load", "component": component, "gen": gen, "ts": start},
        {"event": "withdraw", "component": component, "gen": gen, "ts": end,
         "transition": "ACTIVE -> FAILED" if failed else "ACTIVE -> DISPOSED"},
    ]


def activations(total: int, failed: int, *, step: float = 1.0) -> list:
    """`total` closed activations, the LAST `failed` of them failed, one per
    `step` of trace time so a window can select a suffix of them."""
    events: list = []
    for index in range(total):
        events += lifecycle(f"C{index}", 1, index * step, index * step + 0.1,
                            failed=index >= total - failed)
    return events


# ------------------------------------------------------------- the surface

def test_the_window_the_floor_and_the_denominator_parse_per_datum():
    """Three optional qualifiers, per entry, and only the ones the datum wrote.
    The window is canonicalized to milliseconds at the surface, the same place
    the target is, so neither can be re-read in another unit downstream."""
    decl = parse_composition(
        'slo { p95_latency: 250ms over 5m min 200, '
        'success_rate: 99.5 over 1h min 1000 of activations, '
        'max_pending_tasks: 100 }')
    assert decl.slo_windows == {
        "p95_latency": {"over": 300000, "min": 200},
        "success_rate": {"over": 3600000, "min": 1000, "of": "activations"},
    }
    # the datum that wrote none is absent, not present-and-empty
    assert "max_pending_tasks" not in decl.slo_windows


def test_the_qualifiers_are_order_free_and_compose_with_on_breach():
    """`on breach` still parses after them, in either qualifier order, so the
    two surfaces do not have to be written in a memorised sequence."""
    decl = parse_composition(
        'slo { p95_latency: 250ms min 50 over 30s on breach halt }')
    assert decl.slo_windows == {"p95_latency": {"min": 50, "over": 30000}}
    assert decl.slo_responses == {"p95_latency": ("halt", None)}


@pytest.mark.parametrize("body, needle", [
    ("slo { p95_latency: 250ms over 5m over 10m }", "duplicate `over`"),
    ("slo { p95_latency: 250ms min 10 min 20 }", "duplicate `min`"),
    ("slo { success_rate: 99.5 of activations of crossings }",
     "duplicate `of`"),
])
def test_a_repeated_qualifier_is_refused(body, needle):
    """A second qualifier either repeats the first or contradicts it, and
    neither is a contract — the same rule the duplicate datum already gets."""
    with pytest.raises(RevlError) as excinfo:
        parse_composition(body)
    assert needle in str(excinfo.value)


@pytest.mark.parametrize("body, needle", [
    ("slo { p95_latency: 250ms over 0s }", "positive window"),
    ("slo { p95_latency: 250ms min 0 }", "positive sample count"),
])
def test_a_qualifier_that_could_never_hold_is_refused(body, needle):
    """Not a style opinion in either case: a window of zero contains no
    observation, and a floor of zero admits a verdict over an empty sample —
    which is the number the floor exists to stop."""
    with pytest.raises(RevlError) as excinfo:
        parse_composition(body)
    assert needle in str(excinfo.value)


def test_the_denominator_registry_is_closed():
    """A population nothing counts is a rate with no measurement behind it, so
    the refusal lists the registry rather than accepting the word."""
    with pytest.raises(RevlError) as excinfo:
        parse_composition("slo { success_rate: 99.5 of requests }")
    message = str(excinfo.value)
    assert "unknown SLO denominator" in message
    for name in SLO_DENOMINATORS:
        assert f"`{name}`" in message


def test_only_a_rate_takes_a_denominator():
    """A percentile's population is fixed by the measurement that produces it,
    so a second one named on the entry would be a meaning nothing reads — the
    same argument that keeps the comparator out of the surface."""
    assert SLO_TAKES_DENOMINATOR == ("success_rate",)
    with pytest.raises(RevlError) as excinfo:
        parse_composition("slo { p95_latency: 250ms of activations }")
    assert "takes no `of` denominator" in str(excinfo.value)


# ------------------------------------------------------------------- the IR

def test_the_window_rides_its_own_conditional_ir_key(tmp_path):
    """A third conditional key beside `slo` and `slo_on_breach`, carrying only
    the members the datum wrote and spelling the window's unit in its name."""
    doc = project(tmp_path, 'slo { p95_latency: 2s over 30s min 50, '
                            'success_rate: 99.5 of activations }')
    table = resolve_file(str(doc), str(tmp_path))
    ir = table.to_ir()
    assert ir["slo_window"] == {
        "p95_latency_ms": {"overMs": 30000, "minSamples": 50},
        "success_rate_pct": {"of": "activations"},
    }
    # and the reader-facing contract carries it on the SAME entry as the target
    contract = table.slo_contract()
    assert contract["p95_latency_ms"]["window"] == {
        "overMs": 30000, "minSamples": 50}
    assert contract["success_rate_pct"]["window"] == {"of": "activations"}


def test_a_block_with_no_qualifier_emits_the_pre_slice_ir_byte_for_byte(
        tmp_path):
    """THE ADDITIVITY PIN, and a NON-VACUITY control: it passes both before and
    after this slice. A `slo` block that writes no qualifier must produce
    exactly the document it produced before they existed — the `slo` key as
    slice 1 wrote it, and NO `slo_window` key at all."""
    doc = project(tmp_path, "slo { p95_latency: 2s, max_pending_tasks: 100 }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    assert "slo_window" not in ir
    assert json.dumps(ir["slo"], sort_keys=True) == json.dumps(
        {"p95_latency_ms": 2000, "max_pending_tasks": 100}, sort_keys=True)

    (tmp_path / "plain").mkdir()
    plain = project(tmp_path / "plain", "")
    plain_ir = resolve_file(str(plain), str(tmp_path / "plain")).to_ir()
    assert "slo" not in plain_ir and "slo_window" not in plain_ir
    assert {k: v for k, v in ir.items() if k != "slo"} == dict(plain_ir)


def test_contract_from_ir_reads_the_window_onto_the_entry_the_target_is_on():
    """One entry, because the target and its window are one promise. A
    contract document written before this slice resolves with no `window`
    member at all, which every measurement reads as the pre-slice population."""
    contract = slo.contract_from_ir({
        "slo": {"p95_latency_ms": 250, "success_rate_pct": 99.5},
        "slo_window": {"p95_latency_ms": {"overMs": 20000, "minSamples": 21}}})
    assert slo.qualifiers(contract["p95_latency_ms"]) == (20000.0, 21, None)
    assert slo.qualifiers(contract["success_rate_pct"]) == (None, None, None)
    assert "window" not in contract["success_rate_pct"]


# ---------------------------------------------------- the window, measured

#: The run both window tests are taken over: forty revl-measured completions at
#: one-second trace intervals, the first twenty slow (5s) and the last twenty
#: fast (50ms). Over the WHOLE population the p95 selects a slow sample; over
#: the declared twenty-second window it selects a fast one. Same run, same
#: target, two different and both honest answers — which is exactly why the
#: window has to be declared and signed rather than assumed.
SLOW_THEN_FAST = ([hop(i, 5.0) for i in range(20)]
                  + [hop(20 + i, 0.05) for i in range(20)])


def test_without_a_window_a_verdict_is_over_the_whole_generation():
    """The pre-slice reading, kept exactly: this is the control the next test
    is measured against, and it passes both before and after this slice."""
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 250}})
    verdict = slo.measure(contract, slo.observations(SLOW_THEN_FAST))
    assert verdict["p95_latency_ms"]["verdict"] == slo.BREACHED
    assert verdict["p95_latency_ms"]["samples"] == 40
    assert "window" not in verdict["p95_latency_ms"]


def test_a_declared_window_narrows_the_population_the_verdict_is_over():
    """THE LOAD-BEARING TEST of this slice. The same forty completions, the
    same 250ms target: over the whole run the p95 is a slow sample and the
    contract is breached; over the twenty seconds the document declared it is a
    fast one and the contract holds. Before this slice the declared window was
    unspellable and unread, so the second answer did not exist."""
    contract = slo.contract_from_ir({
        "slo": {"p95_latency_ms": 250},
        "slo_window": {"p95_latency_ms": {"overMs": 20000, "minSamples": 21}}})
    verdict = slo.measure(contract, slo.observations(SLOW_THEN_FAST))[
        "p95_latency_ms"]
    assert verdict["verdict"] == slo.HOLDING
    # the window selected the last 21 of 40 samples, and says so
    assert verdict["samples"] == 21
    assert verdict["observed"] == pytest.approx(50.0)
    assert verdict["window"] == {"overMs": 20000.0, "minSamples": 21}


def test_a_window_that_leaves_too_few_samples_is_insufficient_not_a_pass():
    """Narrowing the population is not a way to launder a verdict: once the
    window has been applied the floor still has to be met, and below it the
    answer is `insufficient` — which `satisfied()` reads as not held."""
    contract = slo.contract_from_ir({
        "slo": {"p95_latency_ms": 250},
        "slo_window": {"p95_latency_ms": {"overMs": 5000}}})
    verdict = slo.measure(contract, slo.observations(SLOW_THEN_FAST))[
        "p95_latency_ms"]
    assert verdict["verdict"] == slo.INSUFFICIENT
    assert not slo.satisfied(verdict)
    assert str(slo.PERCENTILE_MIN_SAMPLES) in verdict["reason"]


def test_a_declared_floor_tighter_than_the_structural_one_decides():
    """`min` is the author's promise about how small a sample they are willing
    to be judged on. Both floors apply and the tighter one decides, so neither
    the author nor the compiler can quietly weaken the other."""
    events = [hop(i, 0.05) for i in range(25)]
    plain = slo.contract_from_ir({"slo": {"p95_latency_ms": 250}})
    assert slo.measure(plain, slo.observations(events))[
        "p95_latency_ms"]["verdict"] == slo.HOLDING

    strict = slo.contract_from_ir({
        "slo": {"p95_latency_ms": 250},
        "slo_window": {"p95_latency_ms": {"minSamples": 40}}})
    verdict = slo.measure(strict, slo.observations(events))["p95_latency_ms"]
    assert verdict["verdict"] == slo.INSUFFICIENT
    assert slo.FLOOR_DECLARED in verdict["reason"]
    assert verdict["window"] == {"minSamples": 40}


def test_a_window_over_a_population_with_no_timestamp_is_refused_honestly():
    """The honest refusal. The per-call population is the run's own
    `model-decision` records, and that record carries no timestamp — stamping
    it is a durable-format change under `WAL_VERSION` which this slice does not
    make. So a declared window over it is `insufficient` NAMING the record,
    never a whole-population verdict wearing the window's name."""
    contract = slo.contract_from_ir({
        "slo": {"p95_latency_ms": 250},
        "slo_window": {"p95_latency_ms": {"overMs": 20000}}})
    obs = slo.observations([], decisions(30, 5.0))
    assert obs["latencySource"] == slo.LATENCY_FROM_WAL
    verdict = slo.measure(contract, obs)["p95_latency_ms"]
    assert verdict["verdict"] == slo.INSUFFICIENT
    assert not slo.satisfied(verdict)
    assert "model-decision" in verdict["reason"]
    assert "WAL_VERSION" in verdict["reason"]
    # and without the window the same population still measures, unchanged
    plain = slo.contract_from_ir({"slo": {"p95_latency_ms": 250}})
    assert slo.measure(plain, obs)["p95_latency_ms"]["verdict"] == slo.BREACHED


# ------------------------------------------------------- the denominator

def test_a_rate_with_no_declared_denominator_reads_exactly_as_before():
    """NON-VACUITY control, passing both before and after this slice. A rate
    whose population nobody named is unmeasurable for the reason it always was,
    and the reason is unchanged — inventing a denominator is precisely what
    `docs/design/473-slo-contracts.md` forbids."""
    contract = slo.contract_from_ir({"slo": {"success_rate_pct": 99.5}})
    verdict = slo.measure(contract, slo.observations(activations(10, 1)))[
        "success_rate_pct"]
    assert verdict["verdict"] == slo.UNMEASURABLE
    assert "declares none" in verdict["reason"]
    assert not slo.satisfied(verdict)


def test_a_rate_is_measured_over_the_denominator_the_document_declared():
    """What `of` buys. The population is the lifecycles the trace closed and
    the outcome is the transition each withdraw settled into — the same two
    numbers `revl metrics` already reports, divided rather than re-derived."""
    obs = slo.observations(activations(10, 1))
    assert len(obs["activations"]) == 10

    breaching = slo.contract_from_ir({
        "slo": {"success_rate_pct": 99.5},
        "slo_window": {"success_rate_pct": {"of": "activations"}}})
    verdict = slo.measure(breaching, obs)["success_rate_pct"]
    assert verdict["verdict"] == slo.BREACHED
    assert verdict["observed"] == pytest.approx(90.0)
    assert verdict["samples"] == 10 and verdict["failed"] == 1
    assert verdict["measurement"] == "of:activations"

    holding = slo.contract_from_ir({
        "slo": {"success_rate_pct": 85.0},
        "slo_window": {"success_rate_pct": {"of": "activations"}}})
    assert slo.measure(holding, obs)["success_rate_pct"]["verdict"] == (
        slo.HOLDING)


def test_a_rate_is_a_lower_bound_even_once_it_is_measurable():
    """The direction is the datum's, and making the rate measurable does not
    move it: 100 percent of ten clean activations holds a 99.5 target."""
    contract = slo.contract_from_ir({
        "slo": {"success_rate_pct": 99.5},
        "slo_window": {"success_rate_pct": {"of": "activations"}}})
    verdict = slo.measure(contract, slo.observations(activations(10, 0)))[
        "success_rate_pct"]
    assert verdict["verdict"] == slo.HOLDING
    assert verdict["observed"] == pytest.approx(100.0)


def test_a_window_narrows_the_rate_s_population_too():
    """The window is a property of the entry, not of one datum's measurement,
    so it selects the rate's population the same way it selects the
    percentile's. Ten activations one second apart, the last two failed: over
    the whole run the rate is 80 percent, over the last four it is 50."""
    obs = slo.observations(activations(10, 2))
    whole = slo.contract_from_ir({
        "slo": {"success_rate_pct": 60.0},
        "slo_window": {"success_rate_pct": {"of": "activations"}}})
    assert slo.measure(whole, obs)["success_rate_pct"]["observed"] == (
        pytest.approx(80.0))

    windowed = slo.contract_from_ir({
        "slo": {"success_rate_pct": 60.0},
        "slo_window": {"success_rate_pct": {"of": "activations",
                                            "overMs": 3000}}})
    verdict = slo.measure(windowed, obs)["success_rate_pct"]
    assert verdict["samples"] == 4
    assert verdict["observed"] == pytest.approx(50.0)
    assert verdict["verdict"] == slo.BREACHED


def test_a_denominator_with_no_numerator_names_the_numerator_it_lacks():
    """The second honest refusal. `crossings` IS counted — every recorded
    `emit` — and the runtime still records no outcome for one, so the rate has
    a population and nothing to divide by it. Substituting the population that
    does have an outcome would answer a question the author did not ask, so the
    verdict is `unmeasurable` and the reason names the missing half."""
    contract = slo.contract_from_ir({
        "slo": {"success_rate_pct": 99.5},
        "slo_window": {"success_rate_pct": {"of": "crossings"}}})
    verdict = slo.measure(contract, slo.observations(
        activations(10, 1) + [hop(1.0, 0.05)]))["success_rate_pct"]
    assert verdict["verdict"] == slo.UNMEASURABLE
    assert "numerator" in verdict["reason"]
    assert not slo.satisfied(verdict)
    assert SLO_DENOMINATORS["crossings"] is False


def test_an_open_activation_is_in_neither_the_numerator_nor_the_denominator():
    """A lifecycle the trace never closed has no outcome yet, so counting it as
    a success would inflate the numerator and counting it as a failure would
    invent one. It is simply not in the population."""
    events = activations(4, 0) + [
        {"event": "load", "component": "Cx", "gen": 1, "ts": 9.0}]
    obs = slo.observations(events)
    assert len(obs["activations"]) == 4
    contract = slo.contract_from_ir({
        "slo": {"success_rate_pct": 99.5},
        "slo_window": {"success_rate_pct": {"of": "activations"}}})
    assert slo.measure(contract, obs)["success_rate_pct"]["samples"] == 4


def test_a_rate_over_an_empty_population_is_insufficient_not_perfect():
    """Zero successes out of zero activations is not a hundred percent. The
    floor is at least one member, so the answer is `insufficient`."""
    contract = slo.contract_from_ir({
        "slo": {"success_rate_pct": 99.5},
        "slo_window": {"success_rate_pct": {"of": "activations"}}})
    verdict = slo.measure(contract, slo.observations([]))["success_rate_pct"]
    assert verdict["verdict"] == slo.INSUFFICIENT
    assert not slo.satisfied(verdict)


# ----------------------------------------------------------- the receipt

def _receipt(contract, verdicts, *, key=KEY):
    body = slo.build_body(composition="base.rvl", generation=1,
                          contract=contract, verdicts=verdicts,
                          action=slo.PAUSE,
                          issued_at="2026-01-01T00:00:00+00:00")
    return slo.make_receipt(body, key)


def test_the_declared_window_is_inside_the_signed_body():
    """A receipt that signed the target and not the window would attest to half
    the promise, and a reader comparing two generations could not tell a
    widened window from a held objective."""
    contract = slo.contract_from_ir({
        "slo": {"p95_latency_ms": 250},
        "slo_window": {"p95_latency_ms": {"overMs": 20000, "minSamples": 21}}})
    verdicts = slo.measure(contract, slo.observations(SLOW_THEN_FAST))
    receipt = _receipt(contract, verdicts)
    assert receipt["contract"]["p95_latency_ms"]["window"] == {
        "overMs": 20000.0, "minSamples": 21}
    assert slo.verify_receipt(receipt, KEY) == (True, "")
    # a contract with no window carries no `window` member at all
    plain = slo.contract_from_ir({"slo": {"p95_latency_ms": 250}})
    bare = _receipt(plain, slo.measure(plain, slo.observations(SLOW_THEN_FAST)))
    assert "window" not in bare["contract"]["p95_latency_ms"]
    assert slo.verify_receipt(bare, KEY) == (True, "")


def test_every_top_level_member_of_a_fresh_receipt_is_covered_by_its_signature():
    """The signing discipline, pinned rather than assumed. A member added to
    the body AFTER the MAC is a member the verifier covers and the signer did
    not, which makes nothing verify; a member the signer covered and the
    verifier does not would make an edit invisible. So: a freshly signed
    receipt verifies, and DROPPING or EDITING any top-level member of it
    breaks the signature."""
    contract = slo.contract_from_ir({
        "slo": {"p95_latency_ms": 250},
        "slo_window": {"p95_latency_ms": {"overMs": 20000, "minSamples": 21}}})
    receipt = _receipt(contract, slo.measure(
        contract, slo.observations(SLOW_THEN_FAST)))
    assert slo.verify_receipt(receipt, KEY) == (True, "")

    members = [k for k in receipt if k != slo.SIGNATURE_FIELD]
    assert "keyId" in members and "contract" in members
    for member in members:
        dropped = {k: v for k, v in receipt.items() if k != member}
        ok, reason = slo.verify_receipt(dropped, KEY)
        assert ok is False, f"dropping {member} left the receipt verifying"
        assert reason

        edited = dict(receipt)
        edited[member] = ([*receipt[member], "x"]
                          if isinstance(receipt[member], list)
                          else {**receipt[member], "revl.tamper": 1}
                          if isinstance(receipt[member], dict)
                          else "revl.tamper")
        ok, reason = slo.verify_receipt(edited, KEY)
        assert ok is False, f"editing {member} left the receipt verifying"
        assert reason


# ---------------------------------------------------- the non-vacuity control

def test_a_compliant_generation_is_sealed_keeps_serving_and_writes_no_latch(
        tmp_path):
    """THE CONTROL, and it passes both before and after this slice. A
    generation that holds its contract is sealed with a verifiable receipt, no
    action is dispatched, and NO latch is written — so nothing in this slice
    turned a measurement into a stop."""
    latch = tmp_path / "session.estop"
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 2000}})
    monitor = slo.Monitor(contract, composition="base.rvl", generation=1,
                          latch=str(latch), key=KEY,
                          decisions=decisions(30, 0.2))
    verdict = monitor.seal()
    assert verdict["breached"] == []
    assert verdict["verdicts"]["p95_latency_ms"]["verdict"] == slo.HOLDING
    assert slo.verify_receipt(monitor.receipt, KEY) == (True, "")
    assert not latch.exists()
    assert slo.latch_state(str(latch)) is None


def test_a_windowed_compliant_generation_is_sealed_and_still_writes_no_latch(
        tmp_path):
    """The same control with the slice's own surface in force: declaring a
    window does not arm anything on a generation that holds."""
    latch = tmp_path / "session.estop"
    contract = slo.contract_from_ir({
        "slo": {"p95_latency_ms": 250},
        "slo_window": {"p95_latency_ms": {"overMs": 20000, "minSamples": 21}}})
    monitor = slo.Monitor(contract, composition="base.rvl", generation=1,
                          latch=str(latch), key=KEY, events=SLOW_THEN_FAST)
    verdict = monitor.seal()
    assert verdict["breached"] == []
    assert verdict["verdicts"]["p95_latency_ms"]["verdict"] == slo.HOLDING
    assert slo.verify_receipt(monitor.receipt, KEY) == (True, "")
    assert not latch.exists()


def test_the_panel_prints_the_window_beside_the_target(tmp_path, capsys):
    """The operator reads the promise whole. A datum that declared no
    qualifier prints exactly the line it printed before this slice."""
    from revl.__main__ import main

    doc = project(tmp_path, 'slo { p95_latency: 2s over 30s min 50, '
                            'max_pending_tasks: 100 }')
    assert main(["composition", str(doc), "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "p95_latency_ms" in out
    assert "over 30000ms min 50" in out
    # the datum that declared none is unchanged: the bare target, then the
    # response, exactly the line this panel printed before slice 3
    assert "max_pending_tasks        100   on breach pause (default)" in out

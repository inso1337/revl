"""The OBSERVED half of the SLO contract — roadmap item 473, issue #825.

Slice 1 (`tests/test_473_slo_rollout_gate.py`) landed the DECLARED half: the
`slo { p95_latency: 250ms }` block, its closed datum registry, and the
compile-time `G4` refusal of a composition whose own `emission[...]` ceilings
contradict its objectives. That gate is a consistency check between two
statements one document makes, and `docs/design/473-slo-contracts.md` is
explicit that it is the weakest of the four kinds of evidence a rollout could
rest on.

This file pins the second clause of the item's exit criterion — "a live breach
triggers the declared fallback or pause with an SLO receipt on the generation"
— plus the rollout gate that reads such a receipt back:

  * the `on breach <response>` surface, parsed, carried and lowered;
  * the MEASUREMENT: observed value against declared target, using the datum's
    own direction (a duration is an upper bound, a rate a lower one), in the
    target's own unit;
  * the RESPONSE: the declared action dispatched, with the latch it writes;
  * the RECEIPT: signed, verified fail-closed, attached to its generation;
  * the ROLLOUT GATE: a candidate that still declares an objective the
    predecessor generation measurably breached is REFUSED BY NAME, and one that
    does not is admitted (the non-vacuity control);
  * IR ADDITIVITY: a `slo` block declaring targets and no responses produces
    byte-identically the IR it produced before this slice.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import slo  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.composition import resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import (SLO_DEFAULT_RESPONSE, SLO_DIRECTION,  # noqa: E402
                         SLO_IR_KEYS, SLO_RESPONSES, Parser, slo_datum)

KEY = b"an-slo-signing-key-for-the-tests"

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

#: A second provider of the same key, in the same realm, so a divert to it
#: admits through the ordinary G2/G3 gate rather than being waved through.
STANDBY = """
service Billing {
  emission[net(time="1s", requests=100)] fn charge(account: Str, amount: Int) -> Bool
}

component StandbyBilling provides billing: Billing {
  provide billing {
    fn charge(account, amount) = false
  }
}
"""


def project(tmp_path: Path, slo_block: str, **extra: str) -> Path:
    """A two-row composition whose `slo` clause is `slo_block` verbatim."""
    for name, text in {"billing": BILLING, "consumer": CONSUMER,
                       **extra}.items():
        (tmp_path / f"{name}.rvl").write_text(text)
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


def hops(n: int, seconds: float, component: str = "CheckoutSvc") -> list[dict]:
    """`n` recorded model hops, each measured by revl itself. The provenance
    tag is what makes them measurable: a host-reported number is skipped."""
    return [{"event": "emit", "component": component, "gen": 1, "ts": float(i),
             "llm": {"latencyProvenance": "revl-measured-bracket",
                     "latencySeconds": seconds}}
            for i in range(n)]


def receipt_for(contract, verdicts, *, generation=1, action=slo.PAUSE,
                composition="base.rvl", key=KEY):
    body = slo.build_body(composition=composition, generation=generation,
                          contract=contract, verdicts=verdicts, action=action,
                          issued_at="2026-01-01T00:00:00+00:00")
    return slo.make_receipt(body, key)


# ---------------------------------------------------- the `on breach` surface

def test_on_breach_parses_per_datum_and_only_where_written():
    """`on breach <response>` is optional and per datum. The two returns of
    `_slo_block` stay apart: the targets are the list slice 1 already produced
    and the responses are their own map, which is what keeps a targets-only
    block producing the declaration it produced before this slice."""
    decl = parse_composition(
        'slo { p95_latency: 250ms on breach divert "standby.rvl", '
        'success_rate: 99.5, max_pending_tasks: 8 on breach halt }')
    assert decl.slo == [("p95_latency", 250, 1), ("success_rate", 99.5, 1),
                        ("max_pending_tasks", 8, 1)]
    assert decl.slo_responses == {
        "p95_latency": ("divert", "standby.rvl"),
        "max_pending_tasks": ("halt", None),
    }


def test_a_targets_only_block_declares_no_responses_at_all():
    """The additivity claim at the declaration: the pre-slice spelling yields
    an EMPTY response map, not a map of defaults. "The document chose nothing"
    and "the document chose the default" are different facts and the surface
    keeps them apart; the default is applied by the reader."""
    decl = parse_composition("slo { p95_latency: 250ms, success_rate: 99.5 }")
    assert decl.slo_responses == {}


def test_on_is_an_ordinary_name_until_breach_follows_it():
    """The lookahead is two tokens. `on` heads the clause only when `breach`
    follows, so the word stays ordinary everywhere else — the same discipline
    the `on <event>` handler surface keeps."""
    program = Parser("""
service S {
  emission fn go(on: Str) -> Str
}

component C provides svc: S {
  provide svc { fn go(on) = on }
}
""", "t.rvl").parse()
    assert list(program.services[0].methods["go"].params)[0][0] == "on"


@pytest.mark.parametrize("body, needle", [
    ("slo { p95_latency: 1s on breach retry }",
     "unknown SLO breach response 'retry'"),
    ('slo { p95_latency: 1s on breach divert "" }', "empty source path"),
    ("slo { p95_latency: 1s on breach divert }",
     "the fallback provider source `divert` diverts to"),
])
def test_the_response_registry_is_closed(body, needle):
    """A response nothing implements is a promise the runtime cannot keep, so
    the registry is closed exactly as the datum registry is, and a `divert`
    with no destination is refused rather than degraded into a pause."""
    with pytest.raises(RevlError) as exc:
        parse_composition(body)
    assert needle in str(exc.value)


def test_the_default_response_is_one_fact_stated_once():
    """`parser.SLO_DEFAULT_RESPONSE` and `slo.DEFAULT_ACTION` are the same
    decision seen from the surface and from the runtime. Two spellings of one
    fact drift; this is the assertion that notices."""
    assert SLO_DEFAULT_RESPONSE == slo.DEFAULT_ACTION
    assert set(SLO_RESPONSES) == set(slo.ACTIONS)


# ------------------------------------------------------------- the IR carrier

def test_a_declared_response_rides_its_own_conditional_ir_key(tmp_path):
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach halt, '
                            'max_pending_tasks: 100 }')
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    assert ir["slo"] == {"p95_latency_ms": 2000, "max_pending_tasks": 100}
    assert ir["slo_on_breach"] == {
        "p95_latency_ms": {"action": "halt", "divertTo": None}}


def test_a_targets_only_slo_block_emits_the_pre_slice_ir_byte_for_byte(tmp_path):
    """THE ADDITIVITY PIN. A `slo` block that declares targets and no responses
    must produce exactly the document it produced before the observed half
    existed: the `slo` key as slice 1 wrote it, and NO `slo_on_breach` key at
    all. The comparison is over the serialized bytes, because "additive" is a
    claim about the document and not about a dict that happens to compare
    equal."""
    doc = project(tmp_path, "slo { p95_latency: 2s, max_pending_tasks: 100 }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    assert "slo_on_breach" not in ir
    assert json.dumps(ir["slo"], sort_keys=True) == json.dumps(
        {"p95_latency_ms": 2000, "max_pending_tasks": 100}, sort_keys=True)

    (tmp_path / "plain").mkdir()
    plain = project(tmp_path / "plain", "")
    plain_ir = resolve_file(str(plain), str(tmp_path / "plain")).to_ir()
    assert "slo" not in plain_ir and "slo_on_breach" not in plain_ir
    # and the whole document, not just the two keys: a composition declaring
    # targets only differs from one declaring nothing by exactly the `slo` key.
    assert {k: v for k, v in ir.items() if k != "slo"} == {
        k: v for k, v in plain_ir.items()}


def test_slo_contract_reports_a_response_only_where_one_was_declared(tmp_path):
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach pause, '
                            'success_rate: 99.5 }')
    contract = resolve_file(str(doc), str(tmp_path)).slo_contract()
    assert contract["p95_latency_ms"]["onBreach"] == {
        "action": "pause", "divertTo": None}
    assert "onBreach" not in contract["success_rate_pct"]


def test_contract_from_ir_resolves_the_default_and_says_it_was_a_default():
    ir = {"slo": {"p95_latency_ms": 250, "success_rate_pct": 99.5},
          "slo_on_breach": {"p95_latency_ms": {"action": "halt",
                                               "divertTo": None}}}
    contract = slo.contract_from_ir(ir)
    assert contract["p95_latency_ms"]["action"] == "halt"
    assert contract["p95_latency_ms"]["declared"] is True
    assert contract["success_rate_pct"]["action"] == SLO_DEFAULT_RESPONSE
    assert contract["success_rate_pct"]["declared"] is False
    assert slo.contract_from_ir({}) == {}
    assert slo.contract_from_ir({"components": []}) == {}


# --------------------------------------------------------- the datum spelling

def test_one_door_resolves_both_spellings_of_a_datum():
    """The compile-time gate reads surface names off the declaration and the
    observed half reads unit-bearing keys off the IR. `slo_datum` is the one
    lookup both go through, and a datum it failed to recognise would be
    reported `unmeasurable` — a silent fail-OPEN, since an unmeasured objective
    takes no action."""
    assert slo_datum("p95_latency") == "p95_latency"
    assert slo_datum("p95_latency_ms") == "p95_latency"
    assert slo_datum("success_rate_pct") == "success_rate"
    assert slo_datum("throughput") is None
    for datum, ir_key in SLO_IR_KEYS.items():
        assert slo_datum(ir_key) == datum


def test_every_declared_datum_has_a_direction():
    assert set(SLO_DIRECTION) == set(SLO_IR_KEYS)
    assert SLO_DIRECTION["success_rate"] == "lower"
    assert SLO_DIRECTION["p95_latency"] == "upper"


# -------------------------------------------------------------- the measurement

def test_a_p95_over_the_target_breaches_in_the_targets_own_unit():
    """THE DEFECT THIS SLICE FIXES, pinned. The trace records seconds and the
    contract records milliseconds; a measurement that compared 3.0 against 2000
    would report every slow run as holding. The verdict's `observed` is in the
    target's unit and the comparison is made there."""
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 2000}})
    verdicts = slo.measure(contract, slo.observations(hops(30, 3.0)))
    entry = verdicts["p95_latency_ms"]
    assert entry["verdict"] == slo.BREACHED
    assert entry["observed"] == 3000.0
    assert entry["unit"] == "ms"
    assert slo.breached_keys(contract, verdicts) == ["p95_latency_ms"]


def test_a_p95_under_the_target_holds():
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 2000}})
    verdicts = slo.measure(contract, slo.observations(hops(30, 0.5)))
    assert verdicts["p95_latency_ms"]["verdict"] == slo.HOLDING
    assert slo.satisfied(verdicts["p95_latency_ms"])
    assert slo.breached_keys(contract, verdicts) == []


def test_a_sample_too_small_for_a_percentile_is_insufficient_not_a_maximum():
    """The maximum of four samples is not a 95th percentile. `insufficient` is
    its own verdict because reporting it as either `holding` or `breached`
    would put a number a reader acts on behind a name that does not describe
    it."""
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 10}})
    verdicts = slo.measure(contract, slo.observations(hops(4, 99.0)))
    assert verdicts["p95_latency_ms"]["verdict"] == slo.INSUFFICIENT
    assert not slo.satisfied(verdicts["p95_latency_ms"])
    # FAIL CLOSED, and no action: not satisfied, but not a breach either.
    assert slo.breached_keys(contract, verdicts) == []
    assert slo.not_holding_keys(contract, verdicts) == ["p95_latency_ms"]


def test_a_host_reported_latency_is_not_folded_into_a_revl_measured_p95():
    """A number revl did not take itself is an "arbitrary external service-level
    indicator", which the item's own scope line excludes. It is skipped and
    counted, never averaged in."""
    events = hops(30, 0.1) + [
        {"event": "emit", "component": "C", "ts": 1.0,
         "llm": {"latencyProvenance": "host-reported", "latencySeconds": 900.0}}]
    obs = slo.observations(events)
    assert len(obs["latencies"]) == 30
    assert obs["hostReportedLatencies"] == 1
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 200}})
    assert slo.measure(contract, obs)["p95_latency_ms"]["verdict"] == slo.HOLDING


def test_a_rate_is_a_lower_bound_and_a_duration_an_upper_one():
    """The direction is the datum's, not the surface's. `_decide` is the one
    comparison, so this pins the rule rather than a spelling of it."""
    assert slo._decide(99.9, 99.5, "lower")["verdict"] == slo.HOLDING
    assert slo._decide(99.0, 99.5, "lower")["verdict"] == slo.BREACHED
    assert slo._decide(99.0, 99.5, "upper")["verdict"] == slo.HOLDING
    assert slo._decide(250.0, 250.0, "upper")["verdict"] == slo.HOLDING


def test_the_three_datums_this_tree_cannot_measure_read_unmeasurable():
    """Not `holding`. Three of five datums have no seam, and a receipt that
    omitted them or defaulted them to holding would read as five objectives
    met. Each carries the reason, which names what would have to exist."""
    contract = slo.contract_from_ir({"slo": {
        "success_rate_pct": 99.5, "recovery_time_ms": 30000,
        "approval_wait_ms": 60000}})
    verdicts = slo.measure(contract, slo.observations(hops(30, 0.1)))
    for key in contract:
        assert verdicts[key]["verdict"] == slo.UNMEASURABLE
        assert verdicts[key]["reason"]
        assert not slo.satisfied(verdicts[key])
    assert slo.breached_keys(contract, verdicts) == []
    assert slo.not_holding_keys(contract, verdicts) == sorted(
        contract, key=list(contract).index)


def test_an_empty_contract_measures_to_nothing():
    assert slo.measure({}, slo.observations(hops(30, 9.0))) == {}


# ------------------------------------------------------------ the receipt

def test_a_receipt_round_trips_and_binds_its_key_into_the_signed_body():
    """The `keyId` is inside the signature, not beside it. A member added after
    signing is a member the verifier includes and the signer did not, which
    makes every receipt verify as if it had been altered — so this pins that
    a freshly issued receipt verifies at all."""
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 2000}})
    verdicts = slo.measure(contract, slo.observations(hops(30, 3.0)))
    receipt = receipt_for(contract, verdicts)
    assert receipt["keyId"] == slo.key_id(KEY)
    assert slo.verify_receipt(receipt, KEY) == (True, "")


def test_verification_fails_closed_on_a_wrong_key_and_an_altered_body():
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 2000}})
    verdicts = slo.measure(contract, slo.observations(hops(30, 3.0)))
    receipt = receipt_for(contract, verdicts)

    ok, reason = slo.verify_receipt(receipt, b"a different signing key entirely")
    assert not ok and "signed by key" in reason

    altered = json.loads(json.dumps(receipt))
    altered["verdicts"]["p95_latency_ms"]["verdict"] = slo.HOLDING
    ok, reason = slo.verify_receipt(altered, KEY)
    assert not ok


def test_a_receipt_whose_summary_contradicts_its_own_verdicts_is_refused():
    """A MAC proves authorship, not meaning. `response.breached` is the line a
    reader quotes, so a signed body naming no breach while its own verdicts read
    `breached` is refused even with an intact signature."""
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 2000}})
    verdicts = slo.measure(contract, slo.observations(hops(30, 3.0)))
    body = slo.build_body(composition="base.rvl", generation=1,
                          contract=contract, verdicts=verdicts,
                          action=slo.PAUSE,
                          issued_at="2026-01-01T00:00:00+00:00")
    body["response"]["breached"] = []
    forged = slo.make_receipt(body, KEY)
    ok, reason = slo.verify_receipt(forged, KEY)
    assert not ok and "response.breached is not this receipt's own" in reason


def test_an_slo_receipt_does_not_verify_as_another_protocols_receipt():
    """Four signed protocols, four domain tags. Without a distinct domain the
    same construction over the same shape of document would cross-verify."""
    from revl import erasure_receipt

    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 2000}})
    verdicts = slo.measure(contract, slo.observations(hops(30, 3.0)))
    receipt = receipt_for(contract, verdicts)
    assert slo.RECEIPT_DOMAIN != erasure_receipt.RECEIPT_DOMAIN
    assert erasure_receipt._mac(receipt, KEY) != slo._mac(receipt, KEY)


def test_the_receipt_attaches_to_the_generation_it_names(tmp_path):
    """The exit criterion's last three words. Additive: a generation entry with
    no receipt is the pre-473 entry, and a receipt filed under a generation it
    was not issued against is refused before it is filed."""
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": 2000}})
    verdicts = slo.measure(contract, slo.observations(hops(30, 3.0)))
    receipt = receipt_for(contract, verdicts, generation=7)
    entry = {"generation": 7, "snapshot": None, "ir": {}, "origin": None}
    attached = slo.attach_generation(entry, receipt)
    assert attached["sloReceipt"] == receipt
    assert {k: v for k, v in attached.items() if k != "sloReceipt"} == entry

    with pytest.raises(ValueError) as exc:
        slo.attach_generation({**entry, "generation": 8}, receipt)
    assert "generation 7" in str(exc.value)


# ------------------------------------------------------------- the response

def test_a_live_breach_takes_the_declared_response_and_writes_the_latch(tmp_path):
    """The exit criterion's second clause end to end: a measured breach
    dispatches the DECLARED action, the latch a running composition watches is
    written, and the receipt states what actually happened rather than what was
    intended."""
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach halt }')
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    latch = tmp_path / "session.estop"
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          generation=4, latch=str(latch), key=KEY,
                          events=hops(30, 3.0),
                          now="2026-01-01T00:00:00+00:00")
    verdict = monitor.observe()
    assert verdict["breached"] == ["p95_latency_ms"]
    assert verdict["action"] == slo.HALT
    assert monitor.dispatch["armed"] is True
    record = json.loads(latch.read_text())
    assert record["halted"] is True and record["verdict"] == "halted"
    assert record["resumable"] is False
    assert "p95_latency_ms" in record["reason"]
    assert record["slo"]["generation"] == 4
    assert slo.verify_receipt(monitor.receipt, KEY) == (True, "")
    assert monitor.receipt["response"]["dispatch"]["armed"] is True


def test_a_datum_with_no_declared_response_pauses_rather_than_carrying_on(tmp_path):
    """A breach that changes nothing is not a contract. The floor is `pause`:
    bounded residue, everything registered still owed and recoverable."""
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    latch = tmp_path / "session.estop"
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          generation=1, latch=str(latch), events=hops(30, 3.0))
    verdict = monitor.observe()
    assert verdict["action"] == slo.PAUSE
    record = json.loads(latch.read_text())
    assert record["verdict"] == "paused" and record["resumable"] is True


def test_a_run_that_holds_its_contract_takes_no_action_and_writes_no_latch(tmp_path):
    """NON-VACUITY for the response half: the same composition and the same
    monitor over a compliant run leaves the latch absent and issues no
    receipt."""
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach halt }')
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    latch = tmp_path / "session.estop"
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          generation=1, latch=str(latch), key=KEY,
                          events=hops(30, 0.2))
    verdict = monitor.observe()
    assert verdict["breached"] == []
    assert monitor.receipt is None
    assert not latch.exists()


def test_a_monitor_with_no_contract_is_inert(tmp_path):
    """The byte-identity property, at the runtime end: a composition declaring
    no `slo` block reads no event and writes no latch."""
    doc = project(tmp_path, "")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    latch = tmp_path / "session.estop"
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          latch=str(latch), events=hops(50, 99.0))
    assert monitor.declared is False
    assert monitor.observe() is None
    assert monitor.evaluate() is None
    assert not latch.exists()


def test_a_divert_re_admits_the_fallback_through_the_ordinary_gate(tmp_path):
    """A divert is a re-resolution of the row table with the row's `from`
    pointing at the fallback, admitted through `_admit_full` — the same gate a
    swap runs. Nothing is hot-patched, so a fallback that does not provide the
    key is refused rather than put into service."""
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach divert '
                            '"standby.rvl" }', standby=STANDBY)
    result = slo.admit_divert(composition=str(doc), component="BillingSvc",
                              fallback="standby.rvl", root=str(tmp_path))
    assert result["taken"] is True and result["row"] == "billing"
    rows = {r["label"]: r for r in result["ir"]["rows"]}
    assert rows["billing"]["component"] == "StandbyBilling"
    assert result["resolvedFallback"].endswith("standby.rvl")


def test_a_fallback_that_does_not_admit_is_a_refusal_not_a_silent_pause(tmp_path):
    """A declared fallback that does not admit is a fact about the run. Hiding
    it behind a pause would report a stop the operator never declared."""
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach divert '
                            '"standby.rvl" }', standby=STANDBY)
    with pytest.raises(slo.DivertRefused) as exc:
        slo.admit_divert(composition=str(doc), component="NoSuchComponent",
                         fallback="standby.rvl", root=str(tmp_path))
    assert "no row resolving to component 'NoSuchComponent'" in str(exc.value)

    with pytest.raises(slo.DivertRefused) as exc:
        slo.admit_divert(composition=str(doc), component="BillingSvc",
                         fallback="not-a-file.rvl", root=str(tmp_path))
    assert "names no file" in str(exc.value)


def test_a_refused_divert_is_signed_as_a_divert_that_did_not_happen(tmp_path):
    """The receipt states what happened, not what was intended."""
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach divert '
                            '"gone.rvl" }')
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          generation=2, key=KEY, events=hops(30, 3.0))
    verdict = monitor.observe()
    assert verdict["dispatch"]["action"] == slo.DIVERT
    assert verdict["dispatch"]["taken"] is False
    assert slo.verify_receipt(monitor.receipt, KEY) == (True, "")
    assert monitor.receipt["response"]["dispatch"]["taken"] is False
    assert "[not taken:" in slo.render_receipt(monitor.receipt)


def test_an_armed_latch_is_not_rendered_as_an_action_that_did_not_happen(tmp_path):
    """A latch that armed carries the breach reason it was armed FOR. Reading a
    present `reason` as a failure prints "not taken" on exactly the dispatches
    that were taken."""
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach pause }')
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    monitor = slo.Monitor(slo.contract_from_ir(ir), composition=str(doc),
                          latch=str(tmp_path / "l.estop"), key=KEY,
                          events=hops(30, 3.0))
    rendered = slo.render(monitor.observe())
    assert "action: pause" in rendered
    assert "not taken" not in rendered


# ----------------------------------------------------------- the rollout gate

def _breached_receipt(generation=3, target=2000, observed_seconds=3.0):
    contract = slo.contract_from_ir({"slo": {"p95_latency_ms": target}})
    verdicts = slo.measure(contract,
                           slo.observations(hops(30, observed_seconds)))
    assert verdicts["p95_latency_ms"]["verdict"] == slo.BREACHED
    return receipt_for(contract, verdicts, generation=generation)


def test_a_rollout_is_refused_by_name_on_the_predecessors_breach(tmp_path):
    """THE EXIT CLAUSE. The generation this rollout replaces measurably
    breached an objective the candidate still declares, so the rollout is
    refused — naming the datum, the observed value, the candidate's own target
    and the generation the witness came from."""
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    report = slo.gate_rollout(ir=ir, receipt=_breached_receipt(), key=KEY)
    assert report["admitted"] is False
    assert report["basis"] == slo.WITNESSED
    assert [r["datum"] for r in report["refusals"]] == ["p95_latency_ms"]
    refusal = report["refusals"][0]
    assert refusal["observed"] == 3000.0
    assert refusal["target"] == 2000
    assert refusal["generation"] == 3
    rendered = slo.render_gate(report)
    assert "REFUSED" in rendered and "`p95_latency_ms`" in rendered
    assert "3000.0" in rendered and "generation 3" in rendered

    with pytest.raises(slo.RolloutRefused) as exc:
        slo.admit_rollout(ir=ir, receipt=_breached_receipt(), key=KEY)
    assert exc.value.report["refusals"][0]["datum"] == "p95_latency_ms"


def test_a_compliant_rollout_still_proceeds(tmp_path):
    """NON-VACUITY. The same gate, the same candidate, a predecessor receipt
    whose verdicts HOLD: admitted, and admitted on the strongest basis rather
    than by short-circuiting."""
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    contract = slo.contract_from_ir(ir)
    verdicts = slo.measure(contract, slo.observations(hops(30, 0.2)))
    assert verdicts["p95_latency_ms"]["verdict"] == slo.HOLDING
    report = slo.gate_rollout(ir=ir, receipt=receipt_for(contract, verdicts),
                              key=KEY)
    assert report["admitted"] is True
    assert report["basis"] == slo.WITNESSED
    assert report["refusals"] == []
    assert slo.admit_rollout(ir=ir, receipt=receipt_for(contract, verdicts),
                             key=KEY)["admitted"] is True


def test_a_candidate_that_withdrew_the_promise_is_admitted_and_it_is_recorded(tmp_path):
    """A candidate that widened the objective past the witnessed value is no
    longer contradicted by it. That is the author withdrawing a promise in the
    source, where a reviewer sees it, and the report records the widening
    rather than passing over it."""
    doc = project(tmp_path, "slo { p95_latency: 4s }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    report = slo.gate_rollout(ir=ir, receipt=_breached_receipt(), key=KEY)
    assert report["admitted"] is True
    assert [r["datum"] for r in report["relaxed"]] == ["p95_latency_ms"]
    assert report["relaxed"][0]["previousTarget"] == 2000.0
    assert report["relaxed"][0]["target"] == 4000
    assert "now permits" in slo.render_gate(report)


def test_a_candidate_that_dropped_the_objective_is_not_refused_for_it(tmp_path):
    """Refusing on an objective the candidate does not declare would be
    refusing a document for a sentence it does not contain."""
    doc = project(tmp_path, "slo { max_pending_tasks: 100 }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    report = slo.gate_rollout(ir=ir, receipt=_breached_receipt(), key=KEY)
    assert report["admitted"] is True
    assert report["refusals"] == [] and report["relaxed"] == []


def test_an_unmeasured_objective_never_refuses_a_rollout_and_is_never_hidden(tmp_path):
    """`insufficient` and `unmeasurable` are "nobody knows". Refusing on them
    would refuse every rollout of the three datums this tree cannot measure;
    dropping them would let the admission read as a clean bill. So: admitted,
    and reported under `notHolding`."""
    doc = project(tmp_path, "slo { success_rate: 99.5 }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    contract = slo.contract_from_ir(ir)
    verdicts = slo.measure(contract, slo.observations(hops(30, 0.2)))
    assert verdicts["success_rate_pct"]["verdict"] == slo.UNMEASURABLE
    report = slo.gate_rollout(ir=ir, receipt=receipt_for(contract, verdicts),
                              key=KEY)
    assert report["admitted"] is True
    assert [u["datum"] for u in report["notHolding"]] == ["success_rate_pct"]
    assert "not refused, not measured" in slo.render_gate(report)


def test_a_presented_receipt_that_does_not_verify_refuses_the_rollout(tmp_path):
    """Evidence that cannot be checked is not evidence. Admitting on it would
    let a forged receipt buy an admission."""
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    forged = _breached_receipt()
    forged["verdicts"]["p95_latency_ms"]["verdict"] = slo.HOLDING
    forged["response"]["breached"] = []
    forged["response"]["notHolding"] = []
    report = slo.gate_rollout(ir=ir, receipt=forged, key=KEY)
    assert report["admitted"] is False
    assert report["basis"] == slo.UNVERIFIED


def test_a_keyless_gate_still_runs_and_says_what_it_did_not_check(tmp_path):
    """With no key there is nothing to verify against, and the two directions
    are not symmetric: refusing on an unchecked receipt is the conservative
    error, and admitting on one is no weaker than the `no-evidence` admission a
    caller gets by presenting nothing. So the gate runs, and states that the
    signature was not checked rather than implying it was."""
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    report = slo.gate_rollout(ir=ir, receipt=_breached_receipt(), key=None)
    assert report["admitted"] is False
    assert report["macChecked"] is False
    assert "NOT checked" in report["note"]
    assert "signature UNCHECKED" in slo.render_gate(report)

    with_key = slo.gate_rollout(ir=ir, receipt=_breached_receipt(), key=KEY)
    assert with_key["macChecked"] is True
    assert "UNCHECKED" not in slo.render_gate(with_key)


def test_the_absence_of_a_receipt_is_not_a_refusal(tmp_path):
    """The first generation of any composition has no predecessor, and a gate
    that refused it would refuse every first rollout. The basis says which
    admission this is, so nothing has to infer it."""
    doc = project(tmp_path, "slo { p95_latency: 2s }")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    report = slo.gate_rollout(ir=ir, receipt=None, key=KEY)
    assert report["admitted"] is True and report["basis"] == slo.NO_EVIDENCE
    assert "absence of evidence" in report["note"]


def test_the_gate_is_inert_for_a_composition_that_declares_nothing(tmp_path):
    doc = project(tmp_path, "")
    ir = resolve_file(str(doc), str(tmp_path)).to_ir()
    report = slo.gate_rollout(ir=ir, receipt=_breached_receipt(), key=KEY)
    assert report["admitted"] is True and report["basis"] == slo.NO_CONTRACT


# ---------------------------------------------------------------- the CLI

def test_the_cli_measures_dispatches_signs_verifies_and_gates(tmp_path, capsys):
    """The operator's path, end to end, through `revl slo`. Exit status is the
    contract: 0 clean, 1 a measured breach, 2 a refused rollout."""
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach halt }')
    (tmp_path / "trace.json").write_text(json.dumps({"events": hops(30, 3.0)}))
    (tmp_path / "key").write_text("an-slo-signing-key-for-the-tests")
    latch = tmp_path / "s.estop"
    receipt = tmp_path / "receipt.json"

    code = main(["slo", "--composition", str(doc), "--root", str(tmp_path),
                 "--trace", str(tmp_path / "trace.json"), "--generation", "3",
                 "--latch", str(latch), "--key", str(tmp_path / "key"),
                 "--out", str(receipt)])
    out = capsys.readouterr().out
    assert code == 1
    assert "breached" in out and "p95_latency_ms" in out
    assert json.loads(latch.read_text())["verdict"] == "halted"

    assert main(["slo", "--verify", "--receipt", str(receipt),
                 "--key", str(tmp_path / "key")]) == 0
    assert "VERIFIED" in capsys.readouterr().out

    code = main(["slo", "--gate", "--composition", str(doc),
                 "--root", str(tmp_path), "--receipt", str(receipt),
                 "--key", str(tmp_path / "key")])
    out = capsys.readouterr().out
    assert code == 2
    assert "REFUSED" in out and "p95_latency_ms" in out


def test_the_cli_admits_a_compliant_rollout(tmp_path, capsys):
    """The CLI-level non-vacuity control: the same three commands over a run
    that held its contract exit 0 throughout and write no latch."""
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach halt }')
    (tmp_path / "trace.json").write_text(json.dumps({"events": hops(30, 0.2)}))
    (tmp_path / "key").write_text("an-slo-signing-key-for-the-tests")
    latch = tmp_path / "s.estop"

    assert main(["slo", "--composition", str(doc), "--root", str(tmp_path),
                 "--trace", str(tmp_path / "trace.json"),
                 "--latch", str(latch),
                 "--key", str(tmp_path / "key")]) == 0
    assert "no breach" in capsys.readouterr().out
    assert not latch.exists()

    contract = slo.contract_from_ir(
        resolve_file(str(doc), str(tmp_path)).to_ir())
    verdicts = slo.measure(contract, slo.observations(hops(30, 0.2)))
    (tmp_path / "r.json").write_text(json.dumps(
        receipt_for(contract, verdicts)))
    assert main(["slo", "--gate", "--composition", str(doc),
                 "--root", str(tmp_path), "--receipt", str(tmp_path / "r.json"),
                 "--key", str(tmp_path / "key")]) == 0
    assert "admitted" in capsys.readouterr().out


def test_the_cli_says_so_for_a_composition_that_declares_no_contract(
        tmp_path, capsys):
    doc = project(tmp_path, "")
    assert main(["slo", "--composition", str(doc), "--root", str(tmp_path)]) == 0
    assert "no `slo` contract" in capsys.readouterr().out


def test_the_panel_prints_the_response_and_marks_the_default(tmp_path, capsys):
    doc = project(tmp_path, 'slo { p95_latency: 2s on breach halt, '
                            'max_pending_tasks: 100 }')
    assert main(["composition", str(doc), "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "on breach halt" in out
    assert "on breach pause (default)" in out

"""The OBSERVED half of the SLO contract — roadmap item 473, issue #825.

`docs/v2.0-roadmap.md` row 473 states the exit criterion verbatim:

    "a rollout predicted to breach a declared SLO is refused, and a live
    breach triggers the declared fallback or pause with an SLO receipt on the
    generation."

The first clause is the COMPILE-TIME gate, landed as `4253a63b` (PR #869): a
composition-level `slo { ... }` block is parsed (`parser._slo_block`) and its
declared ceilings are checked when the composition is built
(`composition._check_slo_bounds`). That half is about a PREDICTION — a
composition that cannot meet its declared ceilings never boots.

This module is the second clause, which is about OBSERVATION. It answers three
questions and nothing else:

  1. **What did the runtime actually see?** (`observations`) — the measured
     values, read from the causal trace the runtime already records. No new
     metrics pipeline exists and none is added here.
  2. **Does that breach the declared contract?** (`measure`) — one verdict per
     declared datum, in a closed vocabulary, with the honest reading available
     for every datum this runtime cannot measure.
  3. **What does the contract say to DO about it?** (`Monitor`, `admit_divert`,
     `pause`, `halt`) — the declared per-datum action, dispatched, with a
     signed receipt bound to the running generation.

## The scope, quoted from the design of record

`docs/design/473-slo-contracts.md` fixes the boundary this module must not
cross:

    "The scope is the declared budget set and the predicted or observed values
    the runtime can measure, not arbitrary external service-level indicators."

So this module measures ONLY what `revl` genuinely knows from its own trace:
the latency bracket it took itself (item 121), the lifecycle durations it
timestamped itself, the emission counts it recorded itself, and the
cardinality it derived at admission. It does not call a metrics backend, it
does not scrape a service, and it does not invent a denominator. Where the
runtime cannot measure a declared datum, the verdict is `unmeasurable` and it
carries a reason — see `measure`.

## FAIL CLOSED

The exit criterion's contract is only worth signing if a breach cannot be read
as compliance. Two rules enforce that here:

  * `unmeasurable` is NOT `holding`. `satisfied()` returns False for anything
    that is not `holding`, so a datum no measurement reached never reads as a
    met objective — not in the receipt, and not in the dispatch decision.
  * Verification is fail-closed (`verify_receipt`): a receipt whose envelope is
    wrong, whose body does not match its signature, or whose signing key is not
    the one the verifier holds is REFUSED. There is no partially-trusted
    receipt.

## A composition that declares no `slo` block

Byte-identically unaffected. `contract_from_ir` returns an empty contract, the
monitor's `contract` is falsy, `Monitor.observe` is a no-op that returns None
before touching a single event, and the composition IR emits neither `slo` nor
`slo_on_breach` when nothing was declared. `tests/test_slo_473.py` pins the
byte-identity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from typing import Mapping, Optional

from . import attest

# ---------------------------------------------------------------------------
# The verdict vocabulary — closed, and ordered from best to worst.
# ---------------------------------------------------------------------------

#: The datum's declared objective was met by a measurement that reached it.
HOLDING = "holding"
#: The datum's declared objective was MISSED by a measurement that reached it.
BREACHED = "breached"
#: The datum was measured, but the measurement cannot decide the objective:
#: too few samples for a percentile, no declared denominator for a rate. This is
#: NOT a breach (nothing was observed to fail) and NOT holding (nothing was
#: observed to hold). It is its own answer because collapsing it into either
#: one is the dishonesty the fail-closed rule forbids.
INSUFFICIENT = "insufficient"
#: The datum was not measured at all: this runtime has no seam that observes
#: it. The reason travels with the verdict.
UNMEASURABLE = "unmeasurable"

VERDICTS = (HOLDING, BREACHED, INSUFFICIENT, UNMEASURABLE)

#: The verdicts that mean "this datum did not hold". `insufficient` and
#: `unmeasurable` are deliberately NOT here: an unmeasured objective is not an
#: observed failure, and reporting it as one would make the monitor divert on a
#: datum nobody looked at. It is also, just as deliberately, not in
#: `SATISFIED` — see below.
NOT_HOLDING = (BREACHED, INSUFFICIENT, UNMEASURABLE)

#: The ONE verdict that reads as a met objective. `satisfied()` is the only
#: door a caller should use to ask "did this hold?", and it answers True for
#: exactly one member of `VERDICTS`. FAIL CLOSED: an objective nobody measured
#: is not an objective that held.
SATISFIED = (HOLDING,)

# ---------------------------------------------------------------------------
# The action vocabulary — closed.
# ---------------------------------------------------------------------------

#: Swap the breaching component's provider for a declared fallback, re-admitted
#: through the ordinary gate. Cheapest: no residue is created, because the
#: composition that replaces it is checked before it runs.
DIVERT = "divert"
#: Stop dispatching new boundary crossings and leave every registered entry
#: owed. This is the E-Stop latch's first move (item 443) under a different
#: verdict: the instance is PAUSED, not killed, so what it owes is still
#: recoverable and the record says so.
PAUSE = "pause"
#: The full E-Stop. The instance is dead, its entries are STRANDED, and the way
#: back is `revl recover --wal <file>`.
HALT = "halt"

ACTIONS = (DIVERT, PAUSE, HALT)

#: The action a breached datum takes when the surface declared none. A breach
#: that changes nothing is not a contract, and the exit criterion names exactly
#: two legal outcomes ("the declared fallback or pause"); `pause` is the
#: bounded-residue floor between them. `parser.SLO_DEFAULT_RESPONSE` is the same
#: fact at the surface, and the two are asserted equal by the tests.
DEFAULT_ACTION = PAUSE

# ---------------------------------------------------------------------------
# The receipt — the SIGNED-RECEIPT pattern of `erasure_receipt`, third domain.
# ---------------------------------------------------------------------------

RECEIPT_VERSION = "1.0"
RECEIPT_KIND = "revl.slo-receipt"

#: The MAC is taken over this tag ++ the canonical body bytes. The tag is not
#: decoration: `attest._sign`, `deploy._receipt_mac` and `erasure_receipt._mac`
#: MAC the same shape of document with the same construction, so WITHOUT a
#: distinct domain an SLO receipt verified as an erasure receipt and an erasure
#: receipt verified as an SLO receipt. Four signed protocols, four domains.
RECEIPT_DOMAIN = b"revl.slo-receipt/v1\x00"
SIGNATURE_FIELD = "signature"
HASH_ALG = "sha256"
SIGN_ALG = "hmac-sha256"

KEY_ENV = "REVL_SLO_KEY"
KEY_FILE_ENV = "REVL_SLO_KEY_FILE"

#: What the receipt's `scope` section states, so an auditor reading a rendered
#: receipt cannot mistake what it proves for what it does not.
SCOPE = {
    "title": "the declared SLO contract of ONE running generation",
    "proves": [
        "every declared datum was read from this run's own causal trace",
        "each verdict is the objective's own comparison, not a threshold",
        "a datum that is not `holding` is not reported as satisfied",
        "the receipt names the generation it was issued against",
    ],
    "doesNotProve": [
        "that the composition will hold its SLO in the NEXT generation",
        "any service-level indicator revl does not itself observe",
        "that an `unmeasurable` datum would have held",
        "that a measured percentile is cross-process (see `scope.measurement`)",
    ],
    "reference": "docs/design/473-slo-contracts.md",
}

#: The honesty note the receipt carries about its own measurements. This is the
#: design constraint ("the observed values the runtime can measure, not
#: arbitrary external service-level indicators") made explicit in the signed
#: artifact rather than left in a docstring.
MEASUREMENT_SCOPE = (
    "per-process: `ts` is `time.monotonic()`, meaningful only as a difference "
    "within one run, so a percentile is over THIS process's samples"
)


def satisfied(verdict: Mapping) -> bool:
    """FAIL CLOSED: does this verdict read as a met objective?

    Exactly one member of `VERDICTS` does. `insufficient` and `unmeasurable`
    are not "probably fine" — they are "nobody knows", and a contract that
    reads an unknown as a pass is not a contract. Every consumer that asks
    "did the SLO hold?" must come through here rather than comparing against
    `BREACHED` itself, so there is one place the rule lives.
    """
    return verdict.get("verdict") in SATISFIED


# ---------------------------------------------------------------------------
# 1. The contract, as the composition IR carries it.
# ---------------------------------------------------------------------------

def contract_from_ir(ir: Mapping | None) -> dict:
    """The declared contract of a composition, read off its IR document.

    The compile-time half already emits both halves of the declaration, and
    this reads them rather than re-deriving them from a source file: `slo` is
    the ceiling set `composition._slo_ceilings` validated at admission, and
    `slo_on_breach` is the action each datum declared. Reading the IR is what
    makes the contract in the receipt the SAME OBJECT the gate checked, rather
    than a second parse that could disagree with it.

    Returns `{}` for a composition that declares no `slo` block — the whole
    point of which is that every downstream check short-circuits.
    """
    if not isinstance(ir, Mapping):
        return {}
    declared = ir.get("slo")
    if not isinstance(declared, Mapping) or not declared:
        return {}
    responses = ir.get("slo_on_breach")
    responses = responses if isinstance(responses, Mapping) else {}
    out: dict = {}
    for key, target in declared.items():
        entry = responses.get(key)
        action = DEFAULT_ACTION
        divert_to = None
        explicit = False
        if isinstance(entry, Mapping) and entry.get("action") in ACTIONS:
            action = entry["action"]
            divert_to = entry.get("divertTo")
            explicit = True
        out[key] = {"target": target, "action": action,
                    "divertTo": divert_to, "declared": explicit}
    return out


def breached_keys(contract: Mapping, verdicts: Mapping) -> list[str]:
    """The declared datums a measurement observed MISSING their objective, in
    declaration order.

    `breached` only. An `unmeasurable` datum is not a breach — diverting on a
    datum nobody measured would spend a fallback on a guess — and it is not
    silently dropped either: it is in the receipt, and `notHolding` names it.
    """
    return [key for key in contract if verdicts.get(key, {}).get("verdict")
            == BREACHED]


def not_holding_keys(contract: Mapping, verdicts: Mapping) -> list[str]:
    """Every declared datum that does not read as satisfied, in declaration
    order. This is the fail-closed reading: `insufficient` and `unmeasurable`
    are in here. It is reported in the receipt beside `breached` so an auditor
    can tell "we measured it and it failed" from "we never measured it", which
    is the distinction that makes the first number usable."""
    return [key for key in contract if not satisfied(verdicts.get(key, {}))]


# ---------------------------------------------------------------------------
# 2. The observations — what this runtime genuinely knows.
# ---------------------------------------------------------------------------

#: The reason a datum is `unmeasurable`, per datum, with the honest statement
#: of WHICH seam is missing rather than a shrug. Each of these is a claim about
#: the compiler and must be revised when the seam lands, which is why it names
#: the thing that would have to exist.
UNMEASURABLE_REASON = {
    "recovery_time":
        "no runtime seam observes time-to-recovery; the value is a ceiling "
        "checked at admission, never an observation",
    "approval_wait":
        "no runtime seam observes the wait between an approval being required "
        "and being granted; the trace records that an approval was required, "
        "not how long it was awaited",
}


def _duration_observations(events) -> dict:
    """Per-component lifecycle durations, paired load -> withdraw by
    `(component, gen)` exactly as `metrics._duration_metrics` pairs them.

    A component with no `ts` on either end of its pair is not measured, and the
    caller degrades the whole datum rather than averaging a partial sample: a
    percentile over the components that happened to be timestamped is a
    percentile of a different population, which is the mistake the metrics
    degrade discipline exists to prevent.
    """
    opened: dict = {}
    samples: list = []
    for event in events:
        ts = event.get("ts")
        if ts is None:
            continue
        identity = (event.get("component"), event.get("gen"))
        if event.get("event") == "load":
            opened[identity] = ts
        elif event.get("event") == "withdraw":
            start = opened.pop(identity, None)
            if start is not None:
                samples.append(max(0.0, float(ts) - float(start)))
    return {"durations": samples, "open": sorted(
        c for c, _ in opened if c is not None)}


def _latency_observations(events) -> dict:
    """The model-hop latency bracket (item 121) — the ONE per-crossing latency
    the runtime measures itself.

    `latencyProvenance` is checked rather than trusted: a payload that does not
    claim `revl-measured-bracket` is a host-reported number, and folding a host
    number into a revl-measured percentile is exactly the "arbitrary external
    service-level indicator" the scope forbids. It is counted as skipped, so
    the receipt can say how many crossings it declined to measure.
    """
    samples: list = []
    skipped = 0
    for event in events:
        llm = event.get("llm")
        if not isinstance(llm, Mapping):
            continue
        if llm.get("latencyProvenance") != "revl-measured-bracket":
            skipped += 1
            continue
        value = llm.get("latencySeconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            samples.append(float(value))
        else:
            skipped += 1
    return {"latencies": samples, "hostReported": skipped}


def observations(events) -> dict:
    """Everything the runtime genuinely observed on this run, from the causal
    trace it already records. Pure: it reads the event list and nothing else.

    No new pipeline. Every number here is a number revl took itself:
      * `latencies` — the item-121 model-hop bracket;
      * `durations` — load/withdraw pairs, the same pairing `metrics` uses;
      * `emissions` — the emit events the runtime recorded;
      * `open` — components still loaded at the end of the trace, so a
        duration that has not closed is visible rather than silently absent.
    """
    events = list(events or [])
    durations = _duration_observations(events)
    latencies = _latency_observations(events)
    emissions = 0
    by_component: dict = {}
    for event in events:
        if event.get("event") == "emit":
            emissions += 1
            component = event.get("component")
            by_component[component] = by_component.get(component, 0) + 1
    return {
        "events": len(events),
        "latencies": latencies["latencies"],
        "hostReportedLatencies": latencies["hostReported"],
        "durations": durations["durations"],
        "openAtEnd": durations["open"],
        "emissions": emissions,
        "emissionsByComponent": by_component,
    }


def percentile(values, fraction: float) -> Optional[float]:
    """The nearest-rank percentile of `values`, or None for an empty sample.

    Nearest-rank, not interpolated, and stated because a percentile is only
    checkable if its definition travels with it: the k-th smallest value with
    k = ceil(fraction * n), so a p95 over 20 samples is the 19th. A sample too
    small for the fraction to select a rank distinct from the maximum is
    reported by `measure` as `insufficient` rather than as a p95, because the
    maximum of four samples is not a 95th percentile and printing it as one is
    the number a reader would act on.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(max(1, math.ceil(len(ordered) * fraction)), len(ordered))
    return ordered[index - 1]


#: How many samples a percentile needs before it is a percentile rather than a
#: maximum wearing its name. `ceil(0.95 * n) < n` is the condition, which holds
#: from n = 21; stated as a constant so the receipt and the test agree.
PERCENTILE_MIN_SAMPLES = 21

#: The trace records every duration in SECONDS (`ts` is a `time.monotonic()`
#: reading, `llm.latencySeconds` says so in its name); the contract records
#: every duration in MILLISECONDS, because `parser.SLO_IR_KEYS` puts the unit in
#: the key name so a target cannot be re-read in another unit downstream. The
#: conversion therefore happens exactly ONCE, here, on the observation side, and
#: the verdict's `observed` is in the target's unit — a receipt that compared
#: 1.5 against 250 and called it holding is the arithmetic this constant exists
#: to stop.
MS_PER_SECOND = 1000.0


# ---------------------------------------------------------------------------
# 3. The measurement — one verdict per declared datum.
# ---------------------------------------------------------------------------

def _decide(value: Optional[float], target, direction: str, *,
            n: int = 1, reason: str = "") -> dict:
    """One verdict. `direction` is the datum's own property
    (`parser.SLO_DIRECTION`): `upper` means the observation must stay BELOW
    the target, `lower` means it must stay ABOVE.

    `target` arriving as anything but a number is `unmeasurable` rather than a
    crash: the contract's ceilings are validated at admission, but this reads
    the IR, and a receipt is not the place to discover a malformed document.
    """
    if value is None:
        return {"verdict": UNMEASURABLE, "observed": None, "target": target,
                "samples": n, "reason": reason or "no measurement reached this "
                "datum"}
    if not isinstance(target, (int, float)) or isinstance(target, bool):
        return {"verdict": UNMEASURABLE, "observed": value, "target": target,
                "samples": n,
                "reason": f"the declared target is not a number ({target!r})"}
    observed = float(value)
    limit = float(target)
    if direction == "lower":
        held = observed >= limit
    else:
        held = observed <= limit
    return {"verdict": HOLDING if held else BREACHED, "observed": observed,
            "target": limit, "samples": n, "direction": direction,
            "reason": ""}


def measure(contract: Mapping, obs: Mapping) -> dict:
    """One verdict per declared datum. The whole of the honest scope lives
    here, so there is exactly one place that decides what revl can see.

    Per datum, and why:

      * `p95_latency` — MEASURED, from the item-121 model-hop bracket, over the
        model hops THIS run recorded. The sample is per-crossing and only model
        completions produce one, so a composition with no model hop gets
        `insufficient` with that as the reason rather than a fabricated zero.
        The percentile needs `PERCENTILE_MIN_SAMPLES`; below that the datum is
        `insufficient`, because the maximum of a handful of samples is not a
        95th percentile.
      * `max_pending_tasks` — MEASURED, and the measurement is the run's
        observed maximum concurrency: the largest number of emit events inside
        one wall-clock window is not computable from `time.monotonic()` across
        processes, so what IS measured is the peak number of emit events
        recorded for a single component between its load and its withdraw.
        Stated as `perComponentEmissionsPeak` in the receipt, because a reader
        must be able to see which quantity the ceiling was applied to.
      * `success_rate` — NOT MEASURABLE as declared. A rate needs a numerator
        and a DENOMINATOR, and the surface declares only the rate. The runtime
        records failures (`failures.total` in `metrics`) but there is no
        declared denominator to divide by, so a receipt that computed
        `1 - failures/emissions` would be inventing one — the exact move the
        scope constraint forbids. Verdict: `unmeasurable`, with the missing
        denominator as the reason. The runtime CAN observe whether any failure
        occurred, and `failures` is carried in the receipt so an auditor can
        see it.
      * `recovery_time`, `approval_wait` — NOT MEASURABLE, see
        `UNMEASURABLE_REASON`.

    Returns `{}` for an empty contract, which is how a composition declaring no
    `slo` block stays byte-identically unaffected.

    A contract key may be spelled either way — the surface datum
    (`p95_latency`) or the unit-bearing IR key (`p95_latency_ms`) — and
    `parser.slo_datum` is the one door that resolves it. It has to be one door:
    the compile-time gate reads the surface names off the declaration and this
    reads the IR keys off the document, and a measurement that failed to
    recognise the second spelling would answer `unmeasurable` for every
    objective in the contract, which fails OPEN in the only way this module
    exists to prevent.
    """
    from .parser import SLO_DIRECTION, slo_datum

    if not contract:
        return {}
    latencies = list(obs.get("latencies") or [])
    durations = list(obs.get("durations") or [])
    verdicts: dict = {}
    for key, entry in contract.items():
        target = entry.get("target") if isinstance(entry, Mapping) else entry
        datum = slo_datum(key)
        direction = SLO_DIRECTION.get(datum or key, "upper")
        if datum == "p95_latency":
            if len(latencies) < PERCENTILE_MIN_SAMPLES:
                verdicts[key] = {
                    "verdict": INSUFFICIENT, "observed": None, "target": target,
                    "samples": len(latencies),
                    "reason": f"{len(latencies)} model-hop sample(s); a p95 needs "
                              f"at least {PERCENTILE_MIN_SAMPLES}",
                    "measurement": "modelHopLatencyBracket"}
            else:
                # The trace's seconds against the contract's milliseconds: the
                # unit is converted on the OBSERVATION, never on the target, so
                # what the receipt prints as `target` is the number the document
                # declared.
                seconds = percentile(latencies, 0.95)
                verdicts[key] = _decide(
                    None if seconds is None else seconds * MS_PER_SECOND,
                    target, direction, n=len(latencies))
                verdicts[key]["measurement"] = "modelHopLatencyBracket"
                verdicts[key]["unit"] = "ms"
            continue
        if datum == "max_pending_tasks":
            by_component = obs.get("emissionsByComponent") or {}
            peak = max(by_component.values()) if by_component else 0
            verdicts[key] = _decide(peak, target, direction, n=peak)
            verdicts[key]["measurement"] = "perComponentEmissionPeak"
            verdicts[key]["unit"] = "tasks"
            continue
        if datum == "success_rate":
            verdicts[key] = {
                "verdict": UNMEASURABLE, "observed": None, "target": target,
                "samples": 0,
                "reason": "a rate needs a declared denominator and the surface "
                          "declares none; revl records that failures occurred, "
                          "not a request population to divide them by",
                "measurement": "none"}
            continue
        if datum == "recovery_time":
            verdicts[key] = {
                "verdict": UNMEASURABLE, "observed": None, "target": target,
                "samples": len(durations),
                "reason": UNMEASURABLE_REASON["recovery_time"],
                "measurement": "none"}
            continue
        if datum == "approval_wait":
            verdicts[key] = {
                "verdict": UNMEASURABLE, "observed": None, "target": target,
                "samples": 0,
                "reason": UNMEASURABLE_REASON["approval_wait"],
                "measurement": "none"}
            continue
        verdicts[key] = {
            "verdict": UNMEASURABLE, "observed": None, "target": target,
            "samples": 0,
            "reason": f"no runtime seam observes `{key}`",
            "measurement": "none"}
    return verdicts


# ---------------------------------------------------------------------------
# 4. The receipt.
# ---------------------------------------------------------------------------

def key_from_env() -> Optional[bytes]:
    """The signing key from the environment, or None. `REVL_SLO_KEY_FILE`
    wins over `REVL_SLO_KEY`, the same order `attest.resolve_key` uses."""
    path = os.environ.get(KEY_FILE_ENV)
    if path:
        return attest.load_key(path)
    inline = os.environ.get(KEY_ENV)
    if inline:
        return inline.encode("utf-8")
    return None


def resolve_key(flag: Optional[str] = None) -> Optional[bytes]:
    """`--slo-key`/`--key`, else `REVL_SLO_KEY_FILE`, else `REVL_SLO_KEY`."""
    if flag:
        return attest.load_key(flag)
    return key_from_env()


def key_id(key: bytes) -> str:
    """A non-secret fingerprint of the signing key. A distinct domain from
    `attest.key_id`'s, so a key id printed by an SLO receipt can never be
    confused with an attestation's."""
    return hashlib.sha256(b"revl-slo-keyid\x00" + bytes(key)).hexdigest()[:16]


def build_body(*, composition, generation, contract, verdicts, action,
               scope=None, issued_at, signer=None, obs=None) -> dict:
    """The unsigned receipt body. Pure and deterministic given `issued_at`, so
    the same evidence always produces byte-identical output.

    Everything a reader would act on is INSIDE the signed body: the declared
    contract, the verdicts, the action taken and the generation it was taken
    against. `notHolding` is derived from `verdicts` here rather than passed
    in, so the receipt's own summary cannot disagree with the verdicts it
    summarises — the `_envelope` check re-derives it and refuses a body where
    the two differ, exactly as `erasure_receipt` refuses a tally that
    contradicts its rows.
    """
    observed = obs or {}
    return {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "hashAlg": HASH_ALG,
        "signAlg": SIGN_ALG,
        "composition": composition,
        "generation": generation,
        "issuedAt": issued_at,
        "signer": signer,
        "scope": {
            **SCOPE,
            "measurement": MEASUREMENT_SCOPE,
            "declared": scope,
        },
        "contract": {
            key: {"target": entry.get("target"),
                  "action": entry.get("action"),
                  "divertTo": entry.get("divertTo"),
                  "declared": bool(entry.get("declared"))}
            for key, entry in (contract or {}).items()
        },
        "verdicts": {
            key: {k: v for k, v in value.items() if k != "direction"}
            for key, value in (verdicts or {}).items()
        },
        "response": {
            "action": action,
            "breached": breached_keys(contract or {}, verdicts or {}),
            "notHolding": not_holding_keys(contract or {}, verdicts or {}),
        },
        "observed": {
            "samples": {
                "latencies": len(observed.get("latencies") or []),
                "durations": len(observed.get("durations") or []),
                "emissions": observed.get("emissions", 0),
                "events": observed.get("events", 0),
                "hostReportedLatencies": observed.get("hostReportedLatencies", 0),
            },
            "openAtEnd": list(observed.get("openAtEnd") or []),
        },
    }


def _mac(body: Mapping, key: bytes) -> str:
    """The receipt MAC: domain-tagged HMAC-SHA256 over the canonical body bytes
    (`body` is the receipt with its `signature` member removed).

    The bytes are `attest._canonical_bytes`'s, deferred to rather than
    restated, so a verifier who canonicalizes the way docs/revl-attest.md
    documents recomputes exactly these bytes."""
    return hmac.new(bytes(key), RECEIPT_DOMAIN + attest._canonical_bytes(
        {k: v for k, v in body.items() if k != SIGNATURE_FIELD}),
        hashlib.sha256).hexdigest()


def make_receipt(body: Mapping, key: bytes) -> dict:
    """Sign a body. A body with no canonical byte spelling raises
    `attest.NotCanonicalizable`, which the caller turns into a refusal rather
    than a crash — the same contract `erasure_receipt.make_receipt` keeps.
    There is no partially-signed receipt.

    `keyId` goes INSIDE the body before the MAC is taken, the way
    `erasure_receipt.build_body` binds it, for two reasons. It has to be inside
    for the signature to cover it, or the key identity a reader uses to pick a
    verification key would be the one member of the document anybody could
    rewrite. And it has to be inside for `verify_receipt` to recompute the same
    bytes at all: the verifier MACs the receipt it was handed, with only
    `signature` removed, so a member added after signing is a member the
    verifier includes and the signer did not — which makes every receipt fail
    verification as if it had been altered.
    """
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise ValueError("the SLO receipt signing key must be non-empty bytes")
    signed = {**body, "keyId": key_id(bytes(key))}
    return {**signed, SIGNATURE_FIELD: _mac(signed, bytes(key))}


def _envelope(receipt: Mapping) -> str:
    """Is this record even an SLO receipt of the shape this verifier accepts?
    Returns a refusal reason, or `""` when well formed.

    A MAC proves authorship; it does not prove that what was authored means
    what the reader assumes. So every member whose value carries a fixed
    meaning is checked here: a mislabelled document is refused, a verdict
    outside this protocol's vocabulary is refused rather than printed VALID,
    and a `response.notHolding` that is not the set of verdicts the body itself
    carries is refused — without that last check a signed body could name no
    breach while its own verdicts read `breached`, which is the one confusion
    this protocol exists to remove.
    """
    def reason(member, expected, found):
        return (f"envelope refused: {member} is {found!r}, expected "
                f"{expected!r}")

    if not isinstance(receipt, Mapping):
        return "envelope refused: the receipt is not a document"
    if receipt.get("kind") != RECEIPT_KIND:
        return reason("kind", RECEIPT_KIND, receipt.get("kind"))
    if receipt.get("version") != RECEIPT_VERSION:
        return reason("version", RECEIPT_VERSION, receipt.get("version"))
    if receipt.get("hashAlg") != HASH_ALG:
        return reason("hashAlg", HASH_ALG, receipt.get("hashAlg"))
    if receipt.get("signAlg") != SIGN_ALG:
        return reason("signAlg", SIGN_ALG, receipt.get("signAlg"))
    if not isinstance(receipt.get(SIGNATURE_FIELD), str):
        return "envelope refused: no signature to verify"
    generation = receipt.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        return reason("generation", "the integer generation this was issued "
                      "against", generation)
    contract = receipt.get("contract")
    if not isinstance(contract, Mapping):
        return reason("contract", "the declared SLO contract", contract)
    verdicts = receipt.get("verdicts")
    if not isinstance(verdicts, Mapping):
        return reason("verdicts", "one verdict per declared datum", verdicts)
    for key, value in verdicts.items():
        if not isinstance(value, Mapping):
            return reason(f"verdicts[{key!r}]", "a verdict", value)
        if value.get("verdict") not in VERDICTS:
            return reason(f"verdicts[{key!r}].verdict", VERDICTS,
                          value.get("verdict"))
    response = receipt.get("response")
    if not isinstance(response, Mapping):
        return reason("response", "the action taken", response)
    if response.get("action") not in ACTIONS:
        return reason("response.action", ACTIONS, response.get("action"))
    if not isinstance(response.get("breached"), list):
        return reason("response.breached", "a list of breached datum names",
                      response.get("breached"))
    if not isinstance(response.get("notHolding"), list):
        return reason("response.notHolding", "a list of datum names",
                      response.get("notHolding"))
    # The summary is the receipt's own reading of its own verdicts, and it is
    # the line a reader quotes ("breached: p95_latency"), so a summary that
    # disagrees with the verdicts it counts is refused. Both are inside the
    # signed body, so this is one member of a document checked against another.
    breached = [k for k, v in verdicts.items() if v.get("verdict") == BREACHED]
    unheld = [k for k, v in verdicts.items() if not satisfied(v)]
    if list(response.get("breached")) != breached:
        return ("envelope refused: response.breached is not this receipt's own "
                f"breached verdicts: it says {response.get('breached')!r}, the "
                f"verdicts say {breached!r}")
    if list(response.get("notHolding")) != unheld:
        return ("envelope refused: response.notHolding is not this receipt's own "
                f"unsatisfied verdicts: it says {response.get('notHolding')!r}, "
                f"the verdicts say {unheld!r}")
    return ""


def verify_receipt(receipt: Mapping, key: bytes) -> tuple[bool, str]:
    """Verify an SLO receipt against the signing key. Returns `(ok, reason)`;
    `reason` is `""` when `ok`.

    Three checks, in order, so the cheapest refusal wins and a caller can tell
    a wrong key from a tampered body:

      * the envelope — kind, version, algorithms, generation, the verdict
        vocabulary, and the response's own summary against the verdicts it
        summarises;
      * the key id, when the receipt carries one, so a wrong key is reported as
        a wrong key rather than as a tampered body;
      * the MAC, constant-time, over the canonical body.

    FAIL CLOSED: a receipt that fails any check is refused. There is no
    "valid but unverified" reading, and no path returns True for a document
    this function could not check.
    """
    refusal = _envelope(receipt)
    if refusal:
        return False, refusal
    if not isinstance(key, (bytes, bytearray)) or not key:
        return False, "no key to verify against"
    try:
        expected = _mac(receipt, bytes(key))
    except attest.NotCanonicalizable as error:
        return False, f"refused: {error}"
    if not hmac.compare_digest(str(receipt.get(SIGNATURE_FIELD)), expected):
        found = str(receipt.get("keyId") or "(no key id)")
        mine = key_id(bytes(key))
        if found != mine:
            return False, (f"signature refused: the receipt was signed by key "
                           f"{found}, this verifier holds {mine}")
        return False, ("signature refused: the body does not match the "
                       "signature (the receipt was altered after it was issued)")
    return True, ""


def attach_generation(entry: Mapping, receipt: Mapping) -> dict:
    """The receipt ON THE GENERATION — the exit criterion's last three words.

    `mcp/session.Session._record_generation` appends `{generation, snapshot,
    ir, origin}`; this returns that entry with the receipt added as a fifth
    member. Additive and conditional, so a generation nothing breached is
    byte-identical to the pre-473 entry, exactly as `to_ir` emits nothing for
    a composition with no `slo` block.

    The generation the receipt names and the generation it is attached to must
    agree. They are two readings of the same fact, and a receipt filed under a
    generation it was not issued against is the mislabelling `_envelope`
    refuses — so it is refused here too, before it is filed.
    """
    if not isinstance(receipt, Mapping) or receipt.get("kind") != RECEIPT_KIND:
        raise ValueError("attach_generation needs an SLO receipt")
    if entry.get("generation") != receipt.get("generation"):
        raise ValueError(
            f"receipt names generation {receipt.get('generation')!r}, the entry "
            f"is generation {entry.get('generation')!r}")
    return {**entry, "sloReceipt": dict(receipt)}


# ---------------------------------------------------------------------------
# 5. The ROLLOUT GATE, observed half — evidence E4 of
#    docs/design/473-slo-contracts.md.
#
#    The compile-time gate (`composition._check_slo_bounds`, slice 1) refuses a
#    composition whose own declarations contradict its objectives. That is a
#    self-consistency check and the design note says so plainly. THIS gate is
#    the other half of the item's exit clause: the generation a rollout is about
#    to replace produced a signed receipt, and that receipt is a MEASUREMENT.
#    If it says an objective the candidate still promises was breached, the
#    candidate is promising something the evidence already contradicts, and the
#    rollout is refused BY NAME.
# ---------------------------------------------------------------------------

#: Why a rollout was admitted or refused. Ranked, because the strength of the
#: gate is exactly the strength of its basis and a caller must be able to tell
#: "nothing contradicted this" from "this was checked against a measurement".
NO_CONTRACT = "no-contract"        #: the candidate declares no `slo` block
NO_EVIDENCE = "no-evidence"        #: no predecessor receipt was presented
UNVERIFIED = "unverified-receipt"  #: a receipt was presented and did not verify
WITNESSED = "predecessor-receipt"  #: a verified receipt was read

BASES = (NO_CONTRACT, NO_EVIDENCE, UNVERIFIED, WITNESSED)


class RolloutRefused(Exception):
    """The SLO gate refused a rollout. `report` is the structured refusal, the
    same document `gate_rollout` returns, so a caller that catches this and a
    caller that reads the report see the same bytes."""

    def __init__(self, report: Mapping):
        self.report = dict(report)
        super().__init__(render_gate(self.report))


def gate_rollout(*, ir: Mapping | None, receipt: Mapping | None = None,
                 key: Optional[bytes] = None) -> dict:
    """The rollout verdict for one candidate composition against the receipt of
    the generation it would replace. Pure: no clock, no I/O, no latch.

    The candidate's contract is read from its own IR (`contract_from_ir`), so
    the objectives gated here are the objectives the compile-time gate already
    admitted, not a second parse that could disagree.

    Refusal rule, one sentence: a datum the candidate STILL declares, whose
    `breached` observation in the predecessor's receipt would ALSO breach the
    candidate's own target, refuses the rollout.

    Each clause of that rule is load bearing, so each is stated:

      * *still declares* — a candidate that dropped the objective is promising
        nothing about it, and refusing on an objective nobody claimed would be
        refusing a document for a sentence it does not contain.
      * *breached* — `insufficient` and `unmeasurable` are NOT refusals. They
        are "nobody knows", and a gate that refused on them would refuse every
        rollout of a contract this tree cannot measure, which is three of the
        five datums. They ARE reported, under `notHolding`, so the admission is
        never mistaken for a clean bill.
      * *would ALSO breach the candidate's target* — the comparison is re-run
        against the NEW target with the datum's own direction
        (`parser.SLO_DIRECTION`). A candidate that widened `p95_latency` past
        the witnessed value is no longer contradicted by it and is admitted,
        with the widening recorded. That is not a loophole; it is the author
        withdrawing a promise in the source, in public, where a reviewer sees
        it — as against silently breaching it in production.

    Evidence discipline, fail closed in one direction only. A receipt PRESENTED
    with a key it does not verify against is a REFUSAL (`unverified-receipt`):
    evidence that cannot be checked is not evidence, and admitting on it would
    let a forged receipt buy an admission. The ABSENCE of a receipt is not a
    refusal (`no-evidence`): the first generation of any composition has no
    predecessor, and a gate that refused it would refuse every first rollout.
    The basis is in the report either way, so nothing has to infer which
    happened.
    """
    from .parser import SLO_DIRECTION, slo_datum

    contract = contract_from_ir(ir)
    report: dict = {
        "gate": "slo-rollout",
        "admitted": True,
        "basis": NO_CONTRACT,
        "generation": None,
        "composition": None,
        "refusals": [],
        "notHolding": [],
        "relaxed": [],
    }
    if not contract:
        report["note"] = ("the candidate declares no `slo` block, so this gate "
                          "is inert and refuses nothing")
        return report
    report["declared"] = sorted(contract)
    if not isinstance(receipt, Mapping) or not receipt:
        report["basis"] = NO_EVIDENCE
        report["note"] = (
            "no predecessor receipt was presented, so there is no measurement "
            "to contradict the candidate's objectives; this admission is the "
            "absence of evidence and not evidence of absence")
        return report
    if key:
        ok, reason = verify_receipt(receipt, key)
        if not ok:
            report["admitted"] = False
            report["basis"] = UNVERIFIED
            report["refusals"] = [{"reason": reason}]
            report["note"] = ("a receipt was presented as evidence and could "
                              "not be verified; an unverifiable receipt is "
                              "refused rather than ignored")
            return report
    envelope = _envelope(receipt)
    if envelope:
        report["admitted"] = False
        report["basis"] = UNVERIFIED
        report["refusals"] = [{"reason": envelope}]
        report["note"] = ("the presented receipt is not a well-formed "
                          "`revl.slo-receipt`")
        return report
    report["basis"] = WITNESSED
    report["generation"] = receipt.get("generation")
    report["composition"] = receipt.get("composition")
    verdicts = receipt.get("verdicts") or {}
    for datum_key, entry in contract.items():
        observed_entry = verdicts.get(datum_key)
        if not isinstance(observed_entry, Mapping):
            continue
        verdict = observed_entry.get("verdict")
        if verdict != BREACHED:
            if verdict in NOT_HOLDING:
                report["notHolding"].append({
                    "datum": datum_key, "verdict": verdict,
                    "reason": observed_entry.get("reason", "")})
            continue
        observed = observed_entry.get("observed")
        target = entry.get("target")
        direction = SLO_DIRECTION.get(slo_datum(datum_key) or datum_key,
                                      "upper")
        again = _decide(observed, target, direction,
                        n=observed_entry.get("samples", 0))
        witness = {
            "datum": datum_key,
            "observed": observed,
            "target": target,
            "previousTarget": observed_entry.get("target"),
            "direction": direction,
            "samples": observed_entry.get("samples", 0),
            "generation": receipt.get("generation"),
        }
        if again["verdict"] == BREACHED:
            report["refusals"].append(witness)
        else:
            report["relaxed"].append(witness)
    if report["refusals"]:
        report["admitted"] = False
        report["note"] = (
            "the generation this rollout replaces measurably breached an "
            "objective the candidate still declares")
    return report


def admit_rollout(*, ir: Mapping | None, receipt: Mapping | None = None,
                  key: Optional[bytes] = None) -> dict:
    """`gate_rollout`, raising `RolloutRefused` on a refusal.

    Two functions rather than one for the same reason `Monitor` splits
    `evaluate` from `observe`: the verdict is a pure document a test and a
    report can read, and the refusal is the imperative act a rollout path must
    not be able to ignore by forgetting to look at a returned dict.
    """
    report = gate_rollout(ir=ir, receipt=receipt, key=key)
    if not report["admitted"]:
        raise RolloutRefused(report)
    return report


def render_gate(report: Mapping) -> str:
    """The rollout verdict for a terminal. The refusal names the datum, both
    numbers and the generation the witness came from, because a gate that says
    only "refused" makes an operator go find out why by hand."""
    if not isinstance(report, Mapping):
        return "error: not an SLO rollout verdict"
    head = ("SLO ROLLOUT GATE: admitted" if report.get("admitted")
            else "SLO ROLLOUT GATE: REFUSED")
    out = [f"{head}  (basis: {report.get('basis')})"]
    if report.get("generation") is not None:
        out.append(f"  witness: generation {report['generation']}"
                   + (f" of {report['composition']}"
                      if report.get("composition") else ""))
    for refusal in report.get("refusals") or []:
        if "datum" not in refusal:
            out.append(f"  refused: {refusal.get('reason')}")
            continue
        out.append(
            f"  refused: `{refusal['datum']}` was observed "
            f"{refusal.get('observed')!r} against the candidate's declared "
            f"{refusal.get('target')!r} "
            f"({refusal.get('samples', 0)} sample(s), generation "
            f"{refusal.get('generation')})")
    for relaxed in report.get("relaxed") or []:
        out.append(
            f"  admitted: `{relaxed['datum']}` was observed "
            f"{relaxed.get('observed')!r}, which the candidate's declared "
            f"{relaxed.get('target')!r} now permits (previously "
            f"{relaxed.get('previousTarget')!r})")
    for unheld in report.get("notHolding") or []:
        out.append(f"  not refused, not measured: `{unheld['datum']}` "
                   f"({unheld.get('verdict')}) {unheld.get('reason', '')}")
    if report.get("note"):
        out.append(f"  {report['note']}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 6. The declared action, dispatched.
# ---------------------------------------------------------------------------

class DivertRefused(Exception):
    """A divert could not be taken. Carries the reason, which the monitor puts
    in the receipt: a fallback that did not admit is a fact about the run and
    hiding it behind a pause would be reporting a stop it did not perform."""


def admit_divert(*, composition: str, component: str, fallback: str,
                 root: Optional[str] = None, overlay: Optional[dict] = None,
                 detail: Optional[Mapping] = None, **kwargs) -> dict:
    """The default divert applier: swap the breaching component's provider for
    `fallback` and RE-ADMIT the composition through the ordinary gate.

    This is what "divert to a declared fallback provider" means in a language
    whose compositions are compiled documents: the fallback is a provider
    SOURCE, and the divert is a re-resolution of the row table with that row's
    `from "..."` pointing at it. Nothing is hot-patched: the candidate goes
    through `composition._admit_full`, the same gate a `revl swap` runs, so a
    fallback that does not provide the key (G2), does not provide it in the
    same realm (G3), or drifts the interface is REFUSED here — and a refused
    divert is raised as `DivertRefused` rather than silently reported as taken.
    That is the property that makes a divert safe to declare: it cannot put an
    unadmitted composition into service.

    `component` is PROVENANCE, never identity (426 §1.5), so the row is found
    by which component it resolves to and then located in the declaration by
    its LABEL. A composition whose row came from a stack layer has no base
    declaration to rewrite and refuses by name.

    Returns the admission dossier: the new row table's IR, the document the
    gate produced, and the row that was diverted.
    """
    from . import composition as comp
    from .parser import parse_file

    if not composition or not os.path.exists(composition):
        raise DivertRefused(f"no composition document at {composition!r}")
    doc_path = os.path.abspath(composition)
    base = os.path.abspath(root or os.path.dirname(doc_path) or os.getcwd())
    target = _resolve_fallback(fallback, doc_path, base)
    if target is None:
        raise DivertRefused(f"the declared fallback {fallback!r} names no file "
                            f"beside {os.path.basename(doc_path)} or under "
                            f"{base}")
    program = parse_file(doc_path)
    decl = comp.sole_composition(program, doc_path)
    layered = bool(decl.stack) or decl.site is not None or bool(overlay)

    def resolve_now():
        if layered:
            return comp.fold(decl, doc_path, base, overlay)
        return comp.resolve(decl, doc_path, base)

    table = resolve_now()
    matches = [row for row in table.rows if row.component == component]
    if not matches:
        raise DivertRefused(
            f"the composition has no row resolving to component "
            f"{component!r} (rows: {sorted(r.label for r in table.rows)})")
    if len(matches) > 1:
        raise DivertRefused(
            f"{len(matches)} rows resolve to component {component!r} "
            f"({sorted(r.label for r in matches)}); a divert names exactly one")
    label = matches[0].label
    declared = [row for row in decl.rows if row.label == label]
    if len(declared) != 1:
        raise DivertRefused(
            f"row `{label}` is not declared by the base composition (it came "
            f"from a stack layer), so there is no `from` clause to divert")
    declared[0].path = fallback
    try:
        new_table = resolve_now()
        document = comp._admit_full(new_table, base, **kwargs)
    except Exception as error:            # noqa: BLE001 — reported, not raised
        raise DivertRefused(
            f"the fallback {fallback!r} did not admit as a replacement for "
            f"`{label}`: {error}") from error
    return {
        "taken": True,
        "component": component,
        "row": label,
        "fallback": fallback,
        "resolvedFallback": target,
        "ir": new_table.to_ir(),
        "document": document,
    }


def _resolve_fallback(fallback: str, doc_path: str, root: str) -> Optional[str]:
    """Where `divert "..."` points. Beside the composition document first,
    which is what an author writing `divert "standby.rvl"` means, then under
    the composition root, which is the rule a row's `from` clause follows. An
    absolute path is taken as written. None when neither exists, so the
    refusal can name both places it looked."""
    if not isinstance(fallback, str) or not fallback:
        return None
    if os.path.isabs(fallback):
        return fallback if os.path.exists(fallback) else None
    beside = os.path.join(os.path.dirname(doc_path), fallback)
    if os.path.exists(beside):
        return beside
    under = os.path.join(root, fallback)
    return under if os.path.exists(under) else None


def pause(*, latch: Optional[str], wal: Optional[str] = None,
          reason: str = "", operator: str = "revl.slo",
          detail: Optional[Mapping] = None, now=None) -> dict:
    """The PAUSE path: the E-Stop latch's first move, under a different verdict.

    Item 443's latch is the one mechanism in revl that stops a running
    composition dispatching new boundary crossings without unwinding anything,
    so a pause reuses it rather than inventing a second stop. `estop.read_latch`
    and `runtime._latch_record` treat ANY present latch as in force, so writing
    this record genuinely halts dispatch — including the `verdict` member,
    which they do not read. The members they DO read (`halted`, `reason`,
    `operator`) are the same shape, which is why a pause needs no change to
    either reader.

    The difference from `halt` is `resumable` and what it means:

      * `pause` — `resumable: True`. Nothing was stranded; every registered
        entry is still owed and still recoverable, so clearing the latch and
        booting a fresh process is the way back. This is the bounded-residue
        floor, and it is why it is the default.
      * `halt` — `resumable: False`, the item-443 reading: the instance is
        DEAD, its entries are STRANDED, and the way back is
        `revl recover --wal <file>`.

    A latch already in force is NOT overwritten: the first stop's reason is the
    one that survives, the same idempotence `revl estop` and
    `runtime.estop` keep. Returns the report, with `armed` saying whether this
    call is the one that wrote it.
    """
    from . import estop

    record = {
        "halted": True,
        "verdict": "paused",
        "reason": reason or "slo breach",
        "operator": operator,
        "at": time.time() if now is None else now,
        "wal": wal,
        "resumable": True,
        "reconcile": (f"revl recover --wal {wal}" if wal
                      else "revl recover --wal <file>"),
    }
    if detail:
        record["slo"] = dict(detail)
    path = estop.latch_path(latch, wal, env=False)
    if path is None:
        return {"armed": False, "latch": None, "reason": "no latch path to "
                "write; a pause needs --latch FILE (or --wal FILE, which "
                "derives FILE.estop)", **record}
    existing = estop.read_latch(path)
    if existing is not None:
        return {"armed": False, "latch": path, "alreadyHalted": True,
                **existing}
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
    except OSError as error:
        return {"armed": False, "latch": path,
                "reason": f"cannot write the pause latch: {error}", **record}
    return {"armed": True, "latch": path, **record}


def halt(*, latch: Optional[str], wal: Optional[str] = None, reason: str = "",
         operator: str = "revl.slo", detail: Optional[Mapping] = None,
         now=None) -> dict:
    """The HALT path: the item-443 E-Stop exactly, byte-for-byte the record
    `revl estop` arms, so `revl estop --report` reads it back unchanged and the
    way back is `revl recover --wal <file>`. The instance is dead; entries are
    stranded."""
    from . import estop

    record = {
        "halted": True,
        "verdict": "halted",
        "reason": reason or "slo breach",
        "operator": operator,
        "at": time.time() if now is None else now,
        "wal": wal,
        "resumable": False,
        "reconcile": (f"revl recover --wal {wal}" if wal
                      else "revl recover --wal <file>"),
    }
    if detail:
        record["slo"] = dict(detail)
    path = estop.latch_path(latch, wal, env=False)
    if path is None:
        return {"armed": False, "latch": None, "reason": "no latch path to "
                "write; a halt needs --latch FILE (or --wal FILE, which "
                "derives FILE.estop)", **record}
    existing = estop.read_latch(path)
    if existing is not None:
        return {"armed": False, "latch": path, "alreadyHalted": True,
                **existing}
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
    except OSError as error:
        return {"armed": False, "latch": path,
                "reason": f"cannot write the halt latch: {error}", **record}
    return {"armed": True, "latch": path, **record}


# ---------------------------------------------------------------------------
# 7. The monitor — evaluate, decide, act, receipt.
# ---------------------------------------------------------------------------

class Monitor:
    """Watches one running composition's declared SLO contract and dispatches
    the declared action on a live breach, with a signed receipt bound to the
    running generation.

    Deliberately a pure function with a thin imperative shell. `evaluate` takes
    an event list and returns the whole verdict — no clock, no I/O, no global
    state — which is what makes the contract testable from a recorded trace
    rather than only from a live run. `observe` is the in-run shell that calls
    it, and does nothing at all when no contract was declared.

    A monitor with no contract is INERT: `contract` is `{}`, `observe` returns
    None before reading an event, and no latch is written. That is the
    byte-identical property for a composition declaring no `slo` block.
    """

    def __init__(self, contract: Mapping | None = None, *,
                 composition: Optional[str] = None, generation: int = 0,
                 latch: Optional[str] = None, wal: Optional[str] = None,
                 key: Optional[bytes] = None, signer: Optional[str] = None,
                 divert=None, events=None, now=None):
        self.contract = dict(contract or {})
        self.composition = composition
        self.generation = generation
        self.latch = latch
        self.wal = wal
        self.key = key
        self.signer = signer
        # The divert applier. `None` means the module default
        # (`admit_divert`), and the caller passes a callable to substitute one.
        self.divert = divert
        self.events = list(events or [])
        self.now = now
        self.receipt: Optional[dict] = None
        self.action: Optional[str] = None
        self.dispatch: Optional[dict] = None

    @property
    def declared(self) -> bool:
        return bool(self.contract)

    def evaluate(self, events=None) -> Optional[dict]:
        """The whole verdict for one event list, or None when the composition
        declared no contract. Pure.

        The action is the declared one for the FIRST breached datum in
        declaration order — the contract is a set of objectives, and the most
        conservative reading of "the declared action" is the one the author
        wrote for the objective that failed. `response.action` records which
        datum it came from, so a reader never has to guess.
        """
        if not self.declared:
            return None
        obs = observations(self.events if events is None else events)
        verdicts = measure(self.contract, obs)
        hits = breached_keys(self.contract, verdicts)
        action = DEFAULT_ACTION
        if hits:
            action = self.contract[hits[0]].get("action") or DEFAULT_ACTION
        body = build_body(
            composition=self.composition, generation=self.generation,
            contract=self.contract, verdicts=verdicts, action=action,
            scope={"declaredAt": self.composition}, obs=obs,
            issued_at=attest._now_iso(self.now), signer=self.signer)
        return {"observations": obs, "verdicts": verdicts, "breached": hits,
                "notHolding": not_holding_keys(self.contract, verdicts),
                "action": action, "body": body}

    def observe(self, events=None) -> Optional[dict]:
        """The in-run shell: evaluate, then dispatch the declared action, then
        sign the receipt.

        Ordering is the contract. The receipt is built from the verdicts BEFORE
        the action is taken, so what the receipt describes is the observation
        that caused the action rather than the state after it. The action is
        then dispatched, and its outcome (`armed`, `latch`, `taken`, or the
        refusal reason) is folded in as `response.dispatch` BEFORE signing — so
        the receipt states what actually happened, not what was intended, and a
        divert that did not admit is signed as a divert that did not admit.

        Returns None, and touches nothing, when no contract was declared.
        """
        verdict = self.evaluate(events)
        if verdict is None:
            return None
        if not verdict["breached"]:
            self.receipt = None
            return verdict
        dispatch = self._dispatch(verdict)
        verdict["dispatch"] = dispatch
        verdict["body"]["response"]["dispatch"] = dispatch
        if self.key:
            try:
                self.receipt = make_receipt(verdict["body"], self.key)
            except attest.NotCanonicalizable as error:
                verdict["receiptRefused"] = str(error)
                self.receipt = None
        return verdict

    def _dispatch(self, verdict: Mapping) -> dict:
        """Take the declared action. Every branch reports what it did, and the
        report is what gets signed — including the branches that did nothing,
        because "we breached and took no action" must be visible rather than
        absent."""
        action = verdict["action"]
        reason = self._reason(verdict)
        detail = {
            "composition": self.composition,
            "generation": self.generation,
            "breached": list(verdict["breached"]),
            "notHolding": list(verdict["notHolding"]),
            "action": action,
        }
        self.action = action
        if action == DIVERT:
            applier = self.divert or admit_divert
            target = self.contract[verdict["breached"][0]].get("divertTo")
            try:
                result = applier(
                    composition=self.composition,
                    component=self._component(verdict),
                    fallback=target, detail=detail)
            except DivertRefused as error:
                # A declared fallback that does not admit is a REFUSAL, not a
                # pause: the run continues on the primary and the receipt says
                # so. Silently pausing here would report a stop the operator
                # never declared.
                self.dispatch = {"action": DIVERT, "taken": False,
                                 "reason": str(error), "divertTo": target}
                return self.dispatch
            self.dispatch = {"action": DIVERT, "taken": True,
                             "divertTo": target,
                             "row": (result or {}).get("row")}
            return self.dispatch
        writer = halt if action == HALT else pause
        result = writer(latch=self.latch, wal=self.wal, reason=reason,
                        detail=detail, now=self.now)
        self.dispatch = {"action": action, **result}
        return self.dispatch

    def _component(self, verdict: Mapping) -> str:
        """Which component the breach is attributed to — the component that
        produced the observation the breached datum was read from. Only the
        per-component datums can name one; a datum measured run-wide names the
        composition, and a divert on it is refused by `admit_divert` rather
        than guessed at."""
        obs = verdict["observations"]
        by = obs.get("emissionsByComponent") or {}
        if len(by) == 1:
            return next(iter(by))
        if self.composition:
            return os.path.splitext(os.path.basename(self.composition))[0]
        return ""

    def _reason(self, verdict: Mapping) -> str:
        """The breach, in one line, naming the field and both numbers — the
        line an operator reading the latch file needs."""
        parts = []
        for key in verdict["breached"]:
            entry = verdict["verdicts"][key]
            observed = entry.get("observed")
            parts.append(f"{key} {observed!r} vs declared {entry.get('target')!r}")
        return ("slo breach: " + "; ".join(parts)
                + f" (generation {self.generation}"
                + (f", {os.path.basename(self.composition)}"
                   if self.composition else "") + ")")


# ---------------------------------------------------------------------------
# 8. Rendering — the auditor's readable view. The structured receipt is the
#    product; this states scope first, because a reader who cannot check a
#    claim cannot use it.
# ---------------------------------------------------------------------------

def _not_taken(dispatch: Mapping) -> str:
    """The `[not taken: ...]` suffix, or `""` when the action WAS taken.

    Keyed off `taken`/`armed` and never off the presence of a `reason`: a
    latch that armed successfully carries the breach reason it was armed FOR,
    so reading a present `reason` as a failure prints "not taken" on exactly
    the dispatches that were taken — which is the one line an operator reading
    a receipt would act on backwards."""
    took = dispatch.get("taken", dispatch.get("armed", True))
    if took:
        return ""
    return f" [not taken: {dispatch.get('reason') or 'no reason recorded'}]"


def render(document: Mapping) -> str:
    """Render an evaluation or a receipt for a terminal."""
    if not isinstance(document, Mapping):
        return "error: not an SLO document"
    if document.get("kind") == RECEIPT_KIND:
        return render_receipt(document)
    verdicts = document.get("verdicts")
    if not isinstance(verdicts, Mapping):
        return "error: not an SLO document"
    out = ["SLO: observed contract"]
    for key, value in verdicts.items():
        observed = value.get("observed")
        out.append(
            f"  {value.get('verdict'):<13} {key:<20} "
            f"observed {observed!r} vs target {value.get('target')!r} "
            f"({value.get('samples', 0)} sample(s))")
        if value.get("reason"):
            out.append(f"                {value['reason']}")
    breached = document.get("breached") or []
    out.append("")
    if breached:
        out.append(f"  BREACHED: {', '.join(breached)} -> "
                   f"{document.get('action')}")
    else:
        out.append("  no breach")
    dispatch = document.get("dispatch")
    if isinstance(dispatch, Mapping):
        out.append(f"  action: {dispatch.get('action')}"
                   + (f" -> {dispatch.get('divertTo')}"
                      if dispatch.get("divertTo") else "")
                   + (f" ({dispatch.get('latch')})" if dispatch.get("latch")
                      else "")
                   + _not_taken(dispatch))
    return "\n".join(out)


def render_receipt(receipt: Mapping) -> str:
    """Human rendering of a signed receipt."""
    if not isinstance(receipt, Mapping) or receipt.get("kind") != RECEIPT_KIND:
        return "error: not a revl.slo-receipt document"
    scope = receipt.get("scope") or {}
    out = [
        f"SLO RECEIPT: {receipt.get('composition')}",
        f"  {receipt.get('kind')} v{receipt.get('version')}",
        f"  issued {receipt.get('issuedAt')}"
        + (f" by {receipt['signer']}" if receipt.get("signer") else ""),
        f"  key {receipt.get('keyId')}",
        f"  generation {receipt.get('generation')}",
        "",
        f"  {scope.get('title', '')}",
        "  PROVES:",
    ]
    out += [f"    + {line}" for line in scope.get("proves") or []]
    out.append("  DOES NOT PROVE:")
    out += [f"    - {line}" for line in scope.get("doesNotProve") or []]
    out.append(f"    ({scope.get('reference', '')})")
    if scope.get("measurement"):
        out.append(f"  MEASUREMENT: {scope['measurement']}")
    out += ["", "  declared:"]
    for key, entry in (receipt.get("contract") or {}).items():
        divert = f" -> {entry.get('divertTo')}" if entry.get("divertTo") else ""
        default = "" if entry.get("declared") else " (default)"
        out.append(f"    {key:<20} {entry.get('target')!r}  on breach "
                   f"{entry.get('action')}{divert}{default}")
    out += ["", "  observed:"]
    for key, value in (receipt.get("verdicts") or {}).items():
        out.append(f"    {value.get('verdict'):<13} {key:<20} "
                   f"observed {value.get('observed')!r} vs target "
                   f"{value.get('target')!r} ({value.get('samples', 0)} sample(s))")
        if value.get("reason"):
            out.append(f"                  {value['reason']}")
    response = receipt.get("response") or {}
    out += [
        "",
        f"  action: {response.get('action')}",
        f"  breached: {', '.join(response.get('breached') or []) or 'none'}",
        f"  not holding: "
        f"{', '.join(response.get('notHolding') or []) or 'none'}",
    ]
    dispatch = response.get("dispatch")
    if isinstance(dispatch, Mapping):
        out.append(f"  dispatched: {dispatch.get('action')}"
                   + (f" -> {dispatch.get('divertTo')}"
                      if dispatch.get("divertTo") else "")
                   + (f" ({dispatch.get('latch')})" if dispatch.get("latch")
                      else "")
                   + _not_taken(dispatch))
    samples = (receipt.get("observed") or {}).get("samples") or {}
    out.append("  samples: " + ", ".join(f"{k} {v}" for k, v in samples.items()))
    out.append(f"  signature: {receipt.get(SIGNATURE_FIELD)}")
    return "\n".join(out)

"""Replay-mode readiness over a durable WAL (roadmap item 250, Slice 3b, the
offline half).

Slice 3a (issue #87, the LLM-aware WAL) made ONE model decision durable per
completion crossing: a ``model-decision`` record carrying the trace hop's
payload (model, tokens, cost, latency bracket, attempts) keyed on the
completion's own ``effect`` record. The roadmap item then asks for the modes
that record enables, ``revl replay branch`` in exact / tool-only /
model-substitute / counterfactual mode: re-execute a branch, or ask what would
have happened if it had acted differently.

Re-executing anything needs a LIVE component, a workspace handle and a fiber,
which an offline reader does not have, exactly as :mod:`revl.branch` states of
its own enumerate-and-compare surface. This module is the honest offline half
of Slice 3b: given a WAL, it says, per mode, whether that mode could be run
from what this WAL records, and names precisely what is present and what is
missing. It reads through :func:`revl.wal.read_wal` with no backend on the
path, so a WAL written by any tier's runtime plans identically.

The live executor (``revl replay branch`` actually re-running a branch, and the
substitution seam a model swap needs) is the second half of Slice 3b and lives
in the run/session machinery, not here; it depends on Slice 3a's record being
on the WAL. This module names the requirements that executor must satisfy, and
refuses to pretend an offline reader can meet them.

What this module does NOT do, and will not pretend to
-----------------------------------------------------
It never RUNS a replay. It reports readiness. A mode is reported ``executable:
false`` on every offline plan, because execution needs the live driver; the
useful signal is ``plannable`` (does the WAL carry enough to inform this mode
at all) and the per-mode ``missing`` list (what the live executor would still
need). Emptying a ``missing`` list is a claim the WAL now carries that input.

What item 517 Slice 3 adds: the DECISION, read back
---------------------------------------------------
Re-EXECUTING a branch still needs the live driver, and that has not changed.
But the roadmap item 517 exit clause asks for something an offline reader can
in fact do — "a recorded run replays a model decision from the artifact alone"
— and until Slice 2 there was nothing on the WAL to do it with. Slice 3a's
record says a completion happened, which model answered and what it cost; it
is silent by design about the prompt binding, the request parameters and the
placement, so no reader could say WHY the run decided as it did.

Since item 517 Slice 2 a run may seal an evidence object onto that same record
(``evidence``, `revl.model_evidence`). :func:`plan` verifies it and rebuilds
the decision here: the placement, what answered, what it was given, what it
could have said and what it said, how it was asked, under which rule. That is
a replay of the DECISION, not of the run, and the distinction is kept in the
output rather than blurred — ``executable`` is still false for every mode.

Fail-closed, in both directions:

* a record whose MAC does not check out yields NO reading, not a reading with
  a caveat. Editing a field on disk does not produce a wrong answer here, it
  produces no answer (:func:`revl.model_evidence.reconstruct`);
* with no key supplied the plan reports the seal as PRESENT and UNVERIFIED and
  reads nothing out of it. An unverified seal is not evidence, and
  ``sealedEvidence`` is met only by one that verified.

A WAL whose decisions carry no evidence at all reads as "not sealed", which
this module states in the same voice as :data:`PRE_3A_NOTE`: it reports what is
on the record and never what a run might have done off it.
"""

from __future__ import annotations

from typing import Optional

from .model_evidence import (WAL_EVIDENCE_MEMBER, WAL_REFUSAL_MEMBER,
                             from_wal_record, reconstruct)
from .wal import WALIntegrityError, read_wal

#: The record kind Slice 3a writes for one durable model decision. Named here as
#: a literal rather than imported from :mod:`revl.wal` so this module reads a
#: model-aware WAL correctly whether or not the reader's constant has landed
#: yet; it is byte-identical to ``revl.wal.RECORD_MODEL_DECISION`` and
#: ``replay.RECORD_MODEL_DECISION`` (pinned by the Slice 3a agreement test), and
#: a drift there would leave decisions invisible here too, which the readiness
#: report would then state as "no model decision recorded".
RECORD_MODEL_DECISION = "model-decision"

#: The four replay modes the roadmap item names, ordered by how much of the
#: record each one needs. ``exact`` is the most demanding (reproduce the run
#: without calling the model at all); ``counterfactual`` is the most valuable
#: (what would have happened if it had acted differently).
MODES = ("exact", "tool-only", "model-substitute", "counterfactual")


class ReplayPlanError(RuntimeError):
    """A WAL could not be read, or a replay question cannot be answered from it."""


#: The inputs a replay mode can require, and what each one means. A mode's
#: requirement is met by the WAL only when the corresponding record is present;
#: the four keys the Slice 3a record deliberately does NOT carry
#: (``responseText``, ``promptDigest``, ``toolCalls``, ``seedsAndClock``) are
#: named so a reader sees why a mode is out of reach rather than inferring it
#: from silence. ``liveComponent`` is never met offline, by construction.
REQUIREMENTS = {
    "modelDecisions":
        "one durable model-decision record per completion crossing (item 250 "
        "Slice 3a): which model answered, at what token/cost, in how many "
        "attempts. Present on any WAL written by a Slice-3a runtime that "
        "crossed a model boundary.",
    "responseText":
        "the model's response, needed to feed a recorded completion back "
        "instead of calling the model. Never written to the WAL (Slice 3a "
        "records the decision, never the text).",
    "promptDigest":
        "a digest of the prompt, needed to bind a substituted call to the "
        "original request. The Slice-3a record does not carry one: its "
        "suppression gate is the compile-side taint certificate the driver "
        "holds (item 444), and a digest without that gate is the confirmation "
        "oracle item 121 closes. Met only by a VERIFIED item 517 evidence "
        "object whose prompt binding is `content-addressed` - the one mode "
        "that is unsalted and therefore comparable across runs, and which that "
        "object may carry only when its declared origins prove the input was "
        "neither secret nor confidential. A `salted-within-run` binding does "
        "NOT meet it: it is stable within one run and meaningless across "
        "runs, so a cross-run replay bound to it is bound to nothing.",
    "sealedEvidence":
        "a sealed `revl.model-decision` evidence object on the crossing (item "
        "517), carrying the placement, the prompt binding, the candidate set, "
        "the sampling parameters and the policy in force, MAC-covered and "
        "verified against a key the reader holds. This is what lets the "
        "decision itself be reconstructed from the artifact alone. Met only "
        "when a seal is present AND verified: an unverified seal is not "
        "evidence, and no key is not a pass.",
    "toolCalls":
        "the tool calls the model requested, needed to replay tool-use turns. "
        "Made inside the opaque host body; revl does not see them and Slice 3a "
        "does not record them.",
    "seedsAndClock":
        "the RNG seed and clock readings, needed for a bit-reproducible replay. "
        "Not recorded, so a replay is a divergent continuation, not a "
        "reproduction (the seedsAndClock axis of a branch's notPreserved).",
    "liveComponent":
        "a live component, workspace handle and fiber to re-execute against. An "
        "offline reader has none; this is the requirement the Slice-3b live "
        "executor exists to meet.",
}

#: Per mode, the requirements that must be ON THE RECORD for the mode to be
#: PLANNABLE from this WAL. Ordered most-fundamental first, so the first unmet
#: one is the headline blocker. These gate ``plannable``: if the WAL is missing
#: any of them, no live executor can supply it after the fact.
MODE_RECORD_REQUIRES = {
    "exact": ("modelDecisions", "responseText", "seedsAndClock"),
    "tool-only": ("modelDecisions", "responseText", "toolCalls"),
    "model-substitute": ("modelDecisions",),
    "counterfactual": ("modelDecisions",),
}

#: Per mode, the requirements a plannable mode still needs at EXECUTION time,
#: which the live Slice-3b executor supplies rather than the WAL. Reported in a
#: mode's ``missing`` so the contract is visible, but they do NOT gate
#: ``plannable``: ``liveComponent`` is the component itself; for
#: model-substitute the prompt is re-derived from the re-executed branch state,
#: not read off the record; for counterfactual the absent ``seedsAndClock`` is a
#: caveat (a counterfactual with no recorded seed is a divergent continuation,
#: not a controlled A/B) rather than a hard blocker.
MODE_EXECUTOR_REQUIRES = {
    "exact": ("liveComponent",),
    "tool-only": ("liveComponent",),
    "model-substitute": ("liveComponent",),
    "counterfactual": ("seedsAndClock", "liveComponent"),
}


def _mode_requires(mode: str) -> tuple:
    """Every requirement a mode names, record-side then executor-side."""
    return MODE_RECORD_REQUIRES[mode] + MODE_EXECUTOR_REQUIRES[mode]

#: What a mode achieves, one line, so a plan explains the modes it reports on.
MODE_INTENT = {
    "exact": "reproduce the run byte-for-byte without calling the model",
    "tool-only": "replay recorded model responses as fixtures, re-run tools live",
    "model-substitute": "swap the model, feed it the original prompts, re-run "
                        "from the fork",
    "counterfactual": "ask what would have happened if the agent had acted "
                      "differently",
}

#: The reader's blind spot, stated on every plan: a WAL with no model-decision
#: record cannot be told apart from a run that made no model completion. Both
#: read as "no decision recorded", which is the honest floor for every mode.
PRE_3A_NOTE = (
    "a WAL with no `model-decision` record reads as 'this session made no model "
    "completion'. A WAL written before item 250 Slice 3a made the record "
    "durable reads the same way, and this reader cannot tell the two apart: it "
    "reports readiness from what is on the record, never from what a run might "
    "have done off it."
)

#: Stated on every plan alongside the reconstructed decisions: reading a
#: decision back is not re-running it. Item 517's exit clause is met by the
#: first and says nothing about the second.
DECISION_NOTE = (
    "a reconstructed decision is the account of what the model was asked and "
    "what it chose, verified against its own seal. It is not a re-execution: "
    "no model is called, no tool runs and no completion text is read, because "
    "none is written. `executable` stays false for every mode."
)

#: Stated on every plan, the way :mod:`revl.branch` states it: an offline reader
#: runs nothing. Every mode is `executable: false` here; the live executor is
#: the second half of Slice 3b.
OFFLINE_NOTE = (
    "this is an offline readiness plan, not a replay. It runs nothing: an "
    "offline reader has no live component, so every mode reads `executable: "
    "false` and the live Slice-3b executor is what would satisfy the "
    "`liveComponent` requirement. `plannable` says whether the WAL carries "
    "enough to inform the mode at all."
)


def _load(path: str) -> dict:
    try:
        return read_wal(path)
    except OSError as error:
        raise ReplayPlanError(f"cannot read WAL {path}: {error}") from None
    except WALIntegrityError as error:
        raise ReplayPlanError(str(error)) from None


def _model_decisions(records: list) -> list:
    """The WAL's ``model-decision`` records, in recorded order. Read by kind, so
    a WAL written before the reader's constant landed still reads correctly."""
    return [r for r in records if r.get("record") == RECORD_MODEL_DECISION]


def _decision_entry(record: dict) -> dict:
    """One model decision as a plan entry: the crossing it names and the model
    and cost it recorded, which is the substitution surface a model-substitute
    or counterfactual replay would work from."""
    llm = record.get("llm") or {}
    return {
        "component": record.get("component"),
        "stepIndex": record.get("stepIndex"),
        "outcome": record.get("outcome"),
        "model": llm.get("model"),
        "tokensIn": llm.get("tokensIn"),
        "tokensOut": llm.get("tokensOut"),
        "cost": llm.get("cost"),
        "attempts": llm.get("attempts"),
        "attemptCeiling": llm.get("attemptCeiling"),
    }


def _present_inputs(records: list, readings: list) -> dict:
    """Which requirement inputs this WAL actually carries.

    Before item 517 Slice 2 only ``modelDecisions`` could ever be present on a
    WAL; the rest were the inputs Slice 3a deliberately does not record, and
    ``liveComponent`` is never present offline. The map was kept in this shape
    so that a later slice recording more could flip one entry with no other
    change. This is that slice, and it flips two:

    * ``sealedEvidence`` — at least one crossing carries a seal that VERIFIED
      against the key the reader supplied. Present and unverified is not
      enough and no key is not enough, which is what keeps this from being a
      field that says "trust me";
    * ``promptDigest`` — at least one VERIFIED evidence object binds its prompt
      ``content-addressed``. Only that mode: it is the unsalted digest, legal
      only over an input whose declared origins carry neither ``secret`` nor
      ``confidential``, and it is the only binding that means anything across
      runs. ``salted-within-run`` and ``suppressed`` leave this false.

    Nothing else moves. In particular the response TEXT is still never written
    anywhere, so ``exact`` and ``tool-only`` remain out of reach from a WAL,
    and an evidence object does not pretend otherwise: it binds what was asked
    and what was chosen, by digest, and holds no completion text unless a run
    explicitly decided to retain it."""
    verified = [r for r in readings if r.get("verified")]
    content_addressed = any(
        (r.get("decision") or {}).get("promptBindingMode") == "content-addressed"
        for r in verified)
    return {
        "modelDecisions": bool(_model_decisions(records)),
        "sealedEvidence": bool(verified),
        "responseText": False,
        "promptDigest": bool(content_addressed),
        "toolCalls": False,
        "seedsAndClock": False,
        "liveComponent": False,
    }


def _reading(record: dict, key) -> dict:
    """One model decision, read back out of the artifact (item 517 Slice 3).

    Four states, kept apart on purpose, because collapsing any two of them is
    how a reader ends up trusting something it did not check:

    * **not sealed** — the crossing carries no evidence object. A run that
      never engaged sealing looks exactly like this, and so does a WAL written
      before Slice 2; this reader cannot tell them apart and does not try, the
      same honesty :data:`PRE_3A_NOTE` keeps about Slice 3a;
    * **refused at run time** — the run HAD engaged sealing and could not seal
      this crossing. The record says so, with the link and the reason. That
      run stopped at this crossing (`runtime.RevlModelEvidenceRefused`), so
      this is a fact about a run that did not continue, not a missing feature;
    * **sealed, unverified** — a seal is present and no key was supplied, so
      nothing was checked and nothing is read out;
    * **sealed and verified** — the decision is reconstructed.

    The reading keeps ``crossing`` as the key the WAL indexed the record BY,
    not as the one the sealed body names, and hands the first to
    :func:`~revl.model_evidence.reconstruct` as ``at`` so the two are compared.
    A genuine, genuinely-signed record lifted onto another crossing is refused
    there; keeping the WAL's key here is what lets a reader see WHERE it was
    found while the refusal says what it named.
    """
    crossing = [record.get("component"), record.get("stepIndex")]
    refused = record.get(WAL_REFUSAL_MEMBER)
    evidence = from_wal_record(record)
    entry = {
        "crossing": crossing,
        "sealed": evidence is not None,
        "refusedAtRun": dict(refused) if isinstance(refused, dict) else None,
        "verified": None,
        "link": "",
        "reason": "",
        "decision": None,
        "reproducible": None,
    }
    if evidence is None:
        entry["reason"] = (
            "this crossing carries no `%s` member: the run did not seal it, or "
            "it predates item 517 Slice 2. Both read the same way here"
            % WAL_EVIDENCE_MEMBER)
        return entry
    if key is None:
        entry["reason"] = ("a seal is present and no key was supplied, so it "
                           "was not checked and nothing is read out of it")
        return entry
    reading = reconstruct(evidence, key, at=tuple(crossing))
    reading["crossing"] = crossing
    entry.update(reading)
    return entry


def _mode_plan(mode: str, present: dict) -> dict:
    """Readiness for one mode: the requirements it names, which are met, which
    are missing, and the two honest verdicts.

    ``plannable`` is true when every RECORD-side requirement is met: the WAL
    carries enough to inform the mode, and the only gaps left are inputs the
    live executor supplies. ``executable`` is always false offline (no live
    component). The headline ``blocker`` is the first unmet record requirement
    when the record is short, otherwise the live component the executor is."""
    record_needs = MODE_RECORD_REQUIRES[mode]
    needs = _mode_requires(mode)
    record_gaps = [req for req in record_needs if not present.get(req)]
    plannable = record_gaps == []
    blocker = record_gaps[0] if record_gaps else "liveComponent"
    return {
        "mode": mode,
        "intent": MODE_INTENT[mode],
        "requires": list(needs),
        "recordRequires": list(record_needs),
        "met": [req for req in needs if present.get(req)],
        "missing": [req for req in needs if not present.get(req)],
        "plannable": bool(plannable),
        "executable": False,
        "blocker": blocker,
    }


def plan(path: str, mode: Optional[str] = None,
         evidence_key: Optional[bytes] = None) -> dict:
    """The replay-mode readiness plan for one WAL (item 250, Slice 3b, offline).

    Reads the WAL, indexes its durable model decisions, and reports per mode
    whether a replay in that mode could be RUN from what the WAL records. With
    ``mode`` it reports that one mode; without, all four.

    Never runs a replay: an offline reader has no live component. The plan names
    the requirements the live executor would still need, so the two halves of
    Slice 3b agree on the contract.

    ``evidence_key`` (item 517 Slice 3) is the key the run's evidence objects
    were sealed with. With it, every sealed crossing is VERIFIED and the
    decision is reconstructed into ``decisions``; without it, a seal is
    reported as present and unchecked and nothing is read out of it. The key is
    never written to the plan — only its fingerprint, which is already a member
    of each record.
    """
    if mode is not None and mode not in MODE_RECORD_REQUIRES:
        raise ReplayPlanError(
            f"unknown replay mode {mode!r}; known modes: {', '.join(MODES)}")

    wal = _load(path)
    records = wal["records"]
    decisions = _model_decisions(records)
    readings = [_reading(r, evidence_key) for r in decisions]
    present = _present_inputs(records, readings)

    modes = MODES if mode is None else (mode,)
    plans = [_mode_plan(m, present) for m in modes]

    return {
        "kind": "revl.replay-plan",
        "wal": path,
        "complete": wal.get("complete"),
        "torn": wal.get("torn"),
        "modelDecisions": [_decision_entry(r) for r in decisions],
        "decisions": readings,
        "modes": plans,
        "present": present,
        "requirements": {req: REQUIREMENTS[req]
                         for m in modes for req in _mode_requires(m)},
        "offlineNote": OFFLINE_NOTE,
        "pre3aNote": PRE_3A_NOTE,
        "decisionNote": DECISION_NOTE,
    }


def render(doc: dict) -> str:
    """A one-screen readiness report, in the plain-line style `revl branch` and
    `revl compare` render."""
    lines = [f"replay plan (item 250, Slice 3b): {doc['wal']}"]
    if doc.get("torn"):
        lines.append("  WARNING: the WAL is torn (a crash mid-write); the plan "
                     "reads only its intact prefix")
    decisions = doc["modelDecisions"]
    lines.append(f"  {len(decisions)} model decision(s) on record")
    for entry in decisions:
        lines.append(
            f"      {entry.get('component')}#{entry.get('stepIndex')}  "
            f"model {entry.get('model') or '(unreported)'}  "
            f"{entry.get('outcome')}  "
            f"attempts {entry.get('attempts')}/{entry.get('attemptCeiling')}")
    for entry in doc.get("decisions") or []:
        crossing = entry.get("crossing") or [None, None]
        head = f"      {crossing[0]}#{crossing[1]}  "
        if entry.get("verified"):
            d = entry["decision"]
            repro = entry.get("reproducible") or {}
            lines.append(head + "evidence VERIFIED")
            lines.append(f"          ran on  : {d['role']} ({d['residence']})")
            lines.append(f"          answered: model {d['modelDigest'][:16]} "
                         f"on profile "
                         f"{(d['placementDigest'] or '(unreported)')[:16]}")
            lines.append(f"          given   : prompt {d['promptBindingMode']}"
                         + (f" ({d['promptSuppressionReason']})"
                            if d["promptSuppressionReason"] else "")
                         + f", origins {d['origins'] or '[]'}")
            lines.append(f"          chose   : {d['chosen']} of "
                         f"{len(d['candidates'])} candidate(s), "
                         f"{d['outcome']}, fallback depth "
                         f"{d['fallbackDepth']}")
            lines.append(f"          asked   : {d['sampling']}")
            lines.append(f"          rule    : policy "
                         f"{d['policyDigest'][:16]}, key {d['keyId']}")
            if not repro.get("ok", True):
                lines.append("          re-runnable: no, missing "
                             + ", ".join(repro.get("missing") or []))
        elif entry.get("refusedAtRun"):
            refusal = entry["refusedAtRun"]
            lines.append(head + "evidence REFUSED AT RUN TIME "
                         f"({refusal.get('link')}): {refusal.get('reason')}")
        elif entry.get("sealed"):
            lines.append(head + "evidence sealed but NOT read: "
                         + entry.get("reason", ""))
        else:
            lines.append(head + "no evidence on the record")
    lines.append("")
    for mode in doc["modes"]:
        mark = "plannable" if mode["plannable"] else "not plannable"
        lines.append(f"  {mode['mode']:<17} {mark}  (executable: no, offline)")
        lines.append(f"      intent : {mode['intent']}")
        if mode["missing"]:
            lines.append(f"      missing: {', '.join(mode['missing'])}")
        else:
            lines.append("      missing: nothing on the record; needs only the "
                         "live executor")
    lines += ["", "  " + doc["offlineNote"], "  " + doc["pre3aNote"]]
    if doc.get("decisionNote"):
        lines.append("  " + doc["decisionNote"])
    return "\n".join(lines)


__all__ = [
    "MODES", "MODE_RECORD_REQUIRES", "MODE_EXECUTOR_REQUIRES", "MODE_INTENT",
    "REQUIREMENTS", "RECORD_MODEL_DECISION", "DECISION_NOTE", "ReplayPlanError",
    "plan", "render",
]

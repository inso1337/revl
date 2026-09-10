"""Counterfactual incident replay over a durable WAL (roadmap item 467).

The roadmap asks for a recorded incident to be replayed under an ALTERNATE
policy and against a CANDIDATE component with NO live effect authority, and for
the report to name the first dangerous crossing and whether the candidate is
admitted. It extends `revl recover`, `revl trace`, the WAL and the admission
gate; the honest scope line on the item is its own bound, that no new effect
fires and the report is only as complete as the recorded host responses.

This module is that surface, and it is deliberately the STATIC half of it. It
never boots a session, never instantiates a recorder, never touches the runtime
tier and never writes to or beside the WAL it reads: the candidate is COMPILED
(compilation fires nothing), its G8 audit graph is computed, and the recorded
crossings are re-graded against the alternate policy with the same evaluator the
admission gate runs. "Zero live effect authority" is therefore a property of the
module's shape rather than a promise it makes: there is no effect seam here to
fire one through. `tests/test_467_counterfactual_replay.py` pins that with a
byte-identical WAL before and after a run, and with an assertion that importing
this module pulls no runtime tier into `sys.modules`.

The seam it sits on is a real one. `revl replay WAL` (item 250, Slice 3b) is the
existing offline readiness planner for a branch replay; this module is the
policy question that planner already names as its most valuable mode. The WAL is
the only record of the incident's crossing ORDER, and the candidate's audit
graph is the only place a crossing's capability TOKEN can be resolved: the
emission record carries the crossing's label, key, method, service and arguments,
but not the scope the callee declared, so the token namespace of
`docs/boundary-policy.md` is not derivable from the record alone. That asymmetry
is the whole reason a candidate is required, and the reason `--under` with no
candidate answers "not answerable from this recording" rather than guessing.

What this module does NOT do, and will not pretend to
-----------------------------------------------------
It does not re-decide the recorded crossings. The WAL deliberately does not
carry the gate's admit decision or the host's response, for the reason
`revl.recovery` states of its own gate: the reader never reads back approval or
grant records, so authority injection stays structurally impossible. A
counterfactual over such a record is a RECOMPUTE of the alternate policy over
the reach the record names, not a replay of the decision the recording made. A
crossing this module calls permitted may have been refused at the time, and one
it calls refused may have been covered by an approval edge the record does not
hold. The report says so on every run, in :data:`BOUNDS`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import RevlError
from .policy import Policy, evaluate, load_policy
from .wal import WALIntegrityError, read_wal

#: The record and kind fields that mark one recorded boundary crossing. The
#: writer's `boundary_of` classifies `kind: emission` as `boundary.class:
#: emission` and a `process-crossing` referent, so the step kind is the same
#: fact read off the field the writer always writes; a reader keyed on the
#: classification instead would drop a record written before that field landed.
WAL_RECORD_EFFECT = "effect"
WAL_KIND_EMISSION = "emission"

#: The violation kinds that ARE the alternate policy's reach leg over a
#: crossing: a closed allow-list (`capability`) and a deny-list (`deny`). Every
#: other kind `policy.evaluate` can return reads an input a WAL does not carry
#: (a taint surface, a register floor, an evidence bundle, a tenant partition),
#: so the per-crossing leg filters to these two rather than reporting a
#: half-built audit graph as if the missing inputs had been evaluated. The
#: admission verdict at the bottom runs the WHOLE policy over the compiled
#: candidate, so no rule family is left unheard on the question of admission.
REACH_VIOLATION_KINDS = ("capability", "deny")

#: One sentence per bound, keyed, so a reader sees what this surface cannot
#: answer instead of inferring it from silence. Every one of them is a fact
#: about the record, not a hedge: the first is the deliberate design of the WAL,
#: the second is a filed gap in it (issue #841).
BOUNDS = {
    "recordedAuthority":
        "The WAL carries no admit decision and no host response. `revl.recovery` "
        "states that its gate never reads back approval or grant records, so "
        "authority injection stays structurally impossible, and the same "
        "absence is what this report runs on: it RECOMPUTES the alternate "
        "policy over the reach the record names rather than re-deciding the "
        "crossing the recording made. A crossing reported permitted may have "
        "been refused at the time, and one reported refused may have been "
        "covered by an approval edge the record does not hold.",
    "freeHostExterns":
        "A free host-extern emission leaves no record. A direct `emit "
        "announce(x)` over a host extern compiles to a bare module-level call "
        "that never touches the recording context, so no `emission` effect "
        "record is written for it (issue #841). Such a crossing cannot appear "
        "in this report, and the report cannot tell it apart from a crossing "
        "the incident never made. The reached host externs the candidate "
        "declares are listed below so that gap is visible rather than silent.",
    "emissionCrossingsOnly":
        "Recorded crossings are the WAL's `kind: emission` effect records. The "
        "WAL also records boundary effects classified `acquire` on the durable "
        "resource cleanup path, which are not reduced to a capability token "
        "here: an acquisition record names a resource, not the policy token it "
        "crossed.",
    "reachLegOnly":
        "Each per-crossing verdict recomputes the alternate policy's reach leg "
        "(allow-list and deny-list violations) and nothing else, because a "
        "crossing's capability reach is the whole of what the record supports. "
        "The admission verdict runs every rule family over the compiled "
        "candidate, so a rule family the per-crossing leg cannot see still "
        "decides admission, and a refusal with no recorded crossing behind it "
        "is reported as exactly that.",
    "candidateAdmissionIsThePolicyLeg":
        "The candidate's admission verdict is the boundary-policy leg of the "
        "gate (`policy.evaluate` over the candidate's audit graph), which is "
        "the leg `revl.admission.admit_under_policy` forwards to. The gate's "
        "other legs, service-replacement correctness and the registry and "
        "evidence checks, read a live composition and a registry the caller "
        "holds, not a recording, and are not run here.",
}


class CounterfactualError(RuntimeError):
    """A WAL or a policy could not be read, or the counterfactual question is
    malformed (an unknown candidate file, a composition with open holes)."""


@dataclass(frozen=True)
class RecordedCrossing:
    """One boundary crossing as the WAL recorded it, with the alternate policy's
    verdict against it. The record's own fields are kept verbatim; `tokens` and
    `refusals` are the counterfactual, filled in only when a candidate supplied
    the audit graph that resolves the crossing's label to a capability token."""
    seq: int | None
    component: str
    stepIndex: int | None
    label: str
    site: str | None
    compensated: bool
    tokens: tuple | None = None
    reason: str | None = None
    refusals: tuple = ()
    verdict: str = "not-answerable"

    def key(self) -> str:
        return f"{self.component}#{self.seq}"

    def as_dict(self) -> dict:
        out = {
            "seq": self.seq,
            "component": self.component,
            "stepIndex": self.stepIndex,
            "label": self.label,
            "site": self.site,
            "compensated": self.compensated,
            "verdict": self.verdict,
        }
        if self.tokens is not None:
            out["tokens"] = list(self.tokens)
        if self.reason is not None:
            out["reason"] = self.reason
        if self.refusals:
            out["refusals"] = [
                {"kind": v.kind, "token": v.token, "message": v.message}
                for v in self.refusals]
        return out


def _load_wal(path: str) -> dict:
    try:
        return read_wal(path)
    except OSError as error:
        raise CounterfactualError(
            f"cannot read WAL {path}: {error}") from None
    except WALIntegrityError as error:
        raise CounterfactualError(str(error)) from None


def _load_policy(path: str) -> Policy:
    try:
        return load_policy(path)
    except OSError as error:
        raise CounterfactualError(
            f"cannot read policy {path}: {error}") from None
    except RevlError as error:
        raise CounterfactualError(str(error)) from None


def recorded_crossings(records: list) -> list:
    """Every boundary crossing the WAL recorded, in RECORDED ORDER.

    Recorded order is the seq space the writer owns (one strictly increasing
    sequence for the whole life of a log, resumed rather than reset on reopen),
    so it is the order that makes "the FIRST dangerous crossing" mean the first
    one the incident actually made. A record with no seq sorts last rather than
    being dropped, because a reader that silently discarded it would report a
    shorter incident than the log holds.
    """
    out = []
    for record in records:
        if record.get("record") != WAL_RECORD_EFFECT:
            continue
        if record.get("kind") != WAL_KIND_EMISSION:
            continue
        boundary = record.get("boundary") or {}
        out.append(RecordedCrossing(
            seq=record.get("seq"),
            component=record.get("component"),
            stepIndex=record.get("stepIndex"),
            label=record.get("label"),
            site=record.get("site"),
            compensated=bool(boundary.get("compensated"))))
    out.sort(key=lambda c: (c.seq is None, c.seq))
    return out


def _candidate_audit(files: list):
    """Compile the candidate composition and build its audit graph. Pure: a
    compile and a boundary walk fire no effect, which is the whole reason this
    surface can claim zero live effect authority structurally."""
    from .audit_diff import audit_report  # noqa: PLC0415 — additive, lazy
    from .compiler import compile_files  # noqa: PLC0415 — additive, lazy

    try:
        ir = compile_files(list(files))
    except OSError as error:
        raise CounterfactualError(
            f"cannot read candidate source: {error}") from None
    except RevlError as error:
        raise CounterfactualError(
            f"the candidate composition does not compile: {error}") from None
    if ir.get("holes"):
        raise CounterfactualError(
            "the candidate composition has open typed holes; fill them before "
            "a counterfactual, because a hole is a crossing no audit can "
            "enumerate")
    return audit_report(ir)


def _resolve(crossings: list, audit: dict | None) -> list:
    """Resolve each recorded crossing to the candidate's declared capability
    tokens, or to the reason it cannot be resolved.

    The WAL names a crossing (component, label, key, method, service, args) and
    not its token, because the declared scope lives in the callee's declaration
    and the record holds no declaration. The candidate's audit graph is where
    that scope is enumerated (`boundary[component].capabilities`, keyed by the
    same `key.method` label the recorder writes), so it is also where the token
    namespace of `docs/boundary-policy.md` becomes readable. With no candidate
    the honest verdict is `not-answerable`, never a guess at the scope.
    """
    if audit is None:
        return [RecordedCrossing(
            **{**_fields(c), "verdict": "not-answerable",
               "reason": _NO_CANDIDATE_REASON}) for c in crossings]

    boundary = audit.get("boundary") or {}
    out = []
    for crossing in crossings:
        stats = boundary.get(crossing.component)
        if stats is None:
            out.append(RecordedCrossing(
                **_fields(crossing), verdict="not-in-candidate",
                reason=(f"the candidate composition defines no component named "
                        f"`{crossing.component}`, so it makes no crossing "
                        f"there")))
            continue
        tokens = (stats.get("capabilities") or {}).get(crossing.label)
        if tokens is None:
            out.append(RecordedCrossing(
                **_fields(crossing), verdict="not-in-candidate",
                reason=(f"the candidate declares no emission labelled "
                        f"`{crossing.label}` on `{crossing.component}`: the "
                        f"crossing was renamed or removed, so the candidate "
                        f"does not make it")))
            continue
        out.append(RecordedCrossing(
            **_fields(crossing), tokens=tuple(tokens), verdict="permitted"))
    return out


_NO_CANDIDATE_REASON = (
    "not answerable from this recording: the emission record names the "
    "crossing's component, label, key, method, service and arguments, but not "
    "the capability scope the callee declared, so the policy token this "
    "crossing carries is not on the record. Supply the candidate composition "
    "so the token is resolved from the audit graph that enumerates it.")


def _fields(crossing: RecordedCrossing) -> dict:
    """The recorded facts of a crossing, without any counterfactual verdict, so
    a re-derived crossing is built from the record and nothing else."""
    return {
        "seq": crossing.seq, "component": crossing.component,
        "stepIndex": crossing.stepIndex, "label": crossing.label,
        "site": crossing.site, "compensated": crossing.compensated,
    }


def _synthetic_audit(resolved: list, manifest: dict) -> dict:
    """The reach graph the RECORD supports, as an audit the real evaluator can
    be run over.

    Only components the candidate defines and only the labels the candidate
    declares get a boundary entry, because `policy.evaluate` reads a component's
    absence of reach as nothing to refuse: handing it a component the candidate
    does not define would report that component's crossings permitted, which is
    a false clean. The manifest is the candidate's own, so a realm-scoped rule
    selects on the same isolate map the candidate would be admitted under.
    """
    capabilities: dict = {}
    for crossing in resolved:
        if crossing.tokens is None:
            continue
        capabilities.setdefault(crossing.component, {})[crossing.label] = \
            list(crossing.tokens)
    return {
        "manifest": manifest,
        "boundary": {name: {"capabilities": caps}
                     for name, caps in capabilities.items()},
    }


def _grade(resolved: list, policy: Policy, synthetic: dict) -> list:
    """Fill in each resolvable crossing's verdict from the real evaluator.

    `policy.evaluate` runs over the synthetic reach graph and its violations are
    filtered to the reach leg (`REACH_VIOLATION_KINDS`), because the graph holds
    a crossing's capability reach and no other input the other legs read. A
    crossing is refused when a violation names its component and one of its
    tokens; the refusal is carried verbatim so the message the gate would print
    is the message this report prints.
    """
    reach_violations = [v for v in evaluate(policy, synthetic)
                        if v.kind in REACH_VIOLATION_KINDS]
    by_token: dict = {}
    for violation in reach_violations:
        by_token.setdefault((violation.component, violation.token), []).append(
            violation)
    out = []
    for crossing in resolved:
        if crossing.tokens is None:
            out.append(crossing)
            continue
        refusals = []
        for token in crossing.tokens:
            refusals.extend(by_token.get((crossing.component, token), ()))
        out.append(RecordedCrossing(
            **_fields(crossing), tokens=crossing.tokens,
            refusals=tuple(refusals),
            verdict="refused" if refusals else "permitted"))
    return out


def _first_dangerous(crossings: list):
    """The first crossing in RECORDED ORDER the alternate policy refuses, or
    None. Recorded order is the incident's own: a crossing the new policy
    refuses is dangerous in the order it was made, so the earliest refused one
    is the first boundary the incident would not have been allowed to cross."""
    for crossing in crossings:
        if crossing.verdict == "refused":
            return crossing
    return None


def _host_externs(audit: dict | None) -> list:
    """The candidate's reached host externs, by component and name. Named in the
    report because a free host-extern emission leaves no WAL record (issue
    #841), so these are precisely the crossings the recording cannot confirm."""
    if audit is None:
        return []
    out = []
    for name, stats in sorted((audit.get("boundary") or {}).items()):
        for ext in stats.get("externs") or []:
            out.append({"component": name, "extern": ext.get("name"),
                        "tokens": list(ext.get("capabilities")
                                       or (ext.get("name"),))})
    return out


def replay_under(wal_path: str, policy_path: str,
                 candidate: list | None = None) -> dict:
    """Replay a recorded incident's crossings under an alternate policy.

    Reads the WAL for the incident's crossing order, compiles `candidate` for
    the composition the crossing would be made in and for the audit graph that
    resolves each crossing's capability token, then recomputes the alternate
    policy's reach leg over that reach graph with `policy.evaluate`, the same
    evaluator the admission gate runs. Reports the first recorded crossing the
    alternate policy refuses and whether the candidate is admitted under the
    whole policy.

    Fires nothing. A WAL read is a read, a compile is a compile, and the
    counterfactual's only side effect is the report it returns.
    """
    if not policy_path:
        raise CounterfactualError(
            "a counterfactual needs an alternate policy; pass --under POLICY")

    wal = _load_wal(wal_path)
    crossings = recorded_crossings(wal["records"])
    policy = _load_policy(policy_path)
    audit = _candidate_audit(candidate) if candidate else None

    resolved = _resolve(crossings, audit)
    resolved = _grade(resolved, policy, _synthetic_audit(
        resolved, (audit or {}).get("manifest") or {}))

    dangerous = _first_dangerous(resolved)
    admission = None
    if audit is not None:
        violations = evaluate(policy, audit)
        admission = {
            "admitted": not violations,
            "violations": [
                {"kind": v.kind, "component": v.component, "token": v.token,
                 "message": v.message} for v in violations],
        }

    # With a candidate every recorded crossing carries a verdict: the candidate
    # either declares the crossing (and the alternate policy permits or refuses
    # its token) or it does not declare it at all. Without one, no crossing's
    # token is on the record and the honest answer is that the question is not
    # answerable from this recording.
    answerable = audit is not None

    bounds = dict(BOUNDS)
    if wal.get("torn"):
        bounds["tornWal"] = (
            "This WAL is torn: a crash interrupted its final write. Only its "
            "intact prefix is read, so a crossing in the unacknowledged tail is "
            "not in this report and no verdict here covers it.")
    if _has_realm_rules(policy) and audit is None:
        bounds["realmScopedRules"] = (
            "The alternate policy has realm-scoped rules and no candidate was "
            "supplied, so no component's realm is known and those rules select "
            "nothing here. That is a gap in this report's reach, not a finding "
            "that they are satisfied.")

    return {
        "kind": "revl.counterfactual-replay",
        "wal": wal_path,
        "policy": policy_path,
        "complete": wal.get("complete"),
        "torn": wal.get("torn"),
        "candidate": None if audit is None else {
            "files": list(candidate),
            "components": sorted((audit.get("boundary") or {})),
            "liveEffects": 0,
        },
        "recordedCrossings": len(crossings),
        "crossings": [c.as_dict() for c in resolved],
        "answerable": bool(answerable),
        "refusedCrossings": sum(1 for c in resolved
                                if c.verdict == "refused"),
        "firstDangerousCrossing": (None if dangerous is None
                                   else dangerous.as_dict()),
        "admission": admission,
        "hostExternsUnrecordable": _host_externs(audit),
        "bounds": bounds,
    }


def _has_realm_rules(policy: Policy) -> bool:
    return any(rule.scope == "realm" for rule in policy.rules)


def render(doc: dict) -> str:
    """The counterfactual report, in the plain-line style `revl replay` and
    `revl branch` render: what the record holds, what each crossing grades to,
    the first dangerous crossing, and the admission verdict."""
    lines = [f"counterfactual replay (item 467): {doc['wal']}"
             f" under {doc['policy']}"]
    if doc.get("torn"):
        lines.append("  WARNING: the WAL is torn (a crash mid-write); only its "
                     "intact prefix is read")
    lines.append(f"  recorded crossings: {doc['recordedCrossings']} "
                 f"(from the WAL, in recorded order)")
    candidate = doc.get("candidate")
    if candidate is None:
        lines.append("  candidate: none supplied; the policy question is not "
                     "answerable from the record alone")
    else:
        lines.append(f"  candidate: {', '.join(candidate['files'])}")
        lines.append(f"      components: {', '.join(candidate['components'])}")
        lines.append(f"      live effects: {candidate['liveEffects']} "
                     f"(compiled statically, nothing run)")
    lines.append("")
    if not doc["crossings"]:
        lines.append("  no crossing is on the record; a recording with no "
                     "emission record reads as an incident that crossed "
                     "nothing, and a free host-extern emission would not be "
                     "recorded even when it did (issue #841)")
    for crossing in doc["crossings"]:
        head = (f"  {crossing['component']}#{crossing['seq']}  "
                f"{crossing['label']}  [{crossing['verdict']}]")
        lines.append(head)
        if crossing.get("tokens") is not None:
            lines.append(f"      tokens : {', '.join(crossing['tokens'])}")
        if crossing.get("reason"):
            lines.append(f"      reason : {crossing['reason']}")
        for refusal in crossing.get("refusals") or []:
            lines.append(f"      {refusal['kind']}: {refusal['message']}")
    lines.append("")
    dangerous = doc["firstDangerousCrossing"]
    if dangerous is None:
        lines.append("  first dangerous crossing: none; no recorded crossing is "
                     "refused by the alternate policy")
    else:
        lines.append(f"  FIRST DANGEROUS CROSSING: {dangerous['component']}"
                     f"#{dangerous['seq']}  {dangerous['label']}"
                     f"  (token "
                     f"{', '.join(dangerous.get('tokens') or [])})")
    admission = doc.get("admission")
    if admission is None:
        lines.append("  candidate admitted: not answerable without a candidate")
    else:
        lines.append("  candidate admitted: "
                     + ("yes" if admission["admitted"] else "no"))
        for violation in admission["violations"]:
            lines.append(f"      {violation['kind']}: {violation['message']}")
        if not admission["admitted"] and dangerous is None:
            lines.append("      (the refusal is not over a recorded crossing: "
                         "the policy refuses the candidate for a reason the "
                         "recording holds no crossing for)")
    externs = doc["hostExternsUnrecordable"]
    if externs:
        lines.append("")
        lines.append(f"  reached host externs the record cannot confirm "
                     f"({len(externs)}; a free host-extern emission leaves no "
                     f"WAL record, issue #841):")
        for entry in externs:
            lines.append(f"      {entry['component']}: {entry['extern']} "
                         f"[{', '.join(entry['tokens'])}]")
    lines.append("")
    lines.append("  bounds:")
    for key, text in doc["bounds"].items():
        lines.append(f"    {key}: {text}")
    return "\n".join(lines)


__all__ = [
    "BOUNDS", "REACH_VIOLATION_KINDS", "WAL_KIND_EMISSION", "WAL_RECORD_EFFECT",
    "CounterfactualError", "RecordedCrossing", "recorded_crossings",
    "render", "replay_under",
]

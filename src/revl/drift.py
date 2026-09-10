"""Enforced policy-drift control (roadmap item 469, issue #821): the identity
manifest, the drift verdict, and the response the configured control fires.

WHAT EXISTING CODE ALREADY ENFORCES. Admission is already a hash-level identity
statement, and the repo already takes it seriously:

  * :mod:`revl.attest` binds a composition's canonical IR hash, the checker
    identity and the ruleset digest into a signed attestation, and folds the
    digest of each artifact a bundle publishes into the same signed payload.
  * :func:`revl.deploy.artifact_digest` re-computes an emitted tree's digest on
    the receiver and refuses a tree that carries a symlink or no bytes at all.
  * :func:`revl.apply.drift` compares a plan's basis against the live
    composition, structurally: component names, load order, provided keys.
  * :mod:`revl.reconcile` reconciles an activation's WAL and trace against the
    world and answers with `clean`, `refused` or `unresolved`.

Each of those is taken at admission, or at one later handshake (deploy), or over
the composition's SHAPE rather than over bytes. None of them is an invariant
that keeps holding after admission over hash-level identity, and none of them
has a vocabulary for the answer when one of them no longer holds.

THE GAP THIS MODULE FILLS. A live system is the admitted one exactly while every
admitted subject still hashes to the value admission bound. That is a statement
about a SET of named subjects - the model, tool, policy, dependency and peer
artifacts item 469 names - and the re-measurement of each. This module is that
statement: the manifest of admitted digests, the verdict over a re-measurement
of it, and the response the configured control fires. It is the pure half, the
same split :mod:`revl.reconcile` keeps from the runtime: it reads and decides,
it re-hashes nothing, and it suspends no authority itself.

WHAT IT DELIBERATELY DOES NOT DO. Nothing here re-measures a live subject (the
continuously running monitor that does is the follow-up). Nothing here performs
a response: `read-only`, `re-admit`, `suspend` and `rollback` are decisions the
control RECORDS here and actuators carry out later. Semantic equivalence of a
changed subject is out of scope by the item's own boundary: two different bytes
are drift, whatever they mean.

THE ONE RULE THE VERDICT KEEPS. Absence of evidence is not drift. A subject the
admitted manifest names, that the live reading did not produce at all, is
`unverified` rather than `drifted`: the reason to remove authority has to be a
measured mismatch, not a blind spot (the posture
:func:`revl.why_runtime.liveness_expired` takes, and for the same reason, since
this control withdraws authority too). An unverified subject is still reported
LOUDLY - the verdict is never `matched` while one exists, and the control may
configure a response for it - but it fires no response by default.
"""

from __future__ import annotations

import hashlib
import re

#: The version of the configuration shape :func:`normalize_policy` reads. It is
#: bumped when a key this module reads changes meaning, not when a new optional
#: key is added.
DRIFT_CONTROL_VERSION = 1

#: The WAL record kind a drift verdict is written under. It follows the
#: vocabulary :mod:`revl.wal` already uses for a fact the runtime reasons about
#: (`model-decision`, `provider-withdrawn`): a hyphenated kind the reader can
#: index without a schema change.
RECORD_DRIFT_DETECTED = "drift-detected"

#: The roles a subject of an admitted identity can hold, in the item's words.
#: A manifest row is `(role, name)`: the role says what kind of thing was
#: admitted, the name says which one.
ROLES = ("model", "tool", "policy", "dependency", "peer")

STATUS_MATCHED = "matched"
STATUS_DRIFTED = "drifted"
STATUS_MISSING = "missing"
STATUS_UNEXPECTED = "unexpected"
STATUS_UNVERIFIED = "unverified"

VERDICT_MATCHED = "matched"
VERDICT_DRIFTED = "drifted"
VERDICT_UNVERIFIED = "unverified"

#: The drift statuses: a measured difference between the admitted identity and
#: the live one. `unexpected` is a live subject admission never named, which is
#: drift in the direction admission cannot account for either.
DRIFT_STATUSES = (STATUS_DRIFTED, STATUS_MISSING, STATUS_UNEXPECTED)

#: NOTHING: the control observes the drift and removes no authority. This is the
#: floor of the response order below, and the response a fresh admission starts
#: from.
RESPONSE_NONE = "none"
RESPONSE_READ_ONLY = "read-only"
RESPONSE_RE_ADMIT = "re-admit"
RESPONSE_SUSPEND = "suspend"
RESPONSE_ROLLBACK = "rollback"

#: The responses a control may be configured with, weakest first. The order is
#: the item's four responses plus the floor, and reading it as "how much
#: authority is left standing" is what makes a response a downgrade:
#:
#:   * `none` observes and changes nothing;
#:   * `read-only` narrows what the live composition may do, without stopping it;
#:   * `re-admit` refuses to renew the admitted identity until admission runs
#:     again, so the composition may keep acting only as far as admission
#:     re-proves it;
#:   * `suspend` removes the authority now rather than at the next admission;
#:   * `rollback` goes furthest, reverting the running artifact set to the
#:     pinned one instead of only changing what the running set may do.
RESPONSES = (
    RESPONSE_NONE,
    RESPONSE_READ_ONLY,
    RESPONSE_RE_ADMIT,
    RESPONSE_SUSPEND,
    RESPONSE_ROLLBACK,
)

_RANK = {response: rank for rank, response in enumerate(RESPONSES)}

#: The response a drift status earns when the control configures none: a drift
#: the operator did not anticipate suspends rather than observes, because the
#: default has to be the response that fails closed.
DEFAULT_RESPONSE = RESPONSE_SUSPEND

#: The response an unverified subject earns when the control configures none: a
#: blind spot removes no authority on its own, but it is never read as a match.
DEFAULT_UNVERIFIED_RESPONSE = RESPONSE_NONE

DRIFT_GUARANTEE = (
    "a subject that was re-measured and does not hash to its admitted digest is "
    "drift, and its status is `drifted`; a subject the admitted manifest names "
    "that the live reading reports absent is `missing`; a live subject admission "
    "never named is `unexpected`; a subject the live reading did not produce is "
    "`unverified` and is never reported as `matched`; the response fired is the "
    "strongest response the configured policy earns over the drift set."
)

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


class DriftError(Exception):
    """A manifest, a live reading or a control policy that cannot be read as
    what it claims to be. Fail closed: a control that cannot tell drift from a
    malformed reading must not be the thing that decides whether to suspend."""


def digest(data: bytes) -> str:
    """The sha256 of `data`, hex. The one digest spelling a manifest row holds,
    so a producer and a re-measurement cannot disagree about the encoding."""
    return hashlib.sha256(data).hexdigest()


def _check_digest(value, where: str) -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise DriftError(
            f"{where}: {value!r} is not a sha256 digest (64 lowercase hex characters)"
        )
    return value


def _label(role, name) -> str:
    return f"{role}:{name}"


def _order(subject) -> tuple:
    return (
        (subject[0], subject[1]) if isinstance(subject, tuple) else (str(subject), "")
    )


def _manifest_rows(manifest, what: str) -> dict:
    """`{(role, name): digest}` for an admitted manifest, refusing anything that
    is not the shape an identity statement has to have. An unknown role is
    refused here and not in a live reading: admission has to say what it
    admitted in the item's vocabulary, while a live reading may name a subject
    admission never considered, which is a fact to report rather than a typo."""
    if not isinstance(manifest, dict):
        raise DriftError(
            f"{what} must be a mapping of role to subject to sha256, "
            f"got {type(manifest).__name__}"
        )
    rows = {}
    for role, subjects in manifest.items():
        if role not in ROLES:
            raise DriftError(f"{what}: {role!r} is not one of the drift roles {ROLES}")
        if not isinstance(subjects, dict):
            raise DriftError(f"{what}: role {role!r} must map subject name to sha256")
        for name, value in subjects.items():
            if not isinstance(name, str) or not name:
                raise DriftError(f"{what}: role {role!r} has an empty subject name")
            rows[(role, name)] = _check_digest(value, f"{what}: {_label(role, name)}")
    return rows


def _live_reading(live) -> tuple:
    """`(values, reasons)` from a live reading.

    `values` maps a subject the reading produced to its digest, or to `None`
    when the reading reports the subject absent. `reasons` maps a subject whose
    reading is not a digest to the account of what was read instead, so a
    malformed reading is reported as unverified with its cause rather than
    silently compared as a mismatch.
    """
    if live is None:
        return {}, {}
    if not isinstance(live, dict):
        raise DriftError(
            f"the live reading must be a mapping of role to subject to sha256, "
            f"got {type(live).__name__}"
        )
    values, reasons = {}, {}
    for role, subjects in live.items():
        if not isinstance(role, str) or not role:
            raise DriftError(f"the live reading has a role {role!r} that is not a name")
        if not isinstance(subjects, dict):
            raise DriftError(
                f"the live reading: role {role!r} must map subject name to a reading"
            )
        for name, raw in subjects.items():
            if not isinstance(name, str) or not name:
                raise DriftError(
                    f"the live reading: role {role!r} has an empty subject name"
                )
            key = (role, name)
            if raw is None:
                values[key] = None
            elif isinstance(raw, str) and _HEX64.match(raw):
                values[key] = raw
            else:
                reasons[key] = (
                    f"the live reading of {_label(role, name)} is {raw!r}, not a sha256 "
                    "digest, so the subject cannot be verified against the admitted one"
                )
    return values, reasons


def detect(admitted, live=None) -> dict:
    """The drift verdict over a re-measurement of an admitted identity.

    `admitted` is the manifest of digests admission bound, as
    `{role: {name: digest}}`. `live` is the re-measurement in the same shape,
    where a subject the reader reports as absent is `None`; a subject that is
    simply not there is the same fact as a subject the reading did not produce.

    Returns the report every other entry point reads: the verdict, the per
    subject rows, and the counts. `matched` only when every admitted subject was
    verified and nothing unexpected is live; `drifted` when any measured
    difference exists; `unverified` otherwise, which is a live system that may
    well match but was not shown to.
    """
    rows = _manifest_rows(admitted, "the admitted manifest")
    values, reasons = _live_reading(live)

    subjects = []
    for key in sorted(rows, key=_order):
        role, name = key
        admitted_digest = rows[key]
        if key in reasons:
            status = STATUS_UNVERIFIED
            reason = reasons[key]
        elif key not in values:
            status = STATUS_UNVERIFIED
            reason = (
                "the live reading produced no digest for this admitted subject, so it is "
                "not verified as the admitted one; an unmeasured subject is a blind spot, "
                "not a measured mismatch"
            )
        elif values[key] is None:
            status = STATUS_MISSING
            reason = "the live reading reports this admitted subject absent"
        elif values[key] == admitted_digest:
            status = STATUS_MATCHED
            reason = "the live subject hashes to the admitted digest"
        else:
            status = STATUS_DRIFTED
            reason = (
                f"the live subject hashes to {values[key][:12]}..., not the admitted "
                f"{admitted_digest[:12]}..."
            )
        subjects.append(
            {
                "subject": _label(role, name),
                "role": role,
                "name": name,
                "status": status,
                "admitted": admitted_digest,
                "live": values.get(key),
                "reason": reason,
            }
        )

    for key in sorted(set(values) | set(reasons), key=_order):
        if key in rows:
            continue
        role, name = key
        subjects.append(
            {
                "subject": _label(role, name),
                "role": role,
                "name": name,
                "status": STATUS_UNEXPECTED,
                "admitted": None,
                "live": values.get(key),
                "reason": reasons.get(
                    key,
                    "the live composition holds a subject admission never named, so the "
                    "running set is not the admitted one",
                ),
            }
        )

    counts = {
        status: 0
        for status in (
            STATUS_MATCHED,
            STATUS_DRIFTED,
            STATUS_MISSING,
            STATUS_UNEXPECTED,
            STATUS_UNVERIFIED,
        )
    }
    for row in subjects:
        counts[row["status"]] += 1

    if counts[STATUS_DRIFTED] or counts[STATUS_MISSING] or counts[STATUS_UNEXPECTED]:
        verdict = VERDICT_DRIFTED
    elif counts[STATUS_UNVERIFIED]:
        verdict = VERDICT_UNVERIFIED
    else:
        verdict = VERDICT_MATCHED

    return {
        "verdict": verdict,
        "subjects": subjects,
        "counts": counts,
        "guarantee": DRIFT_GUARANTEE,
    }


def normalize_policy(policy=None) -> dict:
    """The control policy, with every unset response filled in.

    The declared shape is `{"onDrift": <response>, "byRole": {<role>: <response>},
    "onUnverified": <response>}`. `onDrift` is the response a drifted, missing or
    unexpected subject earns unless its role overrides it; `onUnverified` is the
    response an unverified subject earns, and is `none` because a subject that
    was not measured is not a subject that was shown to differ.

    A key this control does not read is refused rather than ignored: a misspelt
    `onDrift` would otherwise leave the fail-closed default in place while the
    operator believed they had configured something else.
    """
    config = {
        "version": DRIFT_CONTROL_VERSION,
        "onDrift": DEFAULT_RESPONSE,
        "onUnverified": DEFAULT_UNVERIFIED_RESPONSE,
        "byRole": {},
    }
    if policy is None:
        return config
    if not isinstance(policy, dict):
        raise DriftError(f"driftControl must be a mapping, got {type(policy).__name__}")
    known = ("onDrift", "byRole", "onUnverified")
    unknown = [key for key in policy if key not in known]
    if unknown:
        raise DriftError(
            f"driftControl: {sorted(unknown)} is not read by this control; it reads {list(known)}"
        )
    if "onDrift" in policy:
        config["onDrift"] = _check_response(policy["onDrift"], "driftControl: onDrift")
    if "onUnverified" in policy:
        config["onUnverified"] = _check_response(
            policy["onUnverified"], "driftControl: onUnverified"
        )
    if "byRole" in policy:
        by_role = policy["byRole"]
        if not isinstance(by_role, dict):
            raise DriftError(
                f"driftControl: byRole must map role to response, "
                f"got {type(by_role).__name__}"
            )
        for role, response in by_role.items():
            if role not in ROLES:
                raise DriftError(
                    f"driftControl: byRole names {role!r}, which is not one of the drift "
                    f"roles {ROLES}"
                )
            config["byRole"][role] = _check_response(
                response, f"driftControl: byRole.{role}"
            )
    return config


def _check_response(value, where: str) -> str:
    if value not in RESPONSES:
        raise DriftError(
            f"{where}: {value!r} is not one of the responses {list(RESPONSES)}"
        )
    return value


def respond(report, policy=None) -> dict:
    """The response the configured control fires for a report.

    A response is a downgrade: the control may fire what its policy declared for
    a status, and may never fire more than that, because a subject that drifted
    by one byte has not earned authority it was not configured to have. When a
    drift set earns more than one response - a per role override on one subject
    and the policy default on another, say - the effective response is the
    strongest of them, and the decisive subject is the first one in the report
    that holds it.

    `rollback` is a claim that a pinned artifact is available to go back to, so
    whether the response can be carried out is not something this function can
    decide; :func:`resolve` refuses a rollback no pinned digest backs.
    """
    config = normalize_policy(policy)
    fired = []
    for row in report["subjects"]:
        if row["status"] in DRIFT_STATUSES:
            response = config["byRole"].get(row["role"], config["onDrift"])
            trigger = "drift"
        elif row["status"] == STATUS_UNVERIFIED:
            response = config["onUnverified"]
            trigger = "unverified"
        else:
            continue
        if response == RESPONSE_NONE:
            continue
        fired.append(
            {
                "subject": row["subject"],
                "role": row["role"],
                "status": row["status"],
                "response": response,
                "trigger": trigger,
                "reason": row["reason"],
            }
        )

    if fired:
        decisive = fired[0]
        for row in fired[1:]:
            if _RANK[row["response"]] > _RANK[decisive["response"]]:
                decisive = row
        response = decisive["response"]
        trigger = decisive["trigger"]
        subject = decisive["subject"]
        reason = (
            f"{len(fired)} of {len(report['subjects'])} admitted subjects earn a response; "
            f"the control fires {response}, the strongest response its policy grants here"
        )
    else:
        response = RESPONSE_NONE
        trigger = "none"
        subject = None
        reason = (
            "no subject earns a response: nothing drifted and the control configures no "
            "response for an unverified subject"
        )

    return {
        "response": response,
        "trigger": trigger,
        "subject": subject,
        "subjects": fired,
        "policy": config,
        "reason": reason,
    }


def resolve(report, policy=None, *, rollback_to=None) -> dict:
    """The response a monitor fires for a report, given what the world supports.

    The decision from :func:`respond`, or a refusal when it names an affordance
    the world does not have. `rollback` requires `rollback_to` - the pinned
    artifact digest the control would go back to - and a rollback that cannot
    name one is refused rather than quietly downgraded, because going back to a
    target nobody pinned is not a rollback.
    """
    decision = respond(report, policy)
    if decision["response"] == RESPONSE_ROLLBACK:
        if not isinstance(rollback_to, str) or not _HEX64.match(rollback_to):
            raise DriftError(
                "driftControl fires rollback but no pinned artifact digest was named, so "
                "there is no target to go back to; refusing rather than downgrading the "
                "response"
            )
    return decision


def wal_record(report, decision, *, generation=None, at=None) -> dict:
    """One durable record of a drift verdict and the response it fired.

    The record is written in the shape :mod:`revl.wal` reads: a `record` member
    names the kind, so `revl.wal.read_wal` returns it beside every other record
    with no reader change, exactly as it reads `model-decision`.

    It records the DECISION, not a re-argument of it. What goes in is the
    verdict, the effective response, the subject the response was decided on,
    every subject that was not matched, and the policy that was in force, so a
    post-mortem can ask what the control knew and what it was configured to do.
    `at` is the ISO timestamp the caller read from its clock; this module never
    reads a clock, so a record is a function of its arguments.
    """
    return {
        "record": RECORD_DRIFT_DETECTED,
        "generation": generation,
        "at": at,
        "verdict": report["verdict"],
        "response": decision["response"],
        "trigger": decision["trigger"],
        "subject": decision["subject"],
        "policy": decision["policy"],
        "counts": report["counts"],
        "subjects": [
            {
                "subject": row["subject"],
                "role": row["role"],
                "name": row["name"],
                "status": row["status"],
                "admitted": row["admitted"],
                "live": row["live"],
                "reason": row["reason"],
            }
            for row in report["subjects"]
            if row["status"] != STATUS_MATCHED
        ],
        "guarantee": DRIFT_GUARANTEE,
    }


def drift_records(records) -> list:
    """The drift records in a WAL's records, in recorded order.

    Mirrors :func:`revl.wal.model_decisions`: recovery ignores what is not a
    drift record, while an operator asking `what did this control decide` wants
    the history rather than the last entry."""
    return [
        record
        for record in records
        if isinstance(record, dict) and record.get("record") == RECORD_DRIFT_DETECTED
    ]


def render(report, decision=None) -> list:
    """The report and, when given, the decision, as the operator reads them."""
    lines = [f"drift: {report['verdict']}"]
    for row in report["subjects"]:
        lines.append(f"  {row['subject']}: {row['status']} ({row['reason']})")
    counts = report["counts"]
    lines.append(
        "  matched {matched}, drifted {drifted}, missing {missing}, "
        "unexpected {unexpected}, unverified {unverified}".format(**counts)
    )
    if decision is not None:
        lines.append(f"response: {decision['response']} ({decision['reason']})")
    return lines

"""The running half of enforced policy-drift control (roadmap item 469,
issue #2758): the monitor that re-measures local subjects, the WAL record it
writes, and the two actuators that narrow authority.

:mod:`revl.drift` is the pure half. It owns the admitted manifest, the verdict
over a re-measurement, the configured response, and the record shape, and it
re-hashes nothing and suspends nothing itself (see its header for that split).
This module is the shell around it, and it keeps the same boundary: the verdict
is still :func:`revl.drift.detect`, the response is still
:func:`revl.drift.respond` / :func:`revl.drift.resolve`, and the record is still
:func:`revl.drift.wal_record`. Nothing here re-argues a decision.

WHAT THIS SLICE MEASURES. The subjects whose bytes are local: a `tool` is an
emitted artifact *tree*, hashed with :func:`revl.deploy.artifact_digest` (the
same tree digest deploy re-computes on a receiver), and a `policy` is a file,
hashed with :func:`revl.drift.digest`. A subject is declared once, at admission,
as `{role, name, kind, path}`; :func:`admit` derives the manifest from those
bytes rather than asking an author to type a hash they cannot type. A later pass
re-reads the same declarations and builds the reading :func:`revl.drift.detect`
takes.

THE UNVERIFIED RULE IS IN THE READING, NOT ONLY IN THE VERDICT. `detect` can
only tell "measured and absent" (`None`) from "not measured" (key absent) if
the producer keeps them apart. This monitor does: a declared path that is gone
is `None` (an admitted subject the world no longer has), and a path that exists
but can no longer be measured as the kind it was admitted as (a symlink, an
empty tree, an unreadable file, a tree replaced by a file) is left OUT of the
digest reading and passed to `detect` as `changed`, with its cause also in
`unmeasured`, so the subject is `drifted` and earns `onDrift`. Admission
measured it, so a change of kind or readability is evidence of drift, never a
blind spot. `unverified` is kept for a subject that was never declared
measurable: a hosted model that exposes no hash has no declaration it can be
measured through, so it is not silently passed, and the pass reports it
unverified.

WHAT THIS SLICE ACTUATES, AND WHAT IT REFUSES. Only the two responses that
narrow authority are carried out:

  * `read-only` arms a durable latch (:func:`arm_read_only`), by default
    `<wal>.read-only`. The py runtime reads it at every NEW effectful crossing
    seam (extern crossings through ``estop_gated`` and ``extern_emit``, effect
    acquisition, deferred emit, approval crossing and deferred fire), so a
    component may still read but may no longer start a world-changing crossing.
    Teardown, inverse registration and recovery paths are deliberately not
    narrowed: a read-only hold does not strand cleanup. The runtime must be
    pointed at this exact file with `REVL_READ_ONLY_LATCH` or
    ``arm_read_only_latch``, so the path is a host obligation, not an ambient
    default the runtime already knows.
  * `suspend` arms the item-443 E-Stop latch (:func:`arm_suspend`), the halt
    the runtime and the conductor already honour.

`re-admit` and `rollback` CHANGE WHAT RUNS, so they are deliberately absent from
this slice: :func:`apply_response` refuses them by name and the pass records
that refusal, rather than quietly downgrading them to a response that looks
applied. Widening authority is never a response at all.

THE WAL. :func:`run_pass` appends the :func:`revl.drift.wal_record` to a JSON
Lines WAL, sealing a torn trailing record first (§535) so a pass never merges
with an interrupted write. The WAL has ONE writer: a host that shares a live
run's effect WAL must serialize the monitor with that writer, and the
recommended shape is a dedicated drift WAL with the run generation in its
header, which is what makes two drift records comparable.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import deploy, drift, estop, wal
from .errors import RevlError

#: The shape of a monitor document. Bumped when a key this module reads changes
#: meaning, not when a new optional key is added.
MONITOR_VERSION = 1

#: The kinds of local subject this slice can measure. A `file` is one byte
#: sequence (a policy document, a pin); a `tree` is an emitted artifact
#: directory (a tool), hashed by deploy's own tree digest.
KIND_FILE = "file"
KIND_TREE = "tree"
SUBJECT_KINDS = (KIND_FILE, KIND_TREE)

#: The tiers whose runtime checks the read-only latch at an effectful crossing.
#: This slice wires the py reference runtime; a tier that does not check it must
#: not be reported as narrowed. The record carries this list rather than claiming
#: a halt the tier cannot deliver, the same honesty `estop.tier_estop_status`
#: keeps for a suspend.
TIERS_WITH_READ_ONLY = frozenset({"py"})

#: The suffix the read-only latch takes beside a WAL, the way the E-Stop latch
#: takes `.estop`. One spelling for the monitor and the runtime's ambient
#: `REVL_READ_ONLY_LATCH` documentation.
READ_ONLY_SUFFIX = ".read-only"

#: The ambient variable a runtime reads a read-only latch from, when it is not
#: armed explicitly. Mirrors `REVL_ESTOP_LATCH`.
READ_ONLY_LATCH_ENV = "REVL_READ_ONLY_LATCH"

#: What a monitor pass claims. Narrow on purpose: it says what was measured and
#: what the control did, not that a changed subject is semantically different.
MONITOR_GUARANTEE = (
    "a declared local subject is re-measured on every pass; a measured digest "
    "that differs from the admitted one is drift; a declared subject whose path "
    "is gone is missing; an admitted subject that can no longer be measured as "
    "the kind it was admitted as is drift; a subject never declared measurable "
    "is unverified and is never reported as matched; the monitor carries out only read-only and "
    "suspend, and a response it cannot carry out is recorded as unapplied rather "
    "than downgraded."
)

#: The keys a monitor document may carry. A key this monitor does not read is
#: refused, so a misspelt `subjects` cannot look like a document with none.
_DOCUMENT_KEYS = ("version", "manifest", "subjects", "control")

#: The keys one subject declaration may carry, for the same reason.
_SUBJECT_KEYS = ("role", "name", "kind", "path")


class DriftMonitorError(drift.DriftError):
    """A monitor document, a subject declaration or a WAL that cannot be read as
    what it claims to be. Fail closed: a monitor that cannot tell whether it
    measured the admitted bytes must not be the thing that decides whether to
    suspend."""


class DriftActuationError(DriftMonitorError):
    """A configured response this slice cannot carry out. `re-admit` and
    `rollback` change what runs, so they are not implemented here; they are
    refused by name rather than downgraded to a narrowing response."""


class _SubjectAbsent(Exception):
    """The declared subject is not present at its path. This is the measured
    absence `detect` reads as `missing`, distinct from a subject that is present
    but can no longer be measured as its admitted kind (`drifted`)."""


# ---------------------------------------------------------------------------
# the admission document: declarations in, derived manifest out
# ---------------------------------------------------------------------------


def _iso(at) -> str:
    """Normalize `at` to an ISO-8601 UTC string, defaulting to now. The monitor
    is the runtime shell, so it may read a clock; every function that writes a
    record also accepts one, which keeps a pass reproducible in a test."""
    if at is None:
        at = datetime.now(timezone.utc)
    if isinstance(at, datetime):
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at.astimezone(timezone.utc).isoformat()
    if isinstance(at, str) and at:
        return at
    raise DriftMonitorError(f"`at` must be an ISO timestamp or a datetime, got {at!r}")


def _declarations(value) -> list:
    """Validate the subject declarations and return them normalized, with each
    path absolute so a later pass measures the same bytes whatever its working
    directory is."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise DriftMonitorError(
            f"`subjects` must be a list of {{role, name, kind, path}}, "
            f"got {type(value).__name__}"
        )
    rows = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise DriftMonitorError(
                f"a subject declaration must be a mapping, got {type(item).__name__}"
            )
        unknown = sorted(key for key in item if key not in _SUBJECT_KEYS)
        if unknown:
            raise DriftMonitorError(
                f"a subject declaration carries {unknown}, which this monitor does "
                f"not read; it reads {list(_SUBJECT_KEYS)}"
            )
        role = item.get("role")
        name = item.get("name")
        kind = item.get("kind")
        path = item.get("path")
        if role not in drift.ROLES:
            raise DriftMonitorError(
                f"a subject declaration names {role!r}, which is not one of the "
                f"drift roles {drift.ROLES}"
            )
        if not isinstance(name, str) or not name:
            raise DriftMonitorError(f"role {role!r} has an empty subject name")
        if kind not in SUBJECT_KINDS:
            raise DriftMonitorError(
                f"{role}:{name} declares kind {kind!r}; this monitor measures "
                f"{list(SUBJECT_KINDS)}"
            )
        if not isinstance(path, str) or not path:
            raise DriftMonitorError(f"{role}:{name} declares no path")
        key = (role, name)
        if key in seen:
            raise DriftMonitorError(
                f"{role}:{name} is declared twice; one subject has one reading"
            )
        seen.add(key)
        rows.append(
            {"role": role, "name": name, "kind": kind, "path": os.path.abspath(path)}
        )
    return rows


def _label(row) -> str:
    return f"{row['role']}:{row['name']}"


def _digest_subject(row, *, where: str) -> str:
    """The digest of one declared subject, or a refusal.

    `_SubjectAbsent` is raised only when the path is genuinely absent. Anything
    else that stops a measurement is a `DriftMonitorError`. Admission refuses
    the subject on it; a live reading reports the admitted subject `drifted`
    with that cause, because its kind or readability is no longer what
    admission measured.
    """
    path = Path(row["path"])
    label = _label(row)
    if row["kind"] == KIND_TREE:
        try:
            return deploy.artifact_digest(path)
        except FileNotFoundError as error:
            raise _SubjectAbsent(f"{where}: {label} is absent at {path}") from error
        except RevlError as error:
            raise DriftMonitorError(
                f"{where}: {label} at {path} cannot be measured as an artifact "
                f"tree: {error}"
            ) from error
        except OSError as error:
            raise DriftMonitorError(
                f"{where}: {label} at {path} cannot be read as an artifact tree: "
                f"{error}"
            ) from error

    try:
        if path.is_symlink():
            raise DriftMonitorError(
                f"{where}: {label} at {path} is a symlink, so the bytes it binds "
                "live outside the admitted path and can change after admission"
            )
        data = path.read_bytes()
    except FileNotFoundError as error:
        raise _SubjectAbsent(f"{where}: {label} is absent at {path}") from error
    except OSError as error:
        raise DriftMonitorError(
            f"{where}: {label} at {path} cannot be read as a file: {error}"
        ) from error
    return drift.digest(data)


def _manifest(document) -> dict:
    if not isinstance(document, dict):
        raise DriftMonitorError(
            f"a monitor document must be a mapping, got {type(document).__name__}"
        )
    unknown = sorted(key for key in document if key not in _DOCUMENT_KEYS)
    if unknown:
        raise DriftMonitorError(
            f"the monitor document carries {unknown}, which this monitor does not "
            f"read; it reads {list(_DOCUMENT_KEYS)}"
        )
    if document.get("version") != MONITOR_VERSION:
        raise DriftMonitorError(
            f"the monitor document is version {document.get('version')!r}; this "
            f"monitor reads version {MONITOR_VERSION}"
        )
    manifest = drift.normalize_manifest(document.get("manifest"))
    return manifest


def admit(declarations) -> dict:
    """Hash each declared local subject and return the monitor document.

    This is the admission moment for the local subjects: the manifest is DERIVED
    from the bytes, never typed. A subject whose path is absent, a symlink, an
    empty tree or otherwise unmeasurable is refused here, because admitting a
    subject without a digest is admitting nothing to compare against.
    """
    rows = _declarations(declarations)
    manifest = {}
    for row in rows:
        try:
            value = _digest_subject(row, where="admission")
        except _SubjectAbsent as error:
            raise DriftMonitorError(f"cannot admit {_label(row)}: {error}") from error
        manifest.setdefault(row["role"], {})[row["name"]] = value
    return {
        "version": MONITOR_VERSION,
        "manifest": manifest,
        "subjects": rows,
    }


def measure(document) -> dict:
    """Re-measure the declared subjects and build the live reading.

    Returns `{live, changed, unmeasured, declarations}`. `live` is the reading
    :func:`revl.drift.detect` takes: a declared subject whose path is gone is
    `None`. An admitted subject that exists but can no longer be measured as
    its declared kind is absent from `live`, named in `changed` (detect's
    `changed` argument, so it is `drifted`) and carries its cause in
    `unmeasured`. A manifest subject with no declaration at all is absent from
    both, so the verdict reports it `unverified` rather than passing it.
    """
    manifest = _manifest(document)
    rows = _declarations(document.get("subjects"))
    live = {}
    changed = {}
    unmeasured = []
    declared = set()
    for row in rows:
        key = (row["role"], row["name"])
        declared.add(key)
        try:
            value = _digest_subject(row, where="the live reading")
        except _SubjectAbsent as error:
            live.setdefault(row["role"], {})[row["name"]] = None
            unmeasured.append(
                {
                    "subject": _label(row),
                    "status": "missing",
                    "reason": f"{error}; the reading reports this admitted subject "
                    "absent",
                }
            )
        except DriftMonitorError as error:
            if row["name"] in manifest.get(row["role"], {}):
                reason = (
                    f"{error}; admission measured this subject as a "
                    f"{row['kind']}, so a change of kind or readability is drift"
                )
                changed.setdefault(row["role"], {})[row["name"]] = reason
                status = "drifted"
            else:
                reason = str(error)
                status = "unverified"
            unmeasured.append(
                {"subject": _label(row), "status": status, "reason": reason}
            )
        else:
            live.setdefault(row["role"], {})[row["name"]] = value
    for role in sorted(manifest):
        for name in sorted(manifest[role]):
            if (role, name) not in declared:
                unmeasured.append(
                    {
                        "subject": f"{role}:{name}",
                        "status": "unverified",
                        "reason": "the monitor document declares no local path for "
                        "this admitted subject, so it was not measured; a "
                        "hosted model or a peer whose bytes are not local "
                        "stays unverified rather than passing",
                    }
                )
    return {
        "live": live,
        "changed": changed,
        "unmeasured": unmeasured,
        "declarations": rows,
    }


# ---------------------------------------------------------------------------
# the actuators: only the two that narrow authority
# ---------------------------------------------------------------------------


def read_only_latch_path(path: str) -> str:
    """The read-only latch beside `path`'s WAL, the way the E-Stop latch is
    `<wal>.estop`."""
    return f"{path}{READ_ONLY_SUFFIX}"


def suspend_latch_path(path: str) -> str:
    """The E-Stop latch a `suspend` response arms."""
    return estop.latch_path(None, path, env=False)


def _write_latch(path: str, record: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except (OSError, ValueError):  # pragma: no cover (exotic file objects)
            pass


def read_read_only(path: str | None) -> dict | None:
    """The read-only hold at `path`, or None when the latch is absent.

    A latch that exists but does not parse still reads as read-only, the same
    fail-closed rule :func:`revl.estop.read_latch` applies to the E-Stop: an
    authority narrowing that a corrupted file can switch off is not a narrowing.
    Only a genuinely absent latch reads as absent; every other `OSError` fails
    closed. The py runtime mirrors this rule in ``_read_only_latch_record`` and
    a test pins the two readers together.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except OSError:
        return {
            "readOnly": True,
            "reason": "read-only latch (unreadable)",
            "operator": "unknown",
        }
    except (ValueError, TypeError):
        return {
            "readOnly": True,
            "reason": "read-only latch (unreadable)",
            "operator": "unknown",
        }
    if not isinstance(record, dict):
        return {
            "readOnly": True,
            "reason": "read-only latch (unreadable)",
            "operator": "unknown",
        }
    return record


def arm_read_only(
    path: str, *, reason: str = "", at=None, generation=None, detail=None
) -> dict:
    """Arm the durable read-only hold. Idempotent: a hold already in force is
    not overwritten, so the FIRST reason is the one that survives, the same rule
    the E-Stop keeps. Returns what happened, with `armed` saying whether this
    call wrote it."""
    if not isinstance(path, str) or not path:
        raise DriftActuationError(
            "a read-only hold needs a non-empty latch path; without one there is "
            "no file for a runtime to read"
        )
    existing = read_read_only(path)
    if existing is not None:
        return {"armed": False, "alreadyArmed": True, "latch": path, **existing}
    record = {
        "readOnly": True,
        "verdict": "read-only",
        "reason": reason or "drift control: read-only",
        "operator": "revl.drift",
        "at": _iso(at),
        "generation": generation,
        "resumable": True,
    }
    if detail:
        record["drift"] = dict(detail)
    try:
        _write_latch(path, record)
    except OSError as error:
        return {
            "armed": False,
            "latch": path,
            "reason": f"cannot write the read-only latch: {error}",
        }
    return {"armed": True, "latch": path, **record}


def arm_suspend(
    path: str, *, reason: str = "", at=None, wal_path=None, generation=None, detail=None
) -> dict:
    """Arm the item-443 E-Stop latch, in the record `revl estop` already
    writes, so every existing latch reader (the py seam, the conductor,
    `revl estop --report`, recovery) observes a suspend without a reader change.
    Idempotent, for the same reason as :func:`arm_read_only`."""
    if not isinstance(path, str) or not path:
        raise DriftActuationError(
            "a suspend needs a non-empty latch path; without one there is no file "
            "for a runtime to read"
        )
    existing = estop.read_latch(path)
    if existing is not None:
        return {"armed": False, "alreadyHalted": True, "latch": path, **existing}
    record = {
        "halted": True,
        "verdict": "halted",
        "reason": reason or "drift control: suspend",
        "operator": "revl.drift",
        "at": _iso(at),
        "wal": wal_path,
        "resumable": False,
        "reconcile": (
            f"revl recover --wal {wal_path}"
            if wal_path
            else "revl recover --wal <file>"
        ),
    }
    if generation is not None:
        record["generation"] = generation
    if detail:
        record["drift"] = dict(detail)
    try:
        _write_latch(path, record)
    except OSError as error:
        return {
            "armed": False,
            "latch": path,
            "reason": f"cannot write the suspend latch: {error}",
        }
    return {"armed": True, "latch": path, **record}


def _runtime_latch_path() -> str | None:
    """The read-only latch path the py runtime reads: the one armed in this
    process with `arm_read_only_latch` when the runtime is loaded, else the
    ambient `REVL_READ_ONLY_LATCH` it falls back to."""
    runtime = sys.modules.get("runtime")
    reader = getattr(runtime, "read_only_latch_path", None)
    if callable(reader):
        return reader()
    return os.environ.get(READ_ONLY_LATCH_ENV) or None


def _runtime_reads_latch(latch: str) -> bool:
    """Whether the runtime's read-only latch is exactly `latch`. A hold the
    running process never reads is armed, not applied."""
    path = _runtime_latch_path()
    if not path:
        return False
    return os.path.realpath(path) == os.path.realpath(latch)


def apply_response(
    response,
    *,
    reason: str = "",
    at=None,
    generation=None,
    wal_path=None,
    read_only_latch=None,
    suspend_latch=None,
    tier=None,
    detail=None,
) -> dict:
    """Carry out `response`, or refuse it by name.

    Only `none`, `read-only` and `suspend` are accepted. `re-admit` and
    `rollback` change what runs and are not part of this slice, so they raise
    :class:`DriftActuationError` instead of being silently replaced by a
    narrowing response; widening authority is never an option at all. The
    returned mapping records whether the act is IN FORCE (`applied`), which
    latch holds it, and which tiers actually honour it.
    """
    if response == drift.RESPONSE_NONE:
        return {
            "response": response,
            "applied": False,
            "actuator": None,
            "reason": "the control removes no authority for this verdict",
        }
    if response == drift.RESPONSE_READ_ONLY:
        latch = read_only_latch or (
            read_only_latch_path(wal_path) if wal_path else None
        )
        if latch is None:
            raise DriftActuationError(
                "read-only needs a latch path: pass one explicitly or a "
                "wal_path to derive <wal>.read-only"
            )
        armed = arm_read_only(
            latch, reason=reason, at=at, generation=generation, detail=detail
        )
        is_armed = bool(armed.get("armed") or armed.get("alreadyArmed"))
        enforced = bool(
            tier in TIERS_WITH_READ_ONLY and _runtime_reads_latch(latch)
        ) if tier else False
        return {
            "response": response,
            # The latch is ARMED whether or not a runtime watches this path.
            # `applied` is the stronger claim: the named tier honours the latch
            # AND the runtime is pointed at THIS path. A host that has not
            # pointed the runtime at it (REVL_READ_ONLY_LATCH unset or naming
            # another file) has narrowed nothing yet.
            "armed": is_armed,
            "enforced": enforced,
            "applied": is_armed and enforced,
            "actuator": "read-only",
            "latch": latch,
            "reason": (
                armed.get("reason")
                if not is_armed
                else (
                    "the read-only latch is armed and the named tier reads it; "
                    "every new effectful crossing refuses, local reads still proceed"
                    if enforced
                    else "the read-only latch is armed, but no named tier's "
                    "runtime reads this path; a host must point its runtime's "
                    "REVL_READ_ONLY_LATCH at it before the narrowing is enforced"
                )
            ),
            "honors": sorted(TIERS_WITH_READ_ONLY),
            "honored": (tier in TIERS_WITH_READ_ONLY if tier else None),
            "tier": tier,
            "detail": armed,
        }
    if response == drift.RESPONSE_SUSPEND:
        latch = suspend_latch or (suspend_latch_path(wal_path) if wal_path else None)
        if latch is None:
            raise DriftActuationError(
                "suspend needs a latch path: pass one explicitly or a wal_path to "
                "derive <wal>.estop"
            )
        armed = arm_suspend(
            latch,
            reason=reason,
            at=at,
            wal_path=wal_path,
            generation=generation,
            detail=detail,
        )
        in_force = bool(armed.get("armed") or armed.get("alreadyHalted"))
        return {
            "response": response,
            "armed": in_force,
            "enforced": bool(
                tier and estop.tier_estop_status(tier) == estop.ESTOP_HONORING
            ),
            "applied": in_force,
            "actuator": "suspend",
            "latch": latch,
            "reason": (
                armed.get("reason")
                if not in_force
                else "the E-Stop latch is armed; a latch-honouring "
                "runtime halts at its next crossing"
            ),
            "honors": sorted(estop.TIERS_WITH_ESTOP),
            "honored": (estop.tier_estop_status(tier) if tier else None),
            "tier": tier,
            "detail": armed,
        }
    raise DriftActuationError(
        f"the control fires {response!r}, which this slice does not carry out; "
        f"it carries out {drift.RESPONSE_READ_ONLY!r} and "
        f"{drift.RESPONSE_SUSPEND!r} only. `re-admit` and `rollback` change what "
        "runs and are the follow-up, so this response is refused rather than "
        "downgraded"
    )


# ---------------------------------------------------------------------------
# the WAL writer
# ---------------------------------------------------------------------------


def _generation(path: str) -> int:
    try:
        header = wal.read_wal(path)["header"]
    except FileNotFoundError:
        return 1
    generation = header.get("generation") if isinstance(header, dict) else 1
    return generation if isinstance(generation, int) and generation >= 1 else 1


def append_record(path: str, record: dict, *, generation: int = 1) -> None:
    """Append one record to the drift WAL, sealing a torn tail first (§535).

    The header is written only when this call creates the file, so a WAL that
    already carries the run's generation keeps it. The WAL is single-writer;
    see the module header for the host's obligation.
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # Seal BEFORE deciding whether a header is needed: a file holding only a
    # torn header is truncated to empty by the seal, and a record appended after
    # that would otherwise live in a headerless WAL forever.
    if os.path.exists(path) and os.path.getsize(path) > 0:
        wal.seal_torn_tail(path)
    fresh = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8") as handle:
        if fresh:
            # The header comes from the one shared builder, so the monitor does
            # not carry a second copy of the WAL schema or its guarantee. The
            # monitor's own claim lives in `record["monitor"]`, not in the
            # header, which is the sentence recovery is allowed to claim.
            handle.write(
                json.dumps(wal.header_record(generation), sort_keys=True) + "\n"
            )
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except (OSError, ValueError):  # pragma: no cover (exotic file objects)
            pass


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------


def _decision(report, control):
    """`(decision, refusal, demanded)` for a report.

    `decision` is `drift.resolve`'s answer. When it cannot be resolved (a
    rollback policy with no pinned target, say), `decision` is None, `refusal`
    says why, and `demanded` is the response the policy DID demand, so the
    durable record can state what was required rather than losing it as "no
    response".
    """
    try:
        return drift.resolve(report, control), None, None
    except drift.DriftError as error:
        try:
            demanded = drift.respond(report, control)["response"]
        except drift.DriftError:
            demanded = None
        return None, str(error), demanded


def run_pass(
    document,
    *,
    wal_path: str,
    control=None,
    generation=None,
    at=None,
    read_only_latch=None,
    suspend_latch=None,
    tier=None,
) -> dict:
    """One monitor pass: measure, verdict, decide, act, record.

    A `matched` pass writes nothing, so a loop does not fill the WAL with
    confirmations. Every other verdict writes one `drift-detected` record,
    including the unverified verdict that fires no response, because "nobody
    measured this" is exactly the blind spot an operator has to see. The
    returned mapping carries the report and the decision so a caller can render
    or assert on them without re-deriving anything.
    """
    if not isinstance(wal_path, str) or not wal_path:
        raise DriftMonitorError("a monitor pass needs a `wal_path` to record to")
    manifest = _manifest(document)
    reading = measure(document)
    report = drift.detect(manifest, reading["live"], reading["changed"])
    if generation is None:
        generation = _generation(wal_path)
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        generation = 1
    stamp = _iso(at)

    control = control if control is not None else document.get("control")
    # Validate the control BEFORE the matched early return: a misspelt key must
    # be refused on every pass, not only on the pass that happens to drift,
    # because a control the monitor cannot read is not a control it can claim to
    # be enforcing.
    try:
        normalized_control = drift.normalize_policy(control)
    except drift.DriftError as error:
        raise DriftMonitorError(f"the drift control cannot be read: {error}") from error
    decision, refused, demanded = _decision(report, control)
    if report["verdict"] == drift.VERDICT_MATCHED:
        return {
            "verdict": report["verdict"],
            "response": drift.RESPONSE_NONE,
            "applied": False,
            "unapplied": False,
            "report": report,
            "decision": decision,
            "reading": reading,
            "record": None,
            "actuation": None,
        }

    if decision is None:
        actuation = {
            "response": demanded,
            "applied": False,
            "actuator": None,
            "reason": f"the response could not be resolved: {refused}",
            "refused": refused,
        }
        recorded_decision = {
            "response": demanded,
            "trigger": "refused",
            "subject": None,
            "policy": normalized_control,
            "reason": refused,
        }
    else:
        try:
            actuation = apply_response(
                decision["response"],
                reason=decision["reason"],
                at=stamp,
                generation=generation,
                wal_path=wal_path,
                read_only_latch=read_only_latch,
                suspend_latch=suspend_latch,
                tier=tier,
                detail={"verdict": report["verdict"], "subject": decision["subject"]},
            )
        except DriftActuationError as error:
            actuation = {
                "response": decision["response"],
                "applied": False,
                "actuator": None,
                "reason": str(error),
                "refused": str(error),
            }
        recorded_decision = decision

    record = drift.wal_record(
        report, recorded_decision, generation=generation, at=stamp
    )
    record["monitor"] = {
        "version": MONITOR_VERSION,
        "guarantee": MONITOR_GUARANTEE,
        "subjects": [
            {"role": row["role"], "name": row["name"], "kind": row["kind"]}
            for row in reading["declarations"]
        ],
        "unmeasured": reading["unmeasured"],
    }
    record["actuation"] = actuation
    append_record(wal_path, record, generation=generation)
    unapplied = actuation.get("response") not in (
        None,
        drift.RESPONSE_NONE,
    ) and not actuation.get("applied")
    return {
        "verdict": report["verdict"],
        "response": actuation["response"],
        "applied": bool(actuation.get("applied")),
        "unapplied": unapplied,
        "report": report,
        "decision": decision,
        "reading": reading,
        "record": record,
        "actuation": actuation,
    }


def render(result) -> list:
    """The pass as the operator reads it: the verdict, the unmeasured subjects
    with their causes, the response and whether it is in force."""
    lines = drift.render(result["report"], result.get("decision"))
    for row in result["reading"]["unmeasured"]:
        lines.append(f"  {row['status']}: {row['subject']} ({row['reason']})")
    actuation = result.get("actuation")
    if actuation is not None:
        if actuation.get("applied"):
            state = "in force"
        elif actuation.get("armed"):
            state = "armed, not enforced"
        else:
            state = "NOT in force"
        lines.append(
            f"actuation: {actuation.get('response')} {state} "
            f"({actuation.get('reason')})"
        )
        if actuation.get("refused"):
            lines.append(f"  refused: {actuation['refused']}")
    return lines

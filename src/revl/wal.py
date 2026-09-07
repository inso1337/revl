"""The tier-agnostic write-ahead-log core (roadmap item 322, Slice 1).

Crash recovery (`revl recover`, :mod:`revl.recovery`) reads a durable WAL and
proves a way back. Until item 322 the reader lived on the py backend
(``backends/python/replay.py``'s :class:`WriteAheadLog`), so ``recover`` could
only read a WAL the *in-process py driver* wrote. But the WAL is JSON Lines —
one header line, one line per record, a terminal marker — and nothing about
reading it back is py-specific. This module is that reader plus the schema
constants, factored OUT of the py backend so recover reads a WAL produced by
ANY tier's runtime, py or a rust/go/java/wasm subprocess.

The split, precisely:

* the py in-process driver keeps WRITING through ``replay.WriteAheadLog`` exactly
  as before — byte-identical, the py recovery/replay/wal tests are the guard;
* the READ side and the schema constants live here, and both ``recover`` and the
  py writer agree on them (``test_wal_core_agrees_with_py_replay`` pins the
  agreement so the two copies can never silently drift);
* a non-py tier (go first, item 322 Slice 1) writes the SAME JSON Lines schema
  from its subprocess into a host-visible WAL file; recover reads it here with
  no py backend on the path at all.

The record schema a durable WAL speaks (all a tier must emit to be recoverable):

* ``header``            — ``{walVersion, generation, guarantee}``, first line.
* ``discharge-descriptor`` — ``{seq, entry, call:{receiver,method,args},
  origin, witness, idempotency}``; the re-issuable named call for one
  ``transactional`` inverse or one ``compensation`` (items 243/247). This is the
  record a non-py tier writes at REGISTRATION so a fresh process can re-issue it.
* ``discharge``         — ``{discharged:[seq...]}``; the commit-path proof that
  those seqs were committed (transactional) or discharged (compensation). Its
  presence makes recover SKIP the seq — a committed transaction is never rolled
  back.
* ``effect``            — the legacy per-step boundary record the py timeline
  writes; a non-py tier need not emit it (the descriptor path is enough).
* ``commit-approved`` / ``deferred-emission`` / ``flushed`` / ``flush-residue``
  / ``aborted`` — the item 245 session-commit records; py-only for now, read
  here uniformly so a tier that later emits them is handled with no reader change.
* ``model-decision``     — ``{component, stepIndex, outcome, llm}``; one model
  completion made durable at its crossing (item 250 Slice 3a), keyed on the
  completion's own ``effect`` record. ``llm`` is the trace hop's payload
  (model, tokens, cost, latency bracket, attempts against the ceiling, each
  with its provenance); no prompt or response text, no digest. Present only
  for a crossing that carried a completion; consumes no seq. Recovery ignores
  it (it names a fact, not an effect); :func:`model_decisions` indexes it for
  the offline branch surface.
* ``activation-complete`` — the terminal marker. Its PRESENCE is roll-forward,
  its ABSENCE (the crash) is roll-back. The whole decision.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import warnings
from dataclasses import dataclass

#: The on-disk WAL format version. Bump only on a breaking schema change; the
#: header carries it so a reader can refuse a version it does not understand.
WAL_VERSION = 1

#: The versions this reader understands. A WAL whose header names anything else
#: is refused rather than read as if current (item 413). Extend this set, never
#: replace :data:`WAL_VERSION`, if a future reader stays backward compatible.
SUPPORTED_WAL_VERSIONS = frozenset({WAL_VERSION})


class WALIntegrityError(RuntimeError):
    """A WAL failed an integrity gate on read (item 413).

    Raised for a header version this reader does not support, or for MID-FILE
    corruption (a torn or unparseable line with valid records after it). It is
    deliberately NOT raised for a torn TRAILING line, which is the expected
    crash-interrupted-write case recovery exists to tolerate. Raising fails
    CLOSED: a corrupt or version-mismatched WAL stops recovery loudly instead of
    silently dropping records (a dropped ``discharge`` would replay a committed
    transaction's rollback; a dropped ``flushed`` would re-owe a fired emission).

    The gate never reads back approval or grant records, so authority injection
    stays structurally impossible; it only protects the cleanup path's integrity.
    """


#: The single sentence recovery is allowed to claim. Deliberately narrow. Kept
#: byte-identical to ``replay.WAL_GUARANTEE`` (pinned by a test) because it is
#: written verbatim into every WAL header, py or non-py.
WAL_GUARANTEE = (
    "the WAL records each committed effect's step identity, boundary "
    "classification and inverse DESCRIPTOR (not its closure). On restart, "
    "recovery runs the reconstructible boundary inverses newest-first (LIFO); "
    "in-process inverses are moot (their captured memory died with the "
    "process) and closure-only boundary inverses are reported as residue, "
    "never silently claimed to have run."
)


# ---------------------------------------------------------------------------
# the step-kind vocabulary and the fork's scope gate (item 250, Slice 2)
# ---------------------------------------------------------------------------
#
# Slice 1 classified a fork's tail from the LIVE timeline, so the vocabulary and
# the scope gate lived on the py backend. Slice 2 reads the same partition back
# out of a durable WAL with no backend on the path, so both are mirrored here —
# the same split item 322 made for the reader, pinned the same way
# (``test_wal_core_agrees_with_py_replay``) so the two copies cannot drift.

KIND_EFFECT = "effect"
KIND_PROVISION = "provision"
KIND_EMISSION = "emission"
KIND_COMPENSATION = "compensation"
KIND_BOUNDARY = "boundary"
KIND_HINGE = "hinge"
KIND_OPAQUE = "opaque"

KINDS = (KIND_EFFECT, KIND_PROVISION, KIND_EMISSION, KIND_COMPENSATION,
         KIND_BOUNDARY, KIND_HINGE, KIND_OPAQUE)

#: The capability tokens an inverse may declare and still be provably
#: HOST-CONFINED, so a fork rewind may run it. Byte-identical to
#: ``replay.HOST_CONFINED_CAPS``; item 250 Decision 2 says why `fs` is the only
#: one.
HOST_CONFINED_CAPS = frozenset({"fs"})


def scope_host_confined(scope) -> bool:
    """Whether a recorded capability SCOPE is provably host-confined (item 250,
    Decision 2). Behaviourally identical to ``replay.scope_host_confined``: an
    unknown token reads as CROSSING, the fail-safe direction — the honest move is
    to enumerate an inverse, never to run one."""
    if scope is None:
        return True
    if scope.get("sandbox") or scope.get("confined"):
        return True
    caps = tuple(scope.get("caps") or ())
    if not caps:
        return True
    return all(cap in HOST_CONFINED_CAPS for cap in caps)


# ---------------------------------------------------------------------------
# the durable model decision (item 250, Slice 3a)
# ---------------------------------------------------------------------------

#: The record kind that makes one model completion durable. Byte-identical to
#: ``replay.RECORD_MODEL_DECISION`` (pinned by the agreement test): the py
#: writer names it and this reader indexes it, and a drift would leave every
#: decision invisible to `revl compare` while the WAL still carried it.
RECORD_MODEL_DECISION = "model-decision"


def model_decisions(records: list) -> dict:
    """Index a WAL's ``model-decision`` records by the crossing they describe,
    ``(component, stepIndex) -> record``, in recorded order.

    The key is the completion's own ``effect`` record identity, the one thing
    the writer and a post-mortem reader both hold. A WAL written before Slice
    3a has no such record and indexes to ``{}``; that is indistinguishable from
    a run that made no completion, which the offline surface states rather than
    guesses at (see :data:`revl.branch.NOT_COMPARABLE`)."""
    out: dict = {}
    for record in records:
        if record.get("record") != RECORD_MODEL_DECISION:
            continue
        out[(record.get("component"), record.get("stepIndex"))] = record
    return out


def read_wal(path: str) -> dict:
    """Load a WAL from disk into ``{header, records, complete, torn}``.

    Tier-agnostic: it parses JSON Lines and classifies by the ``record`` field,
    so it reads a WAL written by the py in-process driver or by a non-py tier's
    subprocess identically. ``complete`` is whether the terminal
    ``activation-complete`` marker is present. A trailing half-written line (a
    genuine ``kill -9`` can leave one) is tolerated and reported as ``torn``
    rather than crashing the recovery that exists to handle exactly that.

    Two integrity gates run ahead of the roll-forward/roll-back decision (item
    413), both fail-closed via :class:`WALIntegrityError`:

    * a header whose ``walVersion`` is not in :data:`SUPPORTED_WAL_VERSIONS` is
      REFUSED, not read as if current;
    * an unparseable line is tolerated ONLY when it is the last line in the file
      (the crash-interrupted write). An unparseable line with any content after
      it is MID-FILE corruption and is refused, because silently skipping it
      would drop a committed record and corrupt cleanup replay.

    A missing header is left as ``{}`` (an empty or pre-header WAL is a valid
    nothing-to-recover input); the version gate only fires when a header exists.
    The gate reads no approval or grant record, so authority injection stays
    impossible. On a supported version with a clean or trailing-torn file the
    output is byte-identical to the pre-413 reader, so
    ``test_wal_core_agrees_with_py_replay`` still pins this to
    ``replay.WriteAheadLog.read``.
    """
    header: dict = {}
    records: list = []
    complete = False
    torn = False
    # First pass: find the index of the last non-empty (content) line. This
    # avoids building a list of all lines in memory for large WALs while still
    # allowing the same trailing-torn-line detection as before.
    last_content = -1
    with open(path, encoding="utf-8") as handle:
        for i, raw_line in enumerate(handle):
            if raw_line.strip():
                last_content = i

    # Second pass: parse and classify records. Streaming the file avoids holding
    # all lines in memory at once.
    with open(path, encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                if index == last_content:
                    torn = True   # a partial FINAL record: the crash itself
                    continue
                raise WALIntegrityError(
                    f"WAL {path} is corrupt at line {index + 1}: an unparseable "
                    f"record with {last_content - index} line(s) after it. This is "
                    "mid-file corruption, not a crash-torn trailing line; refusing "
                    "to read past it would silently drop committed records."
                ) from None
            kind = entry.get("record")
            if kind == "header":
                header = entry
                _check_version(header, path)
            elif kind == "activation-complete":
                complete = True
                records.append(entry)
            else:
                records.append(entry)
    return {"header": header, "records": records,
            "complete": complete, "torn": torn}


def _check_version(header: dict, path: str) -> None:
    """Refuse a WAL whose header names a version this reader cannot read.

    Only called when a header record is present, so an empty or pre-header WAL
    stays a valid nothing-to-recover input.
    """
    version = header.get("walVersion")
    if version not in SUPPORTED_WAL_VERSIONS:
        raise WALIntegrityError(
            f"WAL {path} declares walVersion {version!r}, which this reader does "
            f"not support (supported: {sorted(SUPPORTED_WAL_VERSIONS)}). Refusing "
            "to read an incompatible WAL as if it were the current format."
        )


class NonDurableWALWarning(UserWarning):
    """The approval WAL is being written somewhere the OS may clear (item 413,
    issue #289).

    Raised as a warning, not an error, and the reasoning is deliberate. Refusing
    to start was considered and rejected:

    * the WAL is the gate's *record*, not its *enforcement*. The gate still
      refuses everything it would refuse with a durable WAL; what is lost is the
      ability to replay or audit a decision after a crash. Turning a degraded
      record into a dead session takes a host that works (read-only HOME,
      immutable container image, locked-down CI runner) and stops it working;
    * the load path is already fail-closed where it matters — ``session.load``
      refuses a policy load with no recording at all. A WAL that exists but sits
      on volatile storage is strictly more recoverable than no WAL;
    * a refusal is trivially worked around by setting ``REVL_WAL_DIR`` to a
      tempdir, which reintroduces the silent non-durability this warning exists
      to end, except now it is invisible again *and* looks deliberate.

    So the fallback stays, and the loss of the property becomes loud instead:
    named durable candidates, the reason each failed, where the WAL actually
    landed, and what it costs. `revl doctor` reports the same fact as a WARN.
    """


@dataclass(frozen=True)
class WALDirResolution:
    """Where the approval WAL directory resolved to, and whether it is durable.

    ``attempts`` is the ordered ``(candidate, reason)`` list of durable locations
    that could NOT be created — empty when the first candidate worked. It is the
    evidence half of the warning: an operator needs the *why* (a read-only HOME
    reads very differently from a HOME that does not exist) far more than the
    bare fact that the fallback happened.
    """

    directory: str
    durable: bool
    attempts: tuple[tuple[str, str], ...] = ()

    def summary(self) -> str:
        """One line, for a table row (`revl doctor`)."""
        if self.durable:
            return f"durable approval-WAL directory {self.directory}"
        tried = "; ".join(f"{path} ({reason})" for path, reason in self.attempts)
        return ("NOT durable — the approval WAL falls back to the process "
                f"tempdir {self.directory}, which the OS may clear at any time. "
                f"Durable locations tried: {tried or 'none'}. "
                "Set $REVL_WAL_DIR to a writable durable directory.")

    def describe(self) -> str:
        """The full operator-facing explanation used for the warning text."""
        if self.durable:
            return self.summary()
        lines = [
            "the revl approval WAL is NOT DURABLE: no per-user state directory "
            "could be created, so it falls back to the process temporary "
            f"directory {self.directory}, which the operating system may clear "
            "at any time (a reboot, a tmp reaper).",
            "",
            "The WAL is the record of what the approval gate authorised. On "
            "volatile storage that record can vanish before it is read, so "
            "`revl recover` may find nothing to replay after a crash and an "
            "audit of what was approved has no source. The gate still refuses "
            "everything it would otherwise refuse — what is lost is the "
            "durability of the record, silently, until now.",
            "",
            "Durable locations tried, and why each failed:",
        ]
        if self.attempts:
            lines += [f"  - {path}: {reason}" for path, reason in self.attempts]
        else:
            lines.append("  - (none: no durable candidate was even attempted)")
        lines += [
            "",
            "To restore durability, point $REVL_WAL_DIR (or $XDG_STATE_HOME) at "
            "a writable directory on durable storage. `revl doctor` reports this "
            "same check.",
        ]
        return "\n".join(lines)


def wal_dir_candidates() -> list[str]:
    """The ordered durable candidates :func:`resolve_wal_dir` will try.

    Split out so `revl doctor` and the warning can NAME what was tried without
    re-deriving the precedence, and so the precedence itself has one definition.
    """
    override = os.environ.get("REVL_WAL_DIR")
    if override:
        return [override]
    candidates = []
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        candidates.append(os.path.join(xdg_state, "revl", "approval-wal"))
    elif sys.platform == "darwin":
        candidates.append(os.path.expanduser(
            "~/Library/Application Support/revl/approval-wal"))
    # Always keep the XDG state default as a final durable candidate so a
    # macOS host with an unwritable Application Support still lands durable.
    candidates.append(os.path.expanduser("~/.local/state/revl/approval-wal"))
    return candidates


def resolve_wal_dir() -> WALDirResolution:
    """Resolve the approval-WAL directory, reporting HOW it resolved.

    The durability logic is :func:`default_wal_dir`'s, unchanged; this is the
    same walk with the outcome kept instead of discarded, so a caller that wants
    to *report* on the setup (`revl doctor`) can do so without triggering the
    warning, and :func:`default_wal_dir` can warn off the same evidence.

    It creates the directory it returns — resolving is the probe. That is the
    only honest answer to "where will the WAL go": a candidate is durable
    exactly when it can be created ``0o700``.
    """
    attempts: list[tuple[str, str]] = []
    for directory in wal_dir_candidates():
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError as exc:
            attempts.append((directory, f"{type(exc).__name__}: {exc}"))
            continue
        return WALDirResolution(directory, True, tuple(attempts))
    return WALDirResolution(tempfile.gettempdir(), False, tuple(attempts))


def default_wal_dir() -> str:
    """The durable, per-user directory the approval WAL defaults into (item 413).

    "The gate's authority is the WAL", yet the default lived under
    :func:`tempfile.gettempdir`: reboot-wiped (a crash-plus-reboot lost the very
    recovery record recovery exists to read) and world-traversable on a shared
    host. This returns a per-user STATE directory that survives a reboot and is
    created owner-only (mode ``0o700``), so another local account can neither
    read nor splice the gate's authority:

    * ``$REVL_WAL_DIR`` when the embedder set it (an explicit host override);
    * else ``$XDG_STATE_HOME/revl/approval-wal`` when ``XDG_STATE_HOME`` is set;
    * else ``~/Library/Application Support/revl/approval-wal`` on macOS;
    * else ``~/.local/state/revl/approval-wal`` (the XDG state default).

    It always returns a directory that exists. If none of the durable candidates
    can be created (a read-only or absent HOME), it falls back to the process
    tempdir: a reboot-wiped WAL is worse than a durable one, but a gate that
    cannot open a WAL at all is worse than either, and the fail-closed load path
    (``session.load`` refuses a policy load with no recording) still holds.

    That fallback used to be SILENT, which is the whole of issue #289: the WAL
    quietly stopped providing the one property it exists for and nothing said
    so. It now raises :class:`NonDurableWALWarning` naming every durable
    candidate tried, why each failed, where the WAL actually landed, and what
    that costs. Nothing is emitted on the normal (durable) path — see
    :class:`NonDurableWALWarning` for why this warns rather than refuses.
    """
    resolution = resolve_wal_dir()
    if not resolution.durable:
        warnings.warn(resolution.describe(), NonDurableWALWarning, stacklevel=2)
    return resolution.directory


def default_wal_path(session_id: str) -> str:
    """The default durable WAL file for one session under :func:`default_wal_dir`.

    The filename keeps the ``revl-approval-<session>.wal`` spelling the tempdir
    default used, so nothing downstream that globs approval WALs needs to change;
    only the directory moves from the reboot-wiped, world-traversable tempdir to
    the owner-only per-user state directory (item 413).
    """
    return os.path.join(default_wal_dir(), f"revl-approval-{session_id}.wal")

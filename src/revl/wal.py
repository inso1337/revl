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

import importlib.util
import json
import os
import sys
import tempfile
import warnings
from dataclasses import dataclass

from ._paths import backends_root

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
#: HOST-CONFINED, so a fork rewind may run it. Item 250 Decision 2 says why `fs`
#: is the only one. Item 872: this is no longer a second COPY of the rule —
#: ``HOST_CONFINED_CAPS`` and :func:`scope_host_confined` below are handed
#: straight out of the py backend, where the live fork and the recorder that
#: writes the scope both live, so there is exactly one definition and the two
#: cannot drift.
#
# The backend is loaded by FILE through the shared `backends/` resolver (the way
# `revl.test` and `revl.gate` load a tier's emitter), never by requiring a
# backend on `sys.path`, so this reader keeps the property item 322 gave it: it
# reads a WAL with no cordis runtime and no backend importable. The py backend
# is written to be loadable that way (`replay.py` has the same fallback for its
# own sibling), and it imports no cordis at module scope.
#
# The delegation is lazy (module `__getattr__`): a caller that only reads or
# writes WAL lines never pays for loading an emitter-size module.
_PY_REPLAY = None


def _py_replay():
    """The py backend module that owns the single definition of the scope gate."""
    global _PY_REPLAY
    if _PY_REPLAY is None:
        module = sys.modules.get("replay")
        if module is None or not hasattr(module, "scope_host_confined"):
            spec = importlib.util.spec_from_file_location(
                "replay", backends_root() / "python" / "replay.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Adopt the name the backend uses for itself, but only when nothing
            # else holds it, so a later `import replay` resolves to this same
            # copy. `replay.py` does the same for its `confidential` sibling.
            sys.modules.setdefault("replay", module)
        _PY_REPLAY = module
    return _PY_REPLAY


def __getattr__(name: str):
    """Expose the backend's scope gate under this module's name (item 872).

    An unknown token already read as CROSSING; an ABSENT scope read as confined,
    so the one input no recorder could vouch for was the one assumed safe. Both
    copies of this fail-open rule are now one definition, which is why these two
    names — the gate and its token set — are the only attributes resolved here.
    """
    if name in ("HOST_CONFINED_CAPS", "scope_host_confined"):
        return getattr(_py_replay(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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

    "Unparseable" covers a line that is not valid UTF-8, not only one that is not
    valid JSON: a ``kill -9`` mid-write can tear a record in the middle of a
    multibyte character, so the file is read as BYTES and each line is decoded
    individually. A torn trailing multibyte record is classified ``torn`` exactly
    as a torn trailing JSON record is, rather than tracebacking with a
    ``UnicodeDecodeError`` the recovery exists to absorb; a non-UTF-8 line with
    content after it is the same mid-file corruption a non-JSON one is, and is
    refused the same way.

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
    with open(path, "rb") as handle:
        for i, raw_line in enumerate(handle):
            if raw_line.strip():
                last_content = i

    # Second pass: parse and classify records. Streaming the file avoids holding
    # all lines in memory at once. The file is read as BYTES so a trailing record
    # torn mid-multibyte-character decodes as `torn` rather than raising.
    with open(path, "rb") as handle:
        for index, raw_byte_line in enumerate(handle):
            if not raw_byte_line.strip():
                continue
            try:
                line = raw_byte_line.decode("utf-8").strip()
                entry = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
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


def _last_newline_offset(handle, size: int) -> int:
    """Offset of the last ``b"\\n"`` in an open binary file of ``size`` bytes, or
    ``-1`` when the file holds no newline at all. Scans backward in chunks so a
    large WAL is not read whole into memory just to find its final boundary."""
    chunk = 4096
    pos = size
    while pos > 0:
        step = min(chunk, pos)
        pos -= step
        handle.seek(pos)
        buf = handle.read(step)
        idx = buf.rfind(b"\n")
        if idx != -1:
            return pos + idx
    return -1


def seal_torn_tail(path: str) -> None:
    """Truncate a never-acknowledged partial trailing write so the WAL ends at a
    clean record boundary before it is appended to (issue #535).

    Every WAL record is written as one fsync'd ``json.dumps(record) + "\\n"``, so
    a durable WAL always ends in a newline. A file that does NOT end in a newline
    therefore carries a torn trailing line — an interrupted final write (a real
    ``kill -9`` can leave one) that was never acknowledged. Left in place, the
    next appended record MERGES onto it: the two become one line that no longer
    parses, silently swallowing the freshly appended record (a dropped
    ``replay-fence`` re-runs an undeclared inverse on every recovery — R-C1); and
    once any record follows, that unparseable line is MID-FILE corruption, which
    the item 413 gate refuses forever (R-C2).

    Sealing removes exactly that torn tail: it truncates the file back to the
    byte after its last newline (or to empty when the file has no newline at
    all), fsync'ing the result. ONLY a non-newline-terminated tail is ever
    removed, so a completed, acknowledged record — always newline-terminated — is
    never touched. This is the physical, durable form of the skip
    :func:`read_wal` already performs for a torn LAST line, applied by every
    appender so a reopened WAL stays clean instead of merging its torn tail into
    permanent corruption.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return  # no log yet: nothing to seal
    if size == 0:
        return
    with open(path, "rb") as handle:
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return  # already ends at a clean record boundary
        cut = _last_newline_offset(handle, size)
    new_len = cut + 1  # keep through the last newline; 0 when the file has none
    if new_len == size:
        return
    with open(path, "r+b") as handle:
        handle.truncate(new_len)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
            pass


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

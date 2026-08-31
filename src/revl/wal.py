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
* ``activation-complete`` — the terminal marker. Its PRESENCE is roll-forward,
  its ABSENCE (the crash) is roll-back. The whole decision.
"""

from __future__ import annotations

import json

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
    with open(path, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle]
    # A torn line is only the crash-interrupted write when it is the LAST line
    # carrying content; anything after it means real mid-file corruption.
    last_content = max((i for i, line in enumerate(lines) if line), default=-1)
    for index, line in enumerate(lines):
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

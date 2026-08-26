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

    This is the exact behaviour ``replay.WriteAheadLog.read`` had before item
    322 factored it here; the py writer's own reader delegates to nothing, so
    ``test_wal_core_agrees_with_py_replay`` pins the two to identical output.
    """
    header: dict = {}
    records: list = []
    complete = False
    torn = False
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                torn = True   # a partial final record — the crash itself
                continue
            kind = entry.get("record")
            if kind == "header":
                header = entry
            elif kind == "activation-complete":
                complete = True
                records.append(entry)
            else:
                records.append(entry)
    return {"header": header, "records": records,
            "complete": complete, "torn": torn}

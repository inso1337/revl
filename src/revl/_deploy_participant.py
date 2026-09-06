"""One participant process of a coordinated deploy (item 118, Slice 1).

Spawned by the conductor (`src/revl/deploy.py`'s :class:`ProcessParticipant`)
and driven over newline-delimited JSON on stdin/stdout — the same control
channel shape `_process_runner.py` already uses for `repoint`.

The reason this is a separate process and not a callable is the design's first
CRITICAL: **the LIFO rollback theorem `apply` proves does not cross a seam.**
Everything that would let someone else undo this participant's work lives HERE
and nowhere else:

  * the ordered accumulator of applied effects and their inverses, in this
    process's memory;
  * the durable WAL, written by this process as each effect commits;
  * the boundary state itself (a JSON `world` file this process owns).

The conductor holds a pipe and a name. When it sends ``{"op": "abort"}`` it is
making a REQUEST; the unwind below runs here, newest-first, and this process
reports what it settled to. A conductor that cannot reach us gets nothing back
and must report `unresolved` — it cannot substitute its own unwind for ours,
which is exactly why the cross-process protocol is coordinated rather than a
lift of `apply.py`'s in-process theorem.

Commands (one JSON object per line, one reply per command):

    {"op": "prepare"}  -> {"ok": bool, ...}    no effects; fully reversible by
                                               doing nothing
    {"op": "commit"}   -> {"ok": bool, ...}    apply this slice's effects in
                                               order, appending the WAL. On a
                                               local failure this process
                                               ALREADY unwound its own slice
                                               LIFO and proved its own
                                               no-residue before answering.
    {"op": "abort"}    -> {"ok": bool, ...}    run our OWN local LIFO unwind
                                               back to the prior generation
    {"op": "status"}   -> the applied set + residue as this process sees it

Spec (argv[1], a JSON file):

    {"identity": "db", "world": "<path>", "wal": "<path>",
     "effects": [{"name": "row", "reversible": true}, ...],
     "failAt": 1,               # optional: fail the commit at this index
     "refusePrepare": "why",    # optional: refuse in PREPARE instead
     "dieAtAbort": true}        # optional: exit(1) on abort, so the conductor
                                # sees an unreachable participant
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WAL_VERSION = 1


def _seal_torn_tail(path: str) -> None:
    """Truncate a never-acknowledged partial trailing write so this participant's
    WAL ends at a clean record boundary before it is appended to (issue #535).

    A durable WAL always ends in a newline; a file that does not is carrying a
    torn final write from a crash. Reopening in append mode without sealing merges
    that torn tail with the next record and corrupts the file mid-flight. This
    removes exactly the non-newline-terminated tail and fsyncs; a completed,
    newline-terminated record is never touched. Behaviourally identical to
    ``revl.wal.seal_torn_tail`` (kept inline: this module runs standalone in the
    participant subprocess)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size == 0:
        return
    with open(path, "rb") as handle:
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return
        chunk = 4096
        pos = size
        cut = -1
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            handle.seek(pos)
            buf = handle.read(step)
            idx = buf.rfind(b"\n")
            if idx != -1:
                cut = pos + idx
                break
    new_len = cut + 1
    if new_len == size:
        return
    with open(path, "r+b") as handle:
        handle.truncate(new_len)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
            pass


class _Participant:
    """This process's slice: its accumulator, its WAL, its boundary state."""

    def __init__(self, spec: dict) -> None:
        self.identity = spec.get("identity") or "?"
        self.world_path = spec["world"]
        self.wal_path = spec["wal"]
        self.effects = list(spec.get("effects") or [])
        self.fail_at = spec.get("failAt")
        self.refuse_prepare = spec.get("refusePrepare")
        self.die_at_abort = bool(spec.get("dieAtAbort"))
        # The ordered accumulator: (effect, inverse-or-None). Held HERE, in this
        # process's memory, which is the whole point.
        self.applied: list[tuple[dict, dict | None]] = []
        self._wal_open = False

    # -- boundary state -----------------------------------------------------

    def _world(self) -> dict:
        try:
            return json.loads(Path(self.world_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_world(self, world: dict) -> None:
        Path(self.world_path).write_text(
            json.dumps(world, sort_keys=True, indent=1) + "\n", encoding="utf-8")

    def _referent(self, effect: dict) -> str:
        return f"{self.identity}:{effect['name']}"

    # -- the WAL ------------------------------------------------------------

    def _append(self, record: dict) -> None:
        if not self._wal_open:
            self._raw({"record": "header", "walVersion": WAL_VERSION,
                       "generation": 0, "participant": self.identity})
            self._wal_open = True
        self._raw(record)

    def _raw(self, record: dict) -> None:
        _seal_torn_tail(self.wal_path)  # never merge a torn tail with a record (#535)
        with open(self.wal_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except (OSError, ValueError):  # pragma: no cover
                pass

    # -- the phases ---------------------------------------------------------

    def prepare(self) -> dict:
        """No runtime effects at all. A refusal here is not a rollback: nothing
        was activated, so there is nothing to undo (design §3.2)."""
        if self.refuse_prepare:
            return {"ok": False, "reason": self.refuse_prepare,
                    "identity": self.identity, "pid": os.getpid()}
        return {"ok": True, "identity": self.identity, "pid": os.getpid(),
                "effects": len(self.effects)}

    def _apply_one(self, effect: dict) -> None:
        world = self._world()
        world[self._referent(effect)] = True
        self._write_world(world)
        inverse = (None if not effect.get("reversible", True)
                   else {"op": "remove", "referent": self._referent(effect)})
        self.applied.append((effect, inverse))
        self._append({"record": "effect", "seq": len(self.applied),
                      "participant": self.identity,
                      "referent": self._referent(effect),
                      "inverse": inverse,
                      "boundary": {"referent": "outlives-process"}})

    def _unwind(self, why: str) -> dict:
        """Our OWN local LIFO unwind, run in THIS process against THIS process's
        accumulator, then a re-read no-residue proof off the boundary state."""
        outstanding: list[str] = []
        for effect, inverse in reversed(self.applied):
            if inverse is None:
                # An irreversible crossing: honestly residue, never claimed undone.
                outstanding.append(self._referent(effect))
                self._append({"record": "residue", "participant": self.identity,
                              "referent": self._referent(effect), "pid": os.getpid()})
                continue
            world = self._world()
            world.pop(inverse["referent"], None)
            self._write_world(world)
            self._append({"record": "undo", "participant": self.identity,
                          "referent": inverse["referent"], "pid": os.getpid()})
        self.applied = []
        # the proof: re-read the boundary state and check nothing of ours is left
        world = self._world()
        left = sorted(k for k in world if k.startswith(f"{self.identity}:"))
        clean = not left and not outstanding
        self._append({"record": "aborted", "participant": self.identity,
                      "why": why, "clean": clean, "pid": os.getpid()})
        return {"clean": clean, "outstanding": sorted(set(outstanding) | set(left)),
                "proof": (f"{self.identity} unwound its own slice newest-first in "
                          f"pid {os.getpid()} and re-read the boundary state: "
                          + ("nothing of its own remains."
                             if clean else f"{len(left) + len(outstanding)} "
                                           "referent(s) remain."))}

    def commit(self) -> dict:
        """Apply this slice, in order. A failure mid-way is unwound HERE, LIFO,
        before we answer — the per-process half of the honest decomposition."""
        for index, effect in enumerate(self.effects):
            if self.fail_at is not None and index == self.fail_at:
                residue = self._unwind(f"local commit failed at step {index}")
                return {"ok": False, "identity": self.identity, "pid": os.getpid(),
                        "reason": (f"{self.identity}: applying "
                                   f"{effect['name']!r} failed at step {index}"),
                        "rolledBackLocally": True, "residue": residue}
            self._apply_one(effect)
        self._append({"record": "activation-complete",
                      "participant": self.identity,
                      "components": [e["name"] for e in self.effects]})
        return {"ok": True, "identity": self.identity, "pid": os.getpid(),
                "applied": [self._referent(e) for e in self.effects]}

    def abort(self) -> dict:
        if self.die_at_abort:
            os._exit(1)   # noqa: SLF001 — deliberately vanish mid-protocol
        residue = self._unwind("coordinator sent ABORT")
        return {"ok": True, "identity": self.identity, "pid": os.getpid(),
                "residue": residue}

    def status(self) -> dict:
        world = self._world()
        return {"ok": True, "identity": self.identity, "pid": os.getpid(),
                "applied": [self._referent(e) for e, _ in self.applied],
                "world": sorted(k for k in world
                                if k.startswith(f"{self.identity}:"))}


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    participant = _Participant(spec)
    ops = {"prepare": participant.prepare, "commit": participant.commit,
           "abort": participant.abort, "status": participant.status}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        handler = ops.get(command.get("op"))
        reply = (handler() if handler is not None
                 else {"ok": False, "reason": f"unknown op {command.get('op')!r}"})
        sys.stdout.write(json.dumps(reply, sort_keys=True) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

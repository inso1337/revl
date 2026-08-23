"""Crash recovery: read the accumulator's write-ahead log and prove a way back
(roadmap item 47).

Item 15 (`revl_snapshot`/`revl_restore`, `mcp/persist.py`) makes the *shape* of
an admitted composition durable. Nothing there covers the effects a half-run
activation already committed when the process died: the accumulator that holds
them, and the inverses that would undo them, live only in process memory, so a
`kill -9` mid-activation orphans whatever external state was touched with no
record of it. But the accumulator — effects paired with their inverses, in
order — *is* a write-ahead log. `backends/python/replay.py`'s
:class:`~replay.WriteAheadLog` persists it, effect by effect as each commits.
This module is the other half: on restart, read the WAL and decide.

Roll forward vs roll back
-------------------------
The WAL's terminal ``activation-complete`` marker is the whole decision:

* **present** — activation finished before the crash. The composition's shape
  is durable via item 15; recovery *rolls forward*, resuming the persisted
  generation through :func:`revl.mcp.persist.restore` (this module composes with
  it, it does not reimplement it). No in-flight boundary state is outstanding.
* **absent** — the process died mid-activation. Recovery *rolls back*: it
  reconstructs the boundary inverses from their descriptors and runs them
  newest-first (LIFO, exactly as an L-Raise teardown would), then states a
  checked verdict with a residue proof.

Why boundary state is the real cargo (the honest analysis)
----------------------------------------------------------
After a crash the process memory is gone. An inverse that closes over an
in-process object — a local ``Map`` handle, the fiber's provision registry — has
nothing left to act on; running it is a no-op. So the WAL's cargo is not those.
It is **boundary state**: emissions that already crossed out of the process, and
acquires whose returned resource *outlives* the process (a file on disk
persists; a socket died with the process). Recovery runs reconstructible
inverses for those, reports the in-process ones as **moot**, and — the honest
part — reports a boundary inverse it could only find as a *closure* as
**residue**, never pretending a dead lambda ran. See docs/crash-recovery.md.
"""

from __future__ import annotations

from typing import Any, Optional


class RecoveryError(RuntimeError):
    """A WAL could not be read or recovered."""


# ---------------------------------------------------------------------------
# the world an inverse acts on
# ---------------------------------------------------------------------------


class World:
    """The external state a reconstructed inverse acts on, in a fresh process.

    Recovery does not (cannot) re-enter the dead runtime; it re-issues a
    boundary inverse — a named call with captured arguments read from the WAL —
    against whatever adapter models the outside world. The default
    :class:`DictWorld` models it as a set of durable referents (files, rows) so
    the demo and tests are deterministic; a real host would supply an adapter
    over the actual filesystem/database.
    """

    def key(self, op: dict) -> str:
        args = op.get("args") or []
        return f"{op.get('receiver')}:{args[0] if args else ''}"

    def present(self, referent: str) -> bool:  # pragma: no cover — interface
        raise NotImplementedError

    def seed(self, referent: str, value: Any = True) -> None:  # pragma: no cover
        raise NotImplementedError

    def apply_inverse(self, op: dict) -> None:  # pragma: no cover — interface
        raise NotImplementedError

    def remaining(self) -> list:  # pragma: no cover — interface
        raise NotImplementedError


class DictWorld(World):
    """A referent set. Seeding a referent means "this boundary state really
    persisted"; applying its inverse removes it. What remains is residue."""

    #: verbs whose semantics are "remove/undo this referent"
    _REMOVE = ("remove", "delete", "unlink", "drop", "close", "release",
               "undo", "revoke", "rollback", "compensate", "pop")

    def __init__(self) -> None:
        self.state: dict = {}

    def present(self, referent: str) -> bool:
        return referent in self.state

    def seed(self, referent: str, value: Any = True) -> None:
        self.state[referent] = value

    def apply_inverse(self, op: dict) -> None:
        method = (op.get("method") or "").lower()
        referent = self.key(op)
        if any(verb in method for verb in self._REMOVE):
            self.state.pop(referent, None)
        else:
            # an inverse that is not a removal (a compensating *further*
            # crossing, e.g. a refund) records its effect; it does not clear the
            # original referent, which is faithful — compensation is not
            # inversion (paper §6.1).
            self.state[f"compensation:{referent}"] = op

    def remaining(self) -> list:
        return sorted(k for k in self.state if not k.startswith("compensation:"))


# ---------------------------------------------------------------------------
# classifying a WAL record for recovery
# ---------------------------------------------------------------------------

_OUTLIVES = ("process-crossing", "outlives-process", "unknown")


def _referent_key(record: dict, world: World) -> Optional[str]:
    """A stable key for the boundary referent a record created, or None for an
    in-process effect that left nothing durable behind."""
    boundary = record.get("boundary") or {}
    if boundary.get("referent") not in _OUTLIVES:
        return None
    inverse = record.get("inverse") or {}
    op = inverse.get("op")
    if op is not None:
        return world.key(op)
    # no reconstructible op: key the referent off its own identity so residue
    # can still name it
    detail = boundary.get("detail") or {}
    ident = (detail.get("key"), detail.get("method"),
             tuple(detail.get("args") or []))
    return f"{record.get('component')}:{record.get('label')}:{ident}"


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------


def recover(wal_path: str, *, world: Optional[World] = None,
            session=None, snapshot: Optional[dict] = None) -> dict:
    """Read the WAL at ``wal_path`` and prove a way back.

    Returns a stated verdict (``rolled-forward`` or ``rolled-back``) with a
    residue proof. For a roll-forward, if ``session`` and ``snapshot`` are
    given, the persisted generation is re-admitted through item 15's restore
    (this composes with it; it does not reimplement admission). For a roll-back,
    reconstructible boundary inverses are run LIFO against ``world`` (a
    :class:`DictWorld` by default).
    """
    from replay import WriteAheadLog  # noqa: PLC0415 — backend module, lazy

    try:
        wal = WriteAheadLog.read(wal_path)
    except OSError as error:
        raise RecoveryError(f"cannot read WAL {wal_path}: {error}") from None

    if wal["complete"]:
        return _roll_forward(wal, session=session, snapshot=snapshot)
    return _roll_back(wal, world=world or DictWorld())


def _roll_forward(wal: dict, *, session=None, snapshot: Optional[dict] = None) -> dict:
    """Activation completed before the crash: the shape is durable (item 15).
    Resume by re-admitting the persisted generation."""
    effects = [r for r in wal["records"] if r.get("record") == "effect"]
    complete = next((r for r in wal["records"]
                     if r.get("record") == "activation-complete"), {})
    resumed = None
    if session is not None and snapshot is not None:
        from .mcp.persist import resume, RestoreError  # noqa: PLC0415
        try:
            resumed = resume(session, snapshot)
        except RestoreError as error:
            # a snapshot the *current* checker rejects does not resume silently
            return {
                "verdict": "roll-forward-refused",
                "decision": "activation completed, but the persisted generation "
                            "no longer passes the gate — item 15's restore "
                            "refused it, so it is not re-admitted",
                "diagnostic": error.diagnostic,
                "guarantee": _guarantee(),
            }
    return {
        "verdict": "rolled-forward",
        "decision": ("the WAL carries `activation-complete`: activation finished "
                     "before the crash, so no in-flight boundary state is "
                     "outstanding. The composition's shape is durable via item "
                     "15; recovery resumes the persisted generation rather than "
                     "undoing anything."),
        "committedEffects": len(effects),
        "components": complete.get("components") or [],
        "resumed": resumed is not None,
        "resume": resumed,
        "residue": {
            "clean": True,
            "outstanding": [],
            "proof": "a completed activation left the accumulator balanced; "
                     "there is nothing half-done to roll back.",
        },
        "guarantee": _guarantee(),
    }


def _roll_back(wal: dict, *, world: World) -> dict:
    """Activation did not complete: reconstruct and run boundary inverses LIFO,
    then state a checked verdict with a residue proof."""
    effects = [r for r in wal["records"] if r.get("record") == "effect"]

    # seed the world with every boundary referent the WAL says was created and
    # outlives the process — this is the external state a crash orphaned.
    seeded: dict = {}
    for record in effects:
        referent = _referent_key(record, world)
        if referent is not None:
            world.seed(referent, record.get("label"))
            seeded[id(record)] = referent

    ran, moot, unreconstructible = [], [], []
    # newest-first: an L-Raise teardown runs inverses in reverse commit order
    for record in reversed(effects):
        boundary = record.get("boundary") or {}
        inverse = record.get("inverse") or {}
        referent = seeded.get(id(record))
        entry = {
            "component": record.get("component"),
            "label": record.get("label"),
            "kind": record.get("kind"),
            "class": boundary.get("class"),
            "referent": boundary.get("referent"),
        }
        if referent is None:
            # in-process: the memory it acted on died with the process; running
            # its inverse would be a no-op. Moot, not residue.
            moot.append({**entry,
                         "why": "in-process referent — died with the process; "
                                "its inverse is a no-op after restart"})
            continue
        if inverse.get("reconstructible"):
            world.apply_inverse(inverse["op"])
            ran.append({**entry, "op": inverse["op"]})
        else:
            # a boundary inverse we could only find as a closure: it cannot be
            # re-issued in a fresh process. Say so — do not pretend it ran.
            unreconstructible.append({
                **entry,
                "reason": inverse.get("reason"),
                "still_out": referent,
            })

    remaining = world.remaining()
    clean = not remaining
    outstanding = unreconstructible
    return {
        "verdict": "rolled-back",
        "decision": ("the WAL has no `activation-complete` marker: the process "
                     "died mid-activation. Recovery reconstructed the boundary "
                     "inverses from their descriptors and ran them newest-first "
                     "(LIFO), L-Raise style."),
        "committedEffects": len(effects),
        "torn": wal.get("torn", False),
        "ran": ran,
        "moot": moot,
        "unreconstructible": unreconstructible,
        "residue": {
            "clean": clean,
            "outstanding": outstanding,
            "worldRemaining": remaining,
            "proof": _residue_proof(ran, moot, unreconstructible, remaining),
        },
        "guarantee": _guarantee(),
    }


def _residue_proof(ran: list, moot: list, unreconstructible: list,
                   remaining: list) -> str:
    if not remaining and not unreconstructible:
        return (f"no residue: {len(ran)} reconstructed boundary inverse(s) ran "
                f"and cleared every durable referent; {len(moot)} in-process "
                f"inverse(s) were moot (memory gone). The world is back to where "
                f"it was before activation began.")
    return (f"RESIDUE: {len(unreconstructible)} boundary inverse(s) were "
            f"closure-only and could not be reconstructed, so "
            f"{len(remaining)} durable referent(s) are still out in the world "
            f"({', '.join(remaining) or 'none named'}). Reported honestly — "
            f"the WAL never claimed a dead closure ran. Declare a reconstructible "
            f"inverse (an `extern acquire ... undo ...` / an emission "
            f"`compensate`) for these to make them recoverable.")


def _guarantee() -> str:
    from replay import WAL_GUARANTEE  # noqa: PLC0415
    return WAL_GUARANTEE


# ---------------------------------------------------------------------------
# rendering (for `revl recover`)
# ---------------------------------------------------------------------------


def render(report: dict) -> str:
    lines = [f"verdict: {report['verdict'].upper()}", report["decision"], ""]
    if report["verdict"] == "rolled-forward":
        lines.append(f"  committed effects (all balanced): {report['committedEffects']}")
        lines.append(f"  components: {', '.join(report['components']) or '(none)'}")
        lines.append(f"  resumed persisted generation: {report['resumed']}")
    elif report["verdict"] == "roll-forward-refused":
        lines.append("  the persisted generation no longer passes the gate")
    else:
        for entry in report.get("ran") or []:
            op = entry.get("op") or {}
            call = (f"{op.get('receiver')}.{op.get('method')}"
                    f"({', '.join(map(repr, op.get('args') or []))})")
            lines.append(f"  ran      {entry['label']:<22} {call}")
        for entry in report.get("moot") or []:
            lines.append(f"  moot     {entry['label']:<22} in-process (memory gone)")
        for entry in report.get("unreconstructible") or []:
            lines.append(f"  RESIDUE  {entry['label']:<22} closure-only — still out: "
                         f"{entry['still_out']}")
    residue = report["residue"]
    lines += ["", f"residue proof [{'CLEAN' if residue['clean'] else 'RESIDUE'}]:",
              f"  {residue['proof']}", "", f"guarantee: {report['guarantee']}"]
    return "\n".join(lines)

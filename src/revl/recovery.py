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

    def apply_compensation(self, op: dict) -> None:  # pragma: no cover — interface
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

    def apply_compensation(self, op: dict) -> None:
        """Recover's Phase-2 apply path for a re-issued `compensation`.

        A compensation is a FURTHER crossing that OFFSETS the forward emission,
        never a removal that INVERTS it (247 decision 4; paper §6.1). So it
        always RECORDS its effect and NEVER pops the referent — regardless of the
        verb name. This is the fix the merged contract requires: the generic
        :meth:`apply_inverse` name-matches ``_REMOVE`` verbs, and several
        compensation verbs live in that set (``delete``, ``revoke``,
        ``rollback``, ``compensate``), so routing a compensation through it would
        POP the forward referent and let recover wrongly report a best-effort
        offset as CLEAN. Forcing the record-branch here means a re-issued
        best-effort compensation lands as RESIDUE — the forward referent is still
        out in the world — never CLEAN."""
        self.state[f"compensation:{self.key(op)}"] = op

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


def _record(kind: str, *, crossing: dict, attempted: Optional[dict],
            error: Optional[dict], attempted_flag: bool, outcome: str,
            referent: Optional[str], hint: str) -> dict:
    """One record in the merged residue schema (docs/design/teardown-contract.md,
    "The merged residue schema"). Minimal and closed: a field a consumer needs
    that is not here is a change to the contract, not a tier-local addition."""
    return {
        "kind": kind,
        "crossing": crossing,
        "attempted": attempted or {"call": None, "args": [], "phase": None},
        "error": error,
        "attemptedFlag": attempted_flag,
        "outcome": outcome,
        "referent": referent,
        "hint": hint,
    }


def _crossing_of_effect(record: dict) -> dict:
    """Build the residue `crossing` (the ORIGINAL effect an entry belonged to)
    from a legacy `effect` WAL record."""
    origin = record.get("origin") or {}
    boundary = record.get("boundary") or {}
    detail = (boundary.get("detail") or {})
    return {
        "key": origin.get("key") or detail.get("key") or record.get("component"),
        "method": origin.get("method") or detail.get("method")
                  or record.get("label"),
        "args": list(origin.get("args") or detail.get("args") or []),
        "site": record.get("site"),
    }


def _crossing_of_descriptor(descriptor: dict) -> dict:
    """Build the residue `crossing` from a WAL discharge-descriptor's `origin`
    (the forward crossing the entry reverses/offsets)."""
    origin = descriptor.get("origin") or {}
    call = descriptor.get("call") or {}
    return {
        "key": origin.get("key") or origin.get("receiver") or call.get("receiver"),
        "method": origin.get("method") or call.get("method"),
        "args": list(origin.get("args") or []),
        "site": origin.get("site"),
    }


def _roll_back(wal: dict, *, world: World) -> dict:
    """Activation did not complete: reconstruct and run boundary inverses LIFO,
    then state a checked verdict with a residue proof.

    Two record families are walked. The legacy `effect` records (bare emissions,
    closure inverses, explicit `record_boundary` acquires) are the original
    boundary-inverse path. The WAL discharge-descriptors (item 243/247, the
    witnessed-wal-recover slice) are the transactional-inverse and compensation
    path: Phase 1 re-issues transactional inverses reverse-seq SKIPPING any seq
    with a durable discharge record (a COMMITTED transaction is NOT rolled back),
    Phase 2 re-issues owed compensations through :meth:`World.apply_compensation`
    (which records, never clears)."""
    effects = [r for r in wal["records"] if r.get("record") == "effect"]
    descriptors = [r for r in wal["records"]
                   if r.get("record") == "discharge-descriptor"]
    discharged: set = set()
    for r in wal["records"]:
        if r.get("record") == "discharge":
            discharged.update(r.get("discharged") or [])

    # seed the world with every boundary referent the WAL says was created and
    # outlives the process — this is the external state a crash orphaned.
    seeded: dict = {}
    for record in effects:
        referent = _referent_key(record, world)
        if referent is not None:
            world.seed(referent, record.get("label"))
            seeded[id(record)] = referent

    outstanding: list = []
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
            outstanding.append(_record(
                "unreconstructible",
                crossing=_crossing_of_effect(record),
                attempted=None,
                error={"type": "unreconstructible",
                       "message": inverse.get("reason") or "closure-only inverse"},
                attempted_flag=False, outcome="not-attempted",
                referent=referent,
                hint="declare a reconstructible inverse (extern `acquire … undo …`, "
                     "a witnessed `undo`, or an emission `compensate`) so a fresh "
                     "process can re-issue it"))

    # ---- WAL discharge-descriptors: transactional (Phase 1) + compensation (Phase 2)
    # seed each descriptor's forward referent, keyed the same way the re-issued
    # call keys off, so a transactional re-issue pops exactly it and a
    # compensation leaves exactly it out.
    for d in descriptors:
        world.seed(world.key(d.get("call") or {}), d.get("entry"))

    transactional = [d for d in descriptors if d.get("entry") == "transactional"]
    compensations = [d for d in descriptors if d.get("entry") == "compensation"]
    transactional_rolled_back, discharged_skipped, restore_residue = [], [], []
    # Phase 1: transactional inverses, reverse-seq, skipping discharged seqs.
    for d in sorted(transactional, key=lambda x: x.get("seq", 0), reverse=True):
        call = d.get("call") or {}
        referent = world.key(call)
        seq = d.get("seq")
        if seq in discharged:
            # COMMITTED before the crash: the mutation is the deliverable and its
            # discharge record is durable. Do NOT replay the rollback — skip it,
            # the referent is deliberately retained. THIS is the central safety
            # claim: a committed transaction is never rolled back on recover.
            discharged_skipped.append({"seq": seq, "referent": referent,
                                       "retained": True})
            continue
        # ABORTED / undischarged: reconstruct and run the declared inverse.
        try:
            world.apply_inverse(call)
            transactional_rolled_back.append({"seq": seq, "referent": referent,
                                              "op": call})
        except Exception as error:  # noqa: BLE001 — 243 rule 6: the inverse is fallible
            restore_residue.append({"seq": seq, "referent": referent})
            outstanding.append(_record(
                "restore-residue",
                crossing=_crossing_of_descriptor(d),
                attempted={"call": call.get("method"),
                           "args": list(call.get("args") or []), "phase": 1},
                error={"type": type(error).__name__, "message": str(error)},
                attempted_flag=True, outcome="failed", referent=referent,
                hint="the witnessed inverse failed on re-issue (anticipated, 243 "
                     "rule 6); check the referent and finish the restore by hand"))

    # Phase 2: owed compensations, reverse-seq, best-effort — RECORD not clear.
    compensations_reissued = []
    for d in sorted(compensations, key=lambda x: x.get("seq", 0), reverse=True):
        call = d.get("call") or {}
        referent = world.key(call)
        if d.get("seq") in discharged:
            # discharged on a clean unload: a compensation is never owed on
            # success (the forward emission was the deliverable). Skip, no residue.
            discharged_skipped.append({"seq": d.get("seq"), "referent": referent,
                                       "retained": False})
            continue
        world.apply_compensation(call)   # forced record-branch: never pops
        compensations_reissued.append({"seq": d.get("seq"), "referent": referent})
        outstanding.append(_record(
            "compensation-residue",
            crossing=_crossing_of_descriptor(d),
            attempted={"call": call.get("method"),
                       "args": list(call.get("args") or []), "phase": 2},
            error={"type": "unconfirmed",
                   "message": "re-issued best-effort; the emission's landing "
                              "cannot be confirmed in a fresh process"},
            attempted_flag=True, outcome="unknown", referent=referent,
            hint="the compensation was re-attempted best-effort; its landing "
                 "cannot be confirmed after a crash — the forward referent is "
                 "still out. Verify it was offset, or carry an idempotency key"))

    remaining = world.remaining()
    clean = not outstanding
    return {
        "verdict": "rolled-back",
        "decision": ("the WAL has no `activation-complete` marker: the process "
                     "died mid-activation. Recovery reconstructed the boundary "
                     "inverses from their descriptors and ran them newest-first "
                     "(LIFO), L-Raise style. Transactional inverses with a durable "
                     "discharge record were skipped (committed, not rolled back); "
                     "owed compensations were re-attempted best-effort."),
        "committedEffects": len(effects),
        "torn": wal.get("torn", False),
        "ran": ran,
        "moot": moot,
        "unreconstructible": unreconstructible,
        "transactionalRolledBack": transactional_rolled_back,
        "dischargedSkipped": discharged_skipped,
        "compensationsReissued": compensations_reissued,
        "residue": {
            "clean": clean,
            "outstanding": outstanding,
            "worldRemaining": remaining,
            "proof": _residue_proof(ran, moot, outstanding, remaining,
                                    discharged_skipped, transactional_rolled_back,
                                    compensations_reissued),
        },
        "guarantee": _guarantee(),
    }


def _residue_proof(ran: list, moot: list, outstanding: list, remaining: list,
                   discharged_skipped: list, transactional_rolled_back: list,
                   compensations_reissued: list) -> str:
    committed = [d for d in discharged_skipped if d.get("retained")]
    ran_n = len(ran) + len(transactional_rolled_back)
    if not outstanding:
        note = ""
        if committed:
            note = (f" {len(committed)} committed transactional mutation(s) were "
                    f"retained (discharge record durable — a committed transaction "
                    f"is never rolled back).")
        return (f"no residue: {ran_n} reconstructed boundary inverse(s) ran and "
                f"cleared every referent they owed; {len(moot)} in-process "
                f"inverse(s) were moot (memory gone).{note} The world holds only "
                f"what was deliberately committed.")
    kinds: dict = {}
    for rec in outstanding:
        kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
    breakdown = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    return (f"RESIDUE: {len(outstanding)} outstanding record(s) ({breakdown}); "
            f"{len(remaining)} referent(s) still out in the world "
            f"({', '.join(remaining) or 'none named'}). "
            f"{len(compensations_reissued)} compensation(s) were re-attempted "
            f"best-effort and cannot be confirmed. Reported honestly — the WAL "
            f"never claimed a dead closure ran, and a re-issued compensation is "
            f"an offset that records, never an inversion that clears.")


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
        for entry in report.get("transactionalRolledBack") or []:
            lines.append(f"  rolled-back  seq {entry['seq']:<3} transactional inverse "
                         f"re-issued — {entry['referent']}")
        for entry in report.get("dischargedSkipped") or []:
            tag = "retained (committed)" if entry.get("retained") else "discharged"
            lines.append(f"  skipped   seq {entry['seq']:<3} {tag} — not rolled back: "
                         f"{entry['referent']}")
        for entry in report.get("compensationsReissued") or []:
            lines.append(f"  RESIDUE  compensation seq {entry['seq']:<3} re-attempted "
                         f"best-effort — still out: {entry['referent']}")
    residue = report["residue"]
    lines += ["", f"residue proof [{'CLEAN' if residue['clean'] else 'RESIDUE'}]:",
              f"  {residue['proof']}", "", f"guarantee: {report['guarantee']}"]
    return "\n".join(lines)

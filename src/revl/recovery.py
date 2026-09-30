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
  it, it does not reimplement it). The marker settles the ACTIVATION, and only
  that: a roll-forward still cross-checks the crossings the WAL kept recording
  after it (issue #536) and the class-(b) deferral queue against its ``flushed``
  records (issue #1017) before it is allowed to report ``residue.clean``.
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

from .taint import REDACTED_SECRET


def _has_redacted_arg(call: dict) -> bool:
    """Whether a durable named-call descriptor has an argument the recorder
    redacted because the author declared it `Secret[T]` (item 256 Slice 3).

    A redacted argument is not a value: re-issuing the call with the placeholder
    would address the WRONG referent (`World.key` is `receiver:args[0]`) and let
    recovery report a miss as a clean rollback. So it is refused and surfaced as
    residue instead — recovery loses the ability to re-issue THAT inverse, and
    says so, rather than pretending it ran."""
    return any(arg == REDACTED_SECRET for arg in (call.get("args") or []))


class RecoveryError(RuntimeError):
    """A WAL could not be read or recovered."""


# ---------------------------------------------------------------------------
# the re-dispatch register: what makes a call safe to issue AGAIN (item 440)
# ---------------------------------------------------------------------------

#: item 440 §(a). Item 309 gave recovery a TWO-valued question — was this
#: inverse declared idempotent or not — and everything undeclared fell into one
#: fenced bucket. The third tier is READ: a call the author declared `undo pure`
#: (lowered to `register: "read"` and carried on the WAL descriptor) CHANGES
#: NOTHING, so re-issuing it is observationally free. That is stronger than a
#: key: a keyed call still crosses and leans on the remote's dedup contract, a
#: read crosses nothing, so there is no outcome to be ambiguous ABOUT.
#:
#: The tier is DECLARED, never derived. revl's `pure` extern classification is
#: checked for shape only ("no observable effect" is the declaration's wording,
#: never a proof) and shipped examples classify mutating host bodies `pure`, so
#: reading the tier off the classification alone would resolve an ambiguity
#: optimistically — the one direction this module never takes. `undo pure` is
#: the author's explicit claim, and lower holds it to a `pure`-classified callee.
READ = "read"

#: The registers a fresh process may RE-DISPATCH without spending a fence and
#: without escalating to an operator. `read` (changes nothing) and `keyed` (the
#: remote dedups on a stable key carried in the descriptor). Everything else —
#: including, crucially, NO register at all — stays fenced and fails closed.
#: Item 207 removed a third member, `shape-proven`: it was an INVERSE-body
#: register, and this set is only ever asked about an owed deferred EMISSION, so
#: no declaration could put it here (`docs/design/207-checkable-extern-body.md`).
REDISPATCH_FREE = frozenset({READ, "keyed"})


def _replay_tier(register: Optional[str], declared_idempotent: bool) -> str:
    """The replay tier of one journalled call: ``read`` | ``free`` | ``fenced``.

    ``read``    the descriptor carries `register: "read"` — re-issue freely and
                report it as a read; a second run never escalates it.
    ``free``    item 309's declared-idempotent inverse — re-issue freely,
                exactly as 309 already does.
    ``fenced``  everything else, including an ABSENT register. One fenced
                at-most-once attempt, then `outcome: "unknown"` for a human.

    ONLY `read` is new here: a `keyed` register on an INVERSE descriptor is
    deliberately not promoted, because 309's replay policy for that family is
    keyed off `undo_idempotent` and widening it is a different item's change.
    `REDISPATCH_FREE` (the set the FORWARD re-issue seam reads) is the place
    that register earns free re-dispatch.

    Absent/unknown input can only produce ``fenced``: the fail-closed default is
    the fall-through, not a special case."""
    if register == READ:
        return "read"
    if declared_idempotent:
        return "free"
    return "fenced"


# ---------------------------------------------------------------------------
# the world an inverse acts on
# ---------------------------------------------------------------------------

#: issue #1477. The two values of a verdict's ``world`` field. A verdict whose
#: world is the model describes what an in-memory stand-in did; nothing it
#: reports as ran, re-issued or reclaimed happened outside the process.
WORLD_MODEL = "model"
WORLD_REAL = "real"

#: The phrase every model-run line carries, so no line of a model verdict can be
#: read as an effect that was undone in the outside world.
MODELLED = "modelled, not performed"


class World:
    """The external state a reconstructed inverse acts on, in a fresh process.

    Recovery does not (cannot) re-enter the dead runtime; it re-issues a
    boundary inverse — a named call with captured arguments read from the WAL —
    against whatever adapter models the outside world. The default
    :class:`DictWorld` models it as a set of durable referents (files, rows) so
    the demo and tests are deterministic; a real host would supply an adapter
    over the actual filesystem/database.

    ``kind`` says which of the two an adapter is, and the verdict carries it as
    ``world`` (issue #1477). ``"model"`` is an in-memory stand-in: what it
    "applies" never leaves the process. ``"real"`` is an adapter over the actual
    outside world. The default is ``"model"``, so an adapter that does not
    declare itself real is reported as a model. That is the direction that
    under-claims.
    """

    kind = WORLD_MODEL

    #: issue #1477. A world that sets this takes the discharge-descriptor family
    #: as ONE batch through :meth:`replay_descriptors` (the composition binding,
    #: `revl.recover_binding`, hands it to the runtime's own abort path). Every
    #: other world gets the per-call `apply_inverse`/`apply_compensation` walk.
    replays_descriptors = False

    #: Whether this world can make the calls outside that family: a legacy
    #: boundary inverse, an owed emission's re-issue, a shared reclaim. A world
    #: that cannot reports them as residue, not attempted, and spends no fence.
    re_issues_calls = True

    def replay_descriptors(self, descriptors: list) -> dict:  # pragma: no cover
        """``{seq: outcome}`` for each descriptor, outcome one of ``ran``,
        ``failed``, ``fenced``, ``settled``, ``unresolved``, ``stranded``
        (`runtime.replay_descriptors`). Only for a world that sets
        :attr:`replays_descriptors`."""
        raise NotImplementedError

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

    def reissue(self, op: dict) -> None:  # pragma: no cover — interface
        """item 440 §(b): the RE-ISSUE SEAM. Fire a FORWARD named call again in
        a fresh process — the owed deferred emission item 309 §3b classified as
        free to replay and then handed to a human anyway, because recover had no
        way to re-invoke the emission host body.

        This is the way: recover does not re-enter the dead runtime (it cannot);
        it re-issues the named call with its captured arguments against the same
        world adapter that already carries `apply_inverse`/`apply_compensation`.
        A real host supplies an adapter over the actual outside world. Called
        ONLY for a descriptor whose tier permits it and only under the operator's
        item-33 policy knob — never by default."""
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

    def reissue(self, op: dict) -> None:
        """Re-fire a forward emission (item 440 §(b)). It RECORDS the crossing,
        exactly like a compensation: an emission is a crossing OUT, never a
        removal, so it must not clear a referent. Recorded under its own
        `reissued:` prefix so `remaining` (the residue set) is untouched — a
        re-issued emission is a crossing that HAPPENED, not residue left out in
        the world."""
        self.state[f"reissued:{self.key(op)}"] = op

    def remaining(self) -> list:
        return sorted(k for k in self.state
                      if not k.startswith(("compensation:", "reissued:")))


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
            session=None, snapshot: Optional[dict] = None,
            reissue: Optional[str] = None,
            forward_admissions: bool = False) -> dict:
    """Read the WAL at ``wal_path`` and prove a way back.

    Returns a stated verdict (``rolled-forward`` or ``rolled-back``) with a
    residue proof. For a roll-forward, if ``session`` and ``snapshot`` are
    given, the persisted generation is re-admitted through item 15's restore
    (this composes with it; it does not reimplement admission). For a roll-back,
    reconstructible boundary inverses are run LIFO against ``world`` (a
    :class:`DictWorld` by default).

    ``reissue`` is item 440 §(b)'s operator knob, resolved from the item-33
    boundary policy (``recovery may re-issue owed emissions [(strength: L)]``).
    ``None`` — the default, and what every caller without a policy gets — means
    recover auto-fires NOTHING and the owed-emission report is byte-identical to
    item 245's v1 rule. A strength turns the seam on for owed emissions whose
    descriptor register meets it: ``keyed`` (the bare rule) admits only the
    dedup-safe registers, ``declared`` additionally admits the author's unproven
    trust-me claim. An owed emission with NO register is never auto-fired under
    any strength — the ambiguous case stays human-finish, always.

    The WAL is read through the tier-agnostic core (:func:`revl.wal.read_wal`),
    NOT the py backend: item 322 factored the reader out of
    ``backends/python/replay.py`` so recover reads a WAL produced by any tier's
    runtime — the py in-process driver or a non-py (go/rust/java/wasm)
    subprocess — with no backend on the path.
    """
    from .wal import read_wal  # noqa: PLC0415 — tier-agnostic core, lazy

    try:
        wal = read_wal(wal_path)
    except OSError as error:
        raise RecoveryError(f"cannot read WAL {wal_path}: {error}") from None

    # issue #1477: every call recover makes against a world goes through
    # `_Counted`, so the verdict can say how many calls the world (and, for a
    # model, the model instead of the world) stood in for.
    tally: list = []

    def bound() -> World:
        return _Counted(world if world is not None else DictWorld(), tally)

    records = wal["records"]
    frozen = next((r for r in records
                   if r.get("record") == "fork-frozen"), None)
    if frozen is not None:
        # item 250, Decision 5: a forked parent was FROZEN at step k. Its history
        # above k was rewound into the branch and it takes no further steps, so
        # recover treats it as RETIRED at k — a terminal, non-live state — and
        # does NOT re-admit it as a callable continuation. The branch (its own
        # distinct WAL) recovers independently to its own fork point.
        return _with_world(_fork_retired(wal, frozen), world, tally)
    approved = next((r for r in records
                     if r.get("record") == "commit-approved"), None)
    if wal["complete"]:
        report = _roll_forward(wal, session=session, snapshot=snapshot)
    elif approved is not None:
        # item 245, Decision 3, the approved-to-discharged window: a crash after
        # `commit-approved` and before `activation-complete` is a COMMITTED
        # session. The durable approval, not the discharge record, is the commit
        # proof; recover replays no inverse and rolls the missing discharge
        # forward. This dominates the discharge-set skip for a session-owned WAL.
        report = _roll_forward_window(wal_path, wal, approved,
                                      world=bound(), reissue=reissue)
    else:
        report = _roll_back(wal, world=bound(), wal_path=wal_path)

    # design 460 §5: the forward-recovery scan runs in BOTH branches — an
    # admission can be owed under either verdict, because the session's activation
    # may have completed long before the turn was admitted. It runs AFTER the base
    # is restored (in `_roll_forward`) / the inverse replay (in `_roll_back`), so a
    # finalize never runs over a world an inverse is about to change. The scan is a
    # no-op for a WAL with no `admit-decided`, so a session that never admitted has
    # a byte-identical report.
    admissions = recover_forward_admissions(
        wal, session=session, snapshot=snapshot,
        forward=forward_admissions, wal_path=wal_path)
    if admissions:
        report["admissions"] = admissions
    # item 308 S1 (issue #96): re-fire the zero-crossing inverse of any `shared`
    # grant a whole-process crash left with holders still counted, exactly once
    # (fenced by `shared-reclaim-fence`). A no-op for a WAL with no shared grant,
    # so every non-shared recover report is byte-identical.
    shared = recover_shared_grants(wal, wal_path=wal_path, world=bound())
    if shared is not None:
        report["shared"] = shared
        # a failed (or fenced-unknown) shared reclaim is honest RESIDUE and must
        # move the verdict's residue proof, so `revl recover`'s exit status is 1
        # and the operator sees it — a shared handle whose declared inverse could
        # not be re-fired is exactly the leaked resource recovery exists to name.
        # A clean set of reclaims (or none) leaves the verdict byte-identical.
        if not shared.get("clean", True):
            residue = report.get("residue")
            if residue is not None:
                unclean = [r for r in shared["reclaims"] if not r["ok"]]
                residue["clean"] = False
                residue["proof"] = (
                    residue.get("proof", "")
                    + (" | " if residue.get("proof") else "")
                    + f"{len(unclean)} shared reclaim(s) unresolved: "
                    + ", ".join(f"{r['handle']} ({r['outcome']})" for r in unclean))
    return _with_world(_with_lineage(report, records), world, tally)


def world_kind(world: Optional[World]) -> str:
    """``"real"`` only for an adapter that declares itself real; everything
    else, including no adapter at all (recover then builds a
    :class:`DictWorld`), is ``"model"``."""
    if world is not None and getattr(world, "kind", None) == WORLD_REAL:
        return WORLD_REAL
    return WORLD_MODEL


class _Counted(World):
    """A world that tallies every call recover makes against it, then passes
    the call on unchanged. It is the adapter the caller supplied (or the
    default model) in every other respect, so a caller that inspects its own
    adapter afterwards sees exactly what it saw before. A call is tallied
    BEFORE it is made: one that raises was still attempted against the world."""

    def __init__(self, inner: World, tally: list) -> None:
        self._inner = inner
        self._tally = tally
        self.kind = world_kind(inner)
        self.replays_descriptors = bool(getattr(inner, "replays_descriptors", False))
        self.re_issues_calls = bool(getattr(inner, "re_issues_calls", True))

    def key(self, op: dict) -> str:
        return self._inner.key(op)

    def present(self, referent: str) -> bool:
        return self._inner.present(referent)

    def seed(self, referent: str, value: Any = True) -> None:
        self._inner.seed(referent, value)

    def apply_inverse(self, op: dict) -> None:
        self._tally.append(("inverse", op))
        self._inner.apply_inverse(op)

    def apply_compensation(self, op: dict) -> None:
        self._tally.append(("compensation", op))
        self._inner.apply_compensation(op)

    def reissue(self, op: dict) -> None:
        self._tally.append(("reissue", op))
        self._inner.reissue(op)

    def remaining(self) -> list:
        return self._inner.remaining()

    def replay_descriptors(self, descriptors: list) -> dict:
        outcome = self._inner.replay_descriptors(descriptors)
        self._tally.extend(("descriptor", seq) for seq, state in outcome.items()
                           if state in ("ran", "failed"))
        return outcome

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _spends_fences(world: Optional[World]) -> bool:
    """Whether an attempt against ``world`` may spend a durable at-most-once
    fence (`replay-fence`, `reissue-fence`, `shared-reclaim-fence`).

    Only a real world may (issue #1477). A fence says "an attempt against the
    outside world was about to start". A model run attempts nothing out there,
    so a fence it wrote would be false, and it would block the real recovery
    that comes later: that run would find the fence and refuse an inverse that
    never ran anywhere."""
    return world_kind(world) == WORLD_REAL


def _with_world(report: dict, world: Optional[World],
                tally: Optional[list] = None) -> dict:
    """Stamp the verdict with the world it ran against (issue #1477).

    A model verdict's residue proof is prefixed so the proof itself cannot be
    read as a claim about the outside world: "N inverse(s) ran and cleared every
    referent" is true of the model and says nothing about the files, rows or
    messages the WAL describes. A real-world verdict is left as it was."""
    kind = world_kind(world)
    report["world"] = kind
    # how many calls recover made against that world. For a model it is how
    # many calls the model stood in for; 0 means the verdict never depended on
    # the world, so it is as true of the outside world as of the model.
    report["worldCalls"] = len(tally or [])
    residue = report.get("residue")
    if kind == WORLD_MODEL and residue is not None and "proof" in residue:
        residue["proof"] = (
            f"world: model. Every inverse, compensation, re-issue and reclaim "
            f"counted here was {MODELLED}: it ran against an in-memory model, "
            f"not the outside world. {residue['proof']}")
    return report


def _with_lineage(report: dict, records: list) -> dict:
    """Annotate a verdict with the session's branch lineage when its WAL carries
    one (item 250, Slice 2).

    A branch recovers exactly as any other session does — over its OWN witnessed
    effects, back to its own fork point — so the VERDICT is untouched and every
    non-branch WAL's report is byte-identical. What changes is that the operator
    reading it is told the rollback lands at a fork point rather than at an
    activation start, and which parent it diverged from; a rollback that silently
    looks like a fresh session's is the one way this report could mislead.
    """
    branch = next((r for r in records if r.get("record") == "fork-branch"), None)
    if branch is None:
        return report
    report["lineage"] = {
        "role": "branch",
        "branch": branch.get("branch"),
        "parent": branch.get("parent"),
        "divergedAt": branch.get("at"),
        "parentWal": branch.get("parentWal"),
        "notPreserved": branch.get("notPreserved") or [],
        "note": (
            "this session is a BRANCH: the rollback above unwinds only what the "
            "branch itself recorded, so it lands at the fork point, not at an "
            "empty workspace. The state below the fork point is the parent's "
            "rewound step-k state and is not this WAL's to restore. The parent "
            "(its own WAL) is retired at k and recovers separately."),
    }
    return report


def _fork_retired(wal: dict, frozen: dict) -> dict:
    """A forked parent's terminal recovery verdict (item 250, Decision 5).

    The parent WAL carries ``fork-frozen``: the session was retired at step k when
    a branch forked from it. Recovery neither rolls it forward (it never committed)
    nor rolls it back live (its history above k was rewound into the branch, whose
    own WAL recovers independently). It is a terminal, non-live state. The crossed
    set and the enumerated would-cross inverses carried in ``fork-begin`` are
    surfaced as the honest residue: they were durable BEFORE the rewind, so they
    survive the crash and are not silently lost."""
    begin = next((r for r in wal["records"]
                  if r.get("record") == "fork-begin"), {})
    complete = next((r for r in wal["records"]
                     if r.get("record") == "fork-complete"), None)
    crossed = begin.get("crossed") or []
    would_cross = begin.get("wouldCross") or []
    at = frozen.get("at")
    outstanding = ([e.get("index") for e in crossed]
                   + [e.get("index") for e in would_cross])
    return {
        "verdict": "fork-retired",
        "decision": (
            f"the WAL carries `fork-frozen`: this session was forked at step {at} "
            "and RETIRED there. Its history above k was rewound into the branch, "
            "so recovery does not re-admit it as a live continuation — the branch "
            "(its own WAL) recovers independently to its own fork point."),
        "at": at,
        "branch": (complete or {}).get("branch"),
        "forkComplete": complete is not None,
        "residue": {
            "clean": not outstanding,
            "outstanding": outstanding,
            "proof": (
                f"{len(crossed)} emission(s) crossed the boundary before the fork "
                f"and cannot be undone; {len(would_cross)} inverse(s) whose scope "
                "crosses the boundary were enumerated but not fired. Both sets were "
                "made durable in `fork-begin` before the rewind, so they survive "
                "the crash. The parent is retired at k; nothing else is claimed."),
        },
        "guarantee": _guarantee(),
    }


def _steady_state_residue(steady: list) -> dict:
    """Classify the boundary crossings a run committed AFTER the
    ``activation-complete`` marker with no ``run-complete`` shutdown marker
    (issue #536).

    The WAL is now held open for the whole run, so a steady-state emission or
    acquire is durably recorded as it commits. When the run reached an orderly
    teardown it stamped ``run-complete`` and this function is not consulted; its
    ABSENCE means the process was ``kill -9``'d in steady state, so every durable
    crossing recorded after activation is in-flight/orphaned and is surfaced as
    honest residue rather than hidden behind the activation marker. An in-process
    crossing is moot — its referent died with the process, nothing was orphaned.
    """
    world = DictWorld()
    crossed, moot, outstanding = [], [], []
    for record in steady:
        boundary = record.get("boundary") or {}
        entry = {
            "component": record.get("component"),
            "label": record.get("label"),
            "kind": record.get("kind"),
            "class": boundary.get("class"),
            "referent": boundary.get("referent"),
        }
        referent = _referent_key(record, world)
        if referent is None:
            moot.append({**entry, "why": "in-process referent — died with the "
                                         "process; nothing durable was orphaned"})
            continue
        crossed.append({**entry, "still_out": referent})
        outstanding.append(_record(
            "steady-state-residue",
            crossing=_crossing_of_effect(record),
            attempted=None,
            error={"type": "steady-state-crossing",
                   "message": "a boundary crossing committed AFTER "
                              "activation-complete with no `run-complete` marker "
                              "— the run was `kill -9`'d in steady state, so this "
                              "crossing is still out in the world"},
            attempted_flag=False, outcome="not-attempted", referent=referent,
            hint="the WAL is now held open for the run's life (issue #536), so a "
                 "steady-state crash is visible here instead of reading as CLEAN; "
                 "reconcile the crossing or resume the generation"))
    return {"crossed": crossed, "moot": moot, "outstanding": outstanding}


def _steady_state_proof(residue: dict) -> str:
    out = residue["outstanding"]
    names = ", ".join(sorted({r["referent"] for r in out})) or "none named"
    return (f"RESIDUE: {len(out)} boundary crossing(s) committed AFTER "
            f"activation-complete with no `run-complete` shutdown marker — the "
            f"run was `kill -9`'d in steady state, not shut down cleanly. "
            f"{len(residue['moot'])} in-process crossing(s) were moot (memory "
            f"gone). Still out in the world: {names}. The WAL is now held open "
            f"for the run's life (issue #536), so these are visible instead of "
            f"hidden behind the activation-complete marker.")


def _deferral_crosscheck(records: list) -> dict:
    """Cross-check the class-(b) deferral queue against the ``flushed`` set on a
    WAL that carries ``activation-complete`` (issue #1017).

    The terminal marker settles the ACTIVATION. It never settled the deferral
    queue: a ``deferred-emission`` descriptor is logged at ENQUEUE (the intent),
    and its outcome is a LATER ``flushed`` record written only after the host
    body fired. So a cut that reaches ``activation-complete`` can still carry an
    approved emission that never crossed — the exact shape the item-245
    deferral-queue finding measured (#1004:
    ``[effect, effect, deferred-emission, effect, effect, commit-approved,
    activation-complete]``). #1004 fixed the CAUSE of that lost queue; the reader
    still certified the cut balanced, because :func:`_roll_forward` returned
    before anything looked at the queue. It looks now.

    Fails CLOSED: an approved descriptor with no ``flushed`` record is RESIDUE,
    never silence. The classification is the one
    :func:`_roll_forward_window` already makes over the same two record families,
    so the two roll-forward surfaces cannot drift on the same WAL:

    * ``flushed`` for its seq — confirmed crossed.
    * ``flush-residue`` for its seq — the host body RAISED at flush
      (continue-and-record); the crossing did not land, so it is owed with the
      recorded error.
    * neither — OWED, ``not-attempted``: the body may not have fired before the
      crash, which is the honest state.

    A descriptor with NO ``commit-approved`` after it was never approved, so it
    was DROPPED, never fired (item 245, Decision 3 — dropping is free, nothing
    crossed the boundary). That is the roll-back path's rule and it stays clean
    here; the fail-closed direction is about APPROVED queues. Approval is read
    positionally — ``commit-approved`` names the manifest hash, not the seqs —
    and a WAL that reuses one file across runs keeps a single monotonic seq
    space, so matching by seq is exact across run boundaries.

    Recovery FIRES NOTHING here: this path has no world adapter and no operator
    re-issue policy, so an owed emission is reported and left for a human. The
    item-440 re-issue seam stays where it is, on the window path.
    """
    last_approved = next((i for i in range(len(records) - 1, -1, -1)
                          if records[i].get("record") == "commit-approved"), -1)
    flushed = {r.get("seq") for r in records if r.get("record") == "flushed"}
    flush_failed = {r.get("seq"): r for r in records
                    if r.get("record") == "flush-residue"}
    fired, owed, dropped, outstanding = [], [], [], []
    for index, record in enumerate(records):
        if record.get("record") != "deferred-emission":
            continue
        seq = record.get("seq")
        call = record.get("call") or {}
        entry = {"seq": seq,
                 "referent": f"{call.get('receiver')}.{call.get('method')}"}
        if index > last_approved:
            dropped.append(entry)
        elif seq in flushed:
            fired.append(entry)
        elif seq in flush_failed:
            info = flush_failed[seq].get("error") or {}
            owed.append({**entry, "outcome": "failed"})
            outstanding.append(_record(
                "flush-residue", crossing=_crossing_of_descriptor(record),
                attempted={"call": call.get("method"),
                           "args": list(call.get("args") or []), "phase": None},
                error=info or {"type": "flush-failed",
                               "message": "the host body raised at flush"},
                attempted_flag=True, outcome="failed",
                referent=entry["referent"],
                hint="the deferred emission's host body raised at flush "
                     "(continue-and-record) and the run went on to stamp "
                     "`activation-complete`; the marker does not settle this "
                     "crossing — finish it by hand (issue #1017)"))
        else:
            owed.append({**entry, "outcome": "not-attempted"})
            outstanding.append(_record(
                "flush-residue", crossing=_crossing_of_descriptor(record),
                attempted={"call": call.get("method"),
                           "args": list(call.get("args") or []), "phase": None},
                error={"type": "not-attempted",
                       "message": "approved but no `flushed` record — the host "
                                  "body may not have fired before the crash"},
                attempted_flag=False, outcome="not-attempted",
                referent=entry["referent"],
                hint="the emission was approved but its flush is unconfirmed, "
                     "and `activation-complete` does not settle it — the marker "
                     "proves activation finished, not that the deferral queue "
                     "was flushed. Finish the flush by hand (issue #1017)"))
    return {"flushed": fired, "owed": owed, "dropped": dropped,
            "outstanding": outstanding}


def _deferral_proof(crosscheck: dict) -> str:
    owed = crosscheck["owed"]
    names = ", ".join(sorted({e["referent"] for e in owed})) or "none named"
    return (f"RESIDUE: {len(owed)} approved deferred emission(s) have no "
            f"`flushed` record under a WAL that carries `activation-complete` — "
            f"the marker settles the activation, never the deferral queue, so an "
            f"unmatched emission is residue, not silence (issue #1017). "
            f"{len(crosscheck['flushed'])} confirmed flushed, "
            f"{len(crosscheck['dropped'])} never approved (dropped, nothing "
            f"crossed). Unconfirmed: {names}.")


def _roll_forward(wal: dict, *, session=None, snapshot: Optional[dict] = None) -> dict:
    """Activation completed before the crash: the shape is durable (item 15).
    Resume by re-admitting the persisted generation.

    The WAL is held open for the whole run (issue #536), so it can carry
    crossings committed AFTER ``activation-complete``. If the run also stamped
    ``run-complete`` (an orderly teardown) those crossings were accounted and the
    verdict is CLEAN. Without it — a steady-state ``kill -9`` — the recorded
    post-activation crossings are surfaced as honest residue instead of the WAL
    reading as falsely CLEAN."""
    records = wal["records"]
    effects = [r for r in records if r.get("record") == "effect"]
    # issue #642: one WAL file can carry MORE THAN ONE run when a later
    # invocation reuses the same `--wal` path (the recorder resumes the seq
    # space and appends onto the prior run's records — issue #536). The run to
    # recover is the LATEST one, so its activation marker is the LAST
    # `activation-complete`, and the crossings it left in steady state are the
    # effects AFTER that marker. Scoping to the last marker (not the first) is
    # what keeps an earlier run's records out of this run's steady set; on a
    # single-run WAL the first and last marker are the same record, so every
    # pre-#642 report is byte-identical.
    marker_idx = next((i for i in range(len(records) - 1, -1, -1)
                       if records[i].get("record") == "activation-complete"),
                      len(records))
    complete = records[marker_idx] if marker_idx < len(records) else {}
    tail = records[marker_idx + 1:]
    # A `run-complete` settles ONLY the crossings committed BEFORE it, within
    # its own run's interval. Scoping to the LAST activation marker is not enough
    # to keep an earlier run's clean shutdown from masking a later run's crash:
    # a reused WAL whose LATEST run crashed BEFORE stamping its own
    # `activation-complete` (a PREactivation crash — the crash caught it while
    # activation was still in flight, so the last `activation-complete` belongs
    # to the PRIOR run) leaves that prior run's `run-complete` sitting in this
    # tail, AHEAD of the later run's outstanding effect. Testing
    # `any(run-complete)` over the whole tail would let that historical marker
    # zero the later effect and report a false CLEAN (#642, the preactivation
    # residual). So bound the settling to what the marker actually precedes:
    # find the LAST `run-complete` in the tail and treat only the effects AFTER
    # it as this run's unaccounted, steady-state residue. An effect BEFORE the
    # last `run-complete` was accounted by that orderly shutdown; an effect after
    # it belongs to a later run that reused the file and is still outstanding.
    # With no `run-complete` in the tail every post-activation effect is residue,
    # and on a single-run WAL the marker is the last record (or absent), so both
    # branches stay byte-identical to the pre-#642 reports.
    last_run_complete = next(
        (i for i in range(len(tail) - 1, -1, -1)
         if tail[i].get("record") == "run-complete"), -1)
    steady = [r for r in tail[last_run_complete + 1:]
              if r.get("record") == "effect"]
    steady_residue = _steady_state_residue(steady)
    # issue #1017: the activation marker is not a flush receipt. Cross-check the
    # deferral queue the WAL already enumerates against its `flushed` records
    # before this verdict is allowed to say `clean`. Empty in both directions on
    # a WAL with no `deferred-emission`, so every deferral-free roll-forward
    # report keeps its existing body and proof text.
    deferrals = _deferral_crosscheck(records)
    resumed = None
    if session is not None and snapshot is not None:
        from .mcp.approval import ApprovalRequired  # noqa: PLC0415
        from .mcp.persist import resume, RestoreError  # noqa: PLC0415
        try:
            resumed = resume(session, snapshot)
        except ApprovalRequired as pending:
            # the re-established approval policy re-armed the activation gate and
            # the recovered generation's activation body reaches a class-(c)
            # crossing: recovery held it rather than firing it, exactly as on
            # first boot. Recovery never auto-answers a human's ticket, so this
            # is a fail-closed verdict a human must clear, not a silent resume.
            ticket = getattr(pending, "ticket", {}) or {}
            return {
                "verdict": "roll-forward-needs-approval",
                "decision": ("activation completed before the crash, but the "
                             "recovered generation's activation body reaches a "
                             "class-(c) crossing under the re-established approval "
                             "policy. Recovery re-armed the gate and did NOT fire "
                             "it — a human must approve it, exactly as on first "
                             "boot (item 246)."),
                "ticket": {k: ticket.get(k)
                           for k in ("hash", "component", "kind", "capabilities")},
                "residue": {
                    "clean": False,
                    "outstanding": [ticket.get("component")],
                    "proof": "a class-(c) activation crossing is held at the gate "
                             "pending approval; recovery never auto-fires it.",
                },
                "guarantee": _guarantee(),
            }
        except RestoreError as error:
            # a snapshot the *current* checker rejects — OR one whose approval
            # posture the recovering session does not carry (a policy-recorded
            # snapshot into a policy-less session) — does not resume silently.
            # The refusal message carries the reason and, for the posture case,
            # the flag to pass so the gate re-arms on resume.
            return {
                "verdict": "roll-forward-refused",
                "decision": "activation completed, but the persisted generation "
                            "no longer passes the gate — item 15's restore "
                            "refused it, so it is not re-admitted",
                "message": str(error),
                "diagnostic": error.diagnostic,
                "guarantee": _guarantee(),
            }
    steady_outstanding = steady_residue["outstanding"]
    outstanding = steady_outstanding + deferrals["outstanding"]
    if steady_outstanding:
        decision = ("the WAL carries `activation-complete` but no `run-complete`: "
                    "activation finished, then the process was `kill -9`'d in "
                    "steady state. The composition's shape is durable via item 15, "
                    f"but the {len(steady_outstanding)} crossing(s) committed after "
                    "activation are in-flight — recovery reports them honestly "
                    "instead of reading the WAL as CLEAN (issue #536).")
        proof = _steady_state_proof(steady_residue)
    elif deferrals["outstanding"]:
        decision = ("the WAL carries `activation-complete`, but an approved "
                    "deferred emission has no `flushed` record: the marker proves "
                    "activation finished, never that the class-(b) deferral queue "
                    "was flushed. Recovery reports the unmatched emission(s) as "
                    "residue rather than certifying the cut balanced, and fires "
                    "nothing (issue #1017).")
        proof = _deferral_proof(deferrals)
    else:
        decision = ("the WAL carries `activation-complete`: activation finished "
                    "before the crash, so no in-flight boundary state is "
                    "outstanding. The composition's shape is durable via item "
                    "15; recovery resumes the persisted generation rather than "
                    "undoing anything.")
        proof = ("a completed activation left the accumulator balanced; "
                 "there is nothing half-done to roll back.")
    if steady_outstanding and deferrals["outstanding"]:
        # both causes at once: neither may mask the other, so the proof carries
        # both sentences the same way a failed shared reclaim appends its own.
        proof = f"{proof} | {_deferral_proof(deferrals)}"
    return {
        "verdict": "rolled-forward",
        "decision": decision,
        "committedEffects": len(effects),
        "components": complete.get("components") or [],
        "resumed": resumed is not None,
        "resume": resumed,
        # issue #536: crossings committed after `activation-complete`. Empty on a
        # clean shutdown (`run-complete` present) or a run that never left
        # activation, which keeps every pre-#536 roll-forward report unchanged.
        "steadyState": steady_residue,
        # issue #1017: the deferral queue's flush state under the activation
        # marker. `owed` is non-empty exactly when the short-circuit would once
        # have certified an unbalanced WAL clean.
        "deferrals": {k: deferrals[k] for k in ("flushed", "owed", "dropped")},
        "residue": {
            "clean": not outstanding,
            "outstanding": outstanding,
            "proof": proof,
        },
        "guarantee": _guarantee(),
    }


def _append_admit_record(wal_path: str, record: dict) -> None:
    """Append one `admit-finalized`/`admit-abandoned` forward-recovery record to
    the WAL, fsync'd (design 460 §5). Appending fires nothing, so it is safe and a
    second recover pass reads the same classification with no special-casing —
    the same discipline `_append_discharge` uses. The record carries no seq: it
    names a fact about an existing `admit-decided`, it is not a new ordered
    event.

    Like every other WAL appender (`_append_discharge`, `_append_replay_fence`,
    `_append_reissue_fence`), it SEALS a never-acknowledged torn trailing write
    FIRST (issue #641, the #535/#563 seal discipline). Forward admission
    recovery runs against a WAL a `kill -9` mid-write may have left with a torn
    final line; appending a terminal `admit-finalized`/`admit-abandoned`
    straight onto that tail merges the two into one unparseable line. The reader
    tolerates it as a torn LAST line and silently drops it — the finalization is
    then not durably readable — and a second terminal append leaves the merged
    line mid-file, which the item 413 gate refuses forever. Sealing truncates
    the torn tail to its last clean newline boundary so the terminal record
    lands on its own line, survives reopening and stays readable after later
    appends; a completed, newline-terminated record is never touched."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415
    from .wal import seal_torn_tail  # noqa: PLC0415 — tier-agnostic core
    seal_torn_tail(wal_path)
    with open(wal_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
            pass


def _served_fenced_crossings(records: list, decision_id: str) -> tuple:
    """The §4 journal-served view of one decision's fenced crossings: pair the
    `admit-crossing` records by `(decisionId, ordinal)` and split them into the
    completed and the in-flight.

    Returns `(served, in_flight)` where `served` maps a fenced crossing's
    `ordinal` to its recorded `outcome` (a crossing with a `phase: "complete"`
    record — it RAN to completion, so a re-apply serves the outcome and dispatches
    zero times) and `in_flight` is the sorted ordinals with a `begin` and no
    `complete` — a fenced crossing cut mid-flight (a plain `kill -9`, not only the
    E-Stop's `estop-ambiguous`). The §4 table's three states fall out of this: a
    completed crossing is in `served`, an in-flight one is in `in_flight`, and a
    crossing with no record at all is in neither (it simply runs for the first
    time on re-apply). A torn trailing `complete` line the reader dropped presents
    as in-flight, which is the fail-closed reading."""
    begins: dict = {}
    completes: dict = {}
    for r in records:
        if r.get("record") != "admit-crossing" \
                or r.get("decisionId") != decision_id:
            continue
        ordinal = r.get("ordinal")
        if r.get("phase") == "complete":
            completes[ordinal] = r.get("outcome")
        elif r.get("phase") == "begin":
            begins[ordinal] = r
    served = {o: completes[o] for o in completes}
    in_flight = sorted(o for o in begins if o not in completes)
    return served, in_flight


def recover_forward_admissions(wal: dict, *, session=None,
                               snapshot: Optional[dict] = None,
                               forward: bool = False,
                               wal_path: Optional[str] = None) -> list:
    """`recoverForwardCommit` (design 460 §5): scan the WAL's `admit-*` records
    and classify every un-finalized two-phase admission, finalizing forward the
    ones the evidence proves advanced.

    Runs in BOTH recovery branches — an admission can be owed under either the
    roll-forward or the roll-back verdict, because the session's activation may
    have completed long before the turn was admitted. `revl recover` calls it
    after `persist.resume` has restored the base (so the content CAS has a
    restored generation to compare against). Returns one report per un-finalized
    decision; with `forward` false it changes nothing (the report-only mode,
    matching `revl estop --report`), with `forward` true it appends
    `admit-finalized` for an advanced decision whose surface still matches and
    `admit-abandoned {stale}` for one whose surface drifted.

    The classification, per §5:

      * `owed` — nothing after `decided` (no `applied`, no journal). The runtime
        did not advance; the recorded turn is re-admittable through the gate, which
        re-asks any ticket (the spends are already consumed, fail-closed). Reported;
        never silently re-run.
      * `advanced` — `admit-applied` is present AND a restored `session` let the
        content CAS run and pass. The runtime advanced past the decision and the
        surface still matches; finalize forward.
      * `unverified` — `admit-applied` is present but there is NO restored
        `session` to recompute the current surface from, so the content CAS could
        not run. Forward recovery refuses to finalize (it will not claim a CAS it
        never performed, issue #476 review); `admit-applied` is historical applied
        state, not checked-current state. Reported; finalizes nothing. A
        `--restore` snapshot moves it to `advanced` or `stale`.
      * `stale` — advanced, but the content CAS failed: the base's manifest or the
        merged class-map digest moved. Write `admit-abandoned {stale}`, report the
        digest that moved, finalize nothing. A decision never finalizes onto a
        surface it did not see.
      * `ambiguous` — an `estop-ambiguous` record sits after `decided` (a fenced
        crossing in flight at the cut, §4/§6). Report the one record; refuse to
        finalize; the operator reconciles it exactly as an `estop-ambiguous`.

    The journal-served re-apply (design 460 §4). Before an `advanced` decision is
    finalized forward, its fenced crossings are read off the journal by
    `(decisionId, ordinal)` (`_served_fenced_crossings`):

      * a fenced crossing recorded COMPLETE is served from the journal — its
        outcome is durable, so a re-apply returns it and dispatches zero times
        ("no double-run of a fenced extern", §8). When `session` exposes the §4
        seam (`begin_journal_served`/`serve_fenced_crossing`), the served outcomes
        are driven through it so the finalize is gated on the seam actually
        serving every one with no dispatch (`dispatched == 0`, the non-vacuity
        witness the §7 exit test reads);
      * a fenced crossing left IN FLIGHT at the cut (a `begin` with no `complete`
        — a plain crash mid-crossing, the same state the E-Stop's
        `estop-ambiguous` names) reclassifies the decision `ambiguous`: forward
        recovery refuses to finalize and leaves the one crossing for the operator,
        never re-dispatching it.

    This function still does not itself re-materialize the turn's fibers in a live
    runtime (that plug drives `serve_fenced_crossing` at each fenced seam); it
    delivers the durable classification, the content CAS, the fenced-crossing
    serving verdict and the forward finalize over the stage records."""
    records = wal.get("records") or []
    decided = [r for r in records if r.get("record") == "admit-decided"]
    if not decided:
        return []

    def _for(kind: str, did: str) -> bool:
        return any(r.get("record") == kind and r.get("decisionId") == did
                   for r in records)

    reports: list = []
    for d in sorted(decided, key=lambda r: r.get("seq", 0)):
        did = d.get("decisionId")
        if _for("admit-finalized", did):
            continue  # a fully committed decision: the scan is a no-op over it.
        if _for("admit-abandoned", did):
            continue  # already settled (plug-failed / estop / a prior stale).

        # an estop-ambiguous record after this decision's seq is the §4 in-flight
        # fenced row: report and refuse to finalize.
        decided_seq = d.get("seq", 0)
        # design 460 §6.2: an `estop-ambiguous` record tagged with THIS decisionId
        # is the in-flight fenced row for exactly this decision (the halt now tags
        # the residue it stranded, §6.3); an untagged one (a legacy/pre-tag record)
        # falls back to the seq window after this decision. Tagged records never
        # cross-attribute to a neighbouring decision.
        ambiguous = [r for r in records
                     if r.get("record") == "estop-ambiguous"
                     and (r.get("decisionId") == did
                          or (r.get("decisionId") is None
                              and r.get("seq", -1) > decided_seq))]
        applied = _for("admit-applied", did)

        if ambiguous:
            reports.append({
                "decisionId": did, "classification": "ambiguous",
                "decision": ("a fenced crossing was in flight at the cut "
                             "(estop-ambiguous under this decision); forward "
                             "recovery refuses to finalize and leaves the one "
                             "record for the operator, exactly as an E-Stop halt."),
                "estopAmbiguous": [r.get("seq") for r in ambiguous],
                "finalized": False})
            continue

        if not applied:
            reports.append({
                "decisionId": did, "classification": "owed",
                "decision": ("the runtime did not advance past the decision (no "
                             "`admit-applied`). The recorded turn is re-admittable "
                             "through the gate, which re-asks any ticket — the "
                             "spends this decision committed are already consumed "
                             "(fail-closed). Reported; not silently re-run."),
                "turn": d.get("turn"), "spends": d.get("spends") or [],
                "finalized": False})
            continue

        # advanced: the content CAS on the recorded `expected` surface. The CAS
        # REQUIRES a restored Session to recompute the live surface from
        # (`_forward_surface_for_turn`, design 460 §3): the class map is never
        # trusted from the record, it is rebuilt from the restored base plus the
        # recorded turn and compared. Without a Session — a `revl recover
        # --forward` with no `--restore` — there is NO current-surface evidence,
        # so an advanced decision cannot be finalized: finalizing here would append
        # `admit-finalized` and report a content CAS that never ran, carrying a
        # decision onto a surface it may never have seen (issue #476 review). An
        # `admit-applied` is HISTORICAL applied state, not CHECKED-current state;
        # forward recovery refuses, reports the decision unresolved, appends
        # nothing, and never claims the CAS passed.
        expected = d.get("expected") or {}
        if session is None:
            reports.append({
                "decisionId": did, "classification": "unverified",
                "decision": ("the runtime advanced past the decision, but forward "
                             "recovery has no restored Session to recompute and "
                             "compare the CURRENT surface against (no `--restore`), "
                             "so the content CAS design 460 §3 requires never ran. "
                             "The decision is NOT finalized — its `admit-applied` is "
                             "historical applied state, not checked-current state. "
                             "Re-run with `--restore SNAPSHOT` to authorize the "
                             "forward finalize under the surface CAS."),
                "casChecked": False, "finalized": False})
            continue
        classification = "advanced"
        cas_detail = None
        try:
            live = session._forward_surface_for_turn(d.get("turn") or {})
        except Exception as error:  # noqa: BLE001 — a refusing checker, §2.1
            classification = "stale"
            cas_detail = str(error)
        else:
            drifted = [k for k in ("baseManifestHash", "classMapDigest")
                       if expected.get(k) is not None
                       and live.get(k) != expected.get(k)]
            if drifted:
                classification = "stale"
                cas_detail = ", ".join(
                    f"{k}: {expected.get(k)} -> {live.get(k)}"
                    for k in drifted)

        if classification == "stale":
            if forward and wal_path is not None:
                _append_admit_record(wal_path, {
                    "record": "admit-abandoned", "decisionId": did,
                    "reason": "stale"})
            reports.append({
                "decisionId": did, "classification": "stale",
                "decision": ("the runtime advanced, but the surface the decision "
                             "was checked against moved (content CAS failed); the "
                             "decision is abandoned and finalizes nothing. The "
                             "landed effects are left to the normal roll-back/"
                             "roll-forward verdict."),
                "drift": cas_detail,
                "casChecked": True,
                "finalized": False,
                "abandoned": bool(forward and wal_path is not None)})
            continue

        # advanced and the surface still matches: the §4 journal-served re-apply.
        # Read this decision's fenced crossings off the journal. One left in flight
        # at the cut (a `begin` with no `complete`) is ambiguous — refuse to
        # finalize and leave it for the operator, exactly as an `estop-ambiguous`.
        served, in_flight = _served_fenced_crossings(records, did)
        if in_flight:
            reports.append({
                "decisionId": did, "classification": "ambiguous",
                "decision": ("a fenced crossing was in flight at the cut (an "
                             "`admit-crossing begin` with no `complete` under this "
                             "decision); forward recovery refuses to finalize and "
                             "leaves the one crossing for the operator, never "
                             "re-dispatching a fenced extern (§4/§8)."),
                "inFlight": in_flight,
                "finalized": False})
            continue

        # every fenced crossing completed: serve them from the journal. When the
        # session exposes the §4 seam, drive the served outcomes through it so the
        # finalize is gated on the seam serving each one with ZERO dispatch — the
        # re-apply re-materializes the turn's provisions without re-running a
        # fenced extern. `dispatched == 0` is the guarantee (§8), asserted here.
        dispatched = 0
        if served and session is not None \
                and hasattr(session, "begin_journal_served"):
            session.begin_journal_served(did, served)
            try:
                for ordinal in sorted(served):
                    call = next((r.get("call") for r in records
                                 if r.get("record") == "admit-crossing"
                                 and r.get("decisionId") == did
                                 and r.get("ordinal") == ordinal
                                 and r.get("phase") == "begin"), {}) or {}
                    session.serve_fenced_crossing(call.get("receiver", ""),
                                                  call.get("method", ""))
                dispatched = session._fenced_dispatch_count
            finally:
                session.end_journal_served()
            if dispatched:
                # a completed fenced crossing that did NOT serve is a broken seam,
                # not a finalizable decision — fail closed rather than double-run.
                reports.append({
                    "decisionId": did, "classification": "ambiguous",
                    "decision": ("the journal-served seam did not serve a "
                                 "completed fenced crossing (dispatched "
                                 f"{dispatched}); forward recovery refuses to "
                                 "finalize rather than risk a double-run (§4)."),
                    "dispatched": dispatched, "finalized": False})
                continue

        if forward and wal_path is not None:
            _append_admit_record(wal_path, {
                "record": "admit-finalized", "decisionId": did,
                "observed": {"forwardRecovered": True}})
        reports.append({
            "decisionId": did, "classification": "advanced",
            "decision": ("the runtime advanced past the decision and the surface "
                         "still matches (content CAS passed); the decision is "
                         "finalized forward rather than left ambiguous — its "
                         "effects landed and its approvals were spent, so a "
                         "dropped admission would be the lie the world contradicts. "
                         "Its fenced crossings are served from the journal, not "
                         "re-dispatched (§4)."),
            "served": sorted(served), "dispatched": dispatched,
            "casChecked": True,
            "finalized": bool(forward and wal_path is not None)})
    return reports


def _append_discharge(wal_path: str, seqs: list) -> None:
    """Roll the missing discharge record forward by APPENDING it (item 245,
    Decision 3). Appending fires nothing, so it is safe, and it makes a second
    recover pass read the same verdict with no special-casing. Same fsync'd
    append discipline the WAL writer uses."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415
    from .wal import seal_torn_tail  # noqa: PLC0415 — tier-agnostic core
    seal_torn_tail(wal_path)
    with open(wal_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"record": "discharge", "discharged": list(seqs)},
                                sort_keys=True) + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
            pass


def _append_replay_fence(wal_path: str, seq: int) -> None:
    """Append the `replay-fence` record for one undeclared transactional inverse
    BEFORE recover applies it (item 309 §3a), fsync'd, so a later recovery run
    that finds it does NOT re-apply. Same fsync'd append discipline the WAL
    writer and `_append_discharge` use; a fence FIRES nothing, so appending it is
    safe. Consume-before-fire: the fence is durable before the single attempt
    starts, and a crash between leaves 'fenced-before-attempt, outcome
    unknown'."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415
    from .wal import seal_torn_tail  # noqa: PLC0415 — tier-agnostic core
    seal_torn_tail(wal_path)
    with open(wal_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"record": "replay-fence", "seq": seq},
                                sort_keys=True) + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
            pass


def _append_shared_reclaim_fence(wal_path: str, handle: str) -> None:
    """Append the `shared-reclaim-fence` record for one `shared` grant BEFORE
    recover re-fires its zero-crossing inverse (item 308 S1, issue #96), fsync'd.

    The same consume-before-fire discipline `replay-fence`/`reissue-fence` use,
    and for the same reason: a `shared` grant with `holders > 0` and no orderly
    zero crossing is residue whose forward referent (a remote session, a lock)
    still persists after a whole-process crash, so `revl recover` re-fires the
    declared inverse EXACTLY ONCE. The fence proves an attempt was ABOUT to
    start, never that it ran: a crash between the fence and the fire leaves
    'fenced-before-attempt, outcome unknown' and a second recover run over the
    same durable ledger finds the fence and re-fires NOTHING (no double-close).

    This is the SHIPPED out-of-frame executor S1 named as owed — `revl recover`
    itself, not a new expiry-sweep step in the 294 grant ledger."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415
    from .wal import seal_torn_tail  # noqa: PLC0415 — tier-agnostic core
    seal_torn_tail(wal_path)
    with open(wal_path, "a", encoding="utf-8") as handle_f:
        handle_f.write(json.dumps({"record": "shared-reclaim-fence",
                                   "handle": handle}, sort_keys=True) + "\n")
        handle_f.flush()
        try:
            os.fsync(handle_f.fileno())
        except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
            pass


def _reclaim_record(handle: str, holders: int, basis: str, ok: bool,
                    *, error: Optional[dict] = None,
                    fenced_unknown: bool = False) -> dict:
    """The `reclaim` residue record `revl audit` surfaces (item 308 S1). It is
    DELIBERATELY DISTINCT from a `bracket-fault`: a `bracket-fault` is an
    in-frame inverse that FAILED, whereas a `reclaim` is an inverse that ran (or
    failed, or was fenced-unknown) OUTSIDE any activation during recovery. A
    failed reclaim carries its error in the same record (`ok: false`); there is
    no separate `reclaim-fault`, the merged-schema economy the teardown contract
    keeps."""
    return {
        "record": "reclaim",
        "handle": handle,
        "holders": holders,
        "basis": basis,
        "ok": ok,
        "error": error,
        "outcome": ("unknown" if fenced_unknown else ("ok" if ok else "err")),
    }


def recover_shared_grants(wal: dict, *, wal_path: Optional[str],
                          world: World) -> Optional[dict]:
    """Item 308 S1 (issue #96): re-fire the zero-crossing inverse of every
    `shared` grant a whole-process crash left with `holders > 0` and no orderly
    last release, EXACTLY ONCE, gated by the `shared-reclaim-fence`.

    The runtime journals a ``shared-grant`` record when a `shared` acquire mints
    a counted grant (``{handle, inverse, holders, basis?}``) and a
    ``shared-complete`` record when the orderly last release runs the inverse in
    its own LIFO bracket. So on recovery:

      * a grant with a matching ``shared-complete`` is DONE — the orderly path
        already ran the inverse; skip it (no double-close);
      * a grant with ``holders`` still counted and no completion is RESIDUE
        whose forward referent persists (cross-process `shared` is refused, so a
        whole-process crash is the reachable shape): re-fire the inverse once,
        fenced. A grant already carrying a ``shared-reclaim-fence`` was fenced
        before an attempt in a PRIOR recover run — its outcome is unknown and it
        is NOT re-fired (the fail-closed, no-double-close direction).

    Returns ``None`` for a WAL that declares no shared grant, so a composition
    with no `shared` handle has a byte-identical recover report. Otherwise a
    ``{"reclaims": [...], "clean": bool}`` block the caller attaches under the
    report's ``shared`` key and `revl audit` surfaces as `reclaim` rows."""
    records = wal["records"]
    grants = [r for r in records if r.get("record") == "shared-grant"]
    if not grants:
        return None
    completed = {r.get("handle") for r in records
                 if r.get("record") == "shared-complete"}
    already_fenced = {r.get("handle") for r in records
                      if r.get("record") == "shared-reclaim-fence"}
    # the two crash bases the design (S1, "Crash reclaim") distinguishes: an
    # operator E-Stop leaves a latch beside the WAL (`<wal>.estop`, the durable
    # rendezvous `revl recover --wal` already reconciles against, item 443), so a
    # grant stranded under a halt is `estop-stranded`; otherwise a whole-process
    # crash left the count with no orderly last release, `whole-process`. Either
    # way the count is OVER-reported, never under (R4-safe): a holder halted
    # before its release is still counted, so the handle stays pinned, never
    # closed early.
    from .estop import latch_path, read_latch  # noqa: PLC0415
    basis = ("estop-stranded"
             if read_latch(latch_path(wal=wal_path)) is not None
             else "whole-process")
    # the LATEST grant record per handle carries the holder set at the last
    # durable ledger write (consume/release are appends, so the last one wins).
    latest: dict = {}
    for g in grants:
        latest[g.get("handle")] = g
    reclaims: list = []
    for handle, grant in sorted(latest.items()):
        holders = list(grant.get("holders") or [])
        if handle in completed:
            # a CONFIRMED completion — an orderly last release, or a clean live
            # reclaim — already ran the inverse and it returned; nothing owed, no
            # double-close.
            continue
        if handle in already_fenced:
            # an inverse attempt was durably fenced but no confirmed completion
            # followed it: an orderly zero crossing about to fire, a live reclaim
            # whose inverse raised, or a prior recover run that fenced before its
            # single attempt. The outcome is unknown and a second attempt cannot
            # be proven safe, so it is honest residue and NOT re-fired — even when
            # the last ledger write shows an empty count (a fenced orderly
            # crossing journals holders == [] before the fire). This check comes
            # BEFORE the zero-count skip precisely so that crossing is not read as
            # a clean balance (issues #709/#710).
            reclaims.append(_reclaim_record(
                handle, len(holders), basis, ok=False,
                error={"type": "fenced-before-attempt",
                       "message": "this shared reclaim was fenced before a "
                                  "confirmed completion; its inverse may have "
                                  "raised or crashed mid-attempt, so a second "
                                  "attempt cannot be proven safe (no "
                                  "double-close)"},
                fenced_unknown=True))
            continue
        if not holders:
            # the count reached zero with no attempt ever fenced: a balanced
            # accumulator, nothing owed.
            continue
        inverse = grant.get("inverse") or {}
        if getattr(world, "reclaims_shared", False):
            # issue #1477: through a real binding, the runtime's own reclaim
            # (`runtime.reclaim_shared`) writes the handle's fence before the
            # inverse and `shared-complete` after it; recover writes neither.
            state = world.reclaim_shared([grant]).get(handle, "unresolved")
            if state in ("ran", "settled"):
                reclaims.append(_reclaim_record(handle, len(holders), basis,
                                                ok=True))
                continue
            note = getattr(world, "generation_note", lambda _h: "")(handle)
            reclaims.append(_reclaim_record(
                handle, len(holders), basis, ok=False,
                error={"type": state,
                       "message": {
                           "failed": "the inverse raised when the runtime "
                                     "reclaimed it; its fence stays, so it "
                                     "will not be re-fired",
                           "fenced": "an earlier attempt fenced this reclaim; "
                                     "its outcome is unknown, not re-fired",
                           "stranded": "an E-Stop is in force, nothing ran",
                           "other-generation": f"{note}; not attempted",
                       }.get(state, "the inverse names no host body in this "
                                    "composition binding; not attempted")},
                fenced_unknown=state == "fenced"))
            continue
        if not _re_issues_calls(world):
            reclaims.append(_reclaim_record(
                handle, len(holders), basis, ok=False,
                error={"type": "not-bound",
                       "message": f"not attempted: {UNBOUND_SHARED_RECLAIM}"}))
            continue
        if wal_path is not None and _spends_fences(world):
            _append_shared_reclaim_fence(wal_path, handle)
        try:
            world.apply_inverse(inverse)
        except Exception as error:  # noqa: BLE001 — a reclaim close is fallible
            reclaims.append(_reclaim_record(
                handle, len(holders), basis, ok=False,
                error={"type": type(error).__name__, "message": str(error)}))
            continue
        reclaims.append(_reclaim_record(handle, len(holders),
                                        basis, ok=True))
    return {"reclaims": reclaims,
            "clean": all(r["ok"] for r in reclaims)}


def _append_reissue_fence(wal_path: str, seq: int, register: Optional[str]) -> None:
    """Append the `reissue-fence` record for one owed deferred emission BEFORE
    recover re-fires it (item 440 §(b)), fsync'd. Same consume-before-fire
    discipline as item 309's `replay-fence`, and for the same reason: the fence
    proves an attempt was ABOUT to start, never that it ran, so a crash between
    the fence and the fire leaves 'fenced-before-attempt, outcome unknown'.

    What a later run does with the fence depends on the tier, and this is where
    the register pays: a `read` or `keyed` descriptor is re-dispatchable BY
    CONSTRUCTION, so a second run may fire it again and the fence is only a
    record of what recovery did. A `declared` (trust-me) descriptor is not, so
    its fence is spent and a second run REFUSES with `outcome: "unknown"` — the
    same fail-closed shape 309 gives an undeclared inverse. The register is
    written into the record so a later run reads the tier even from a WAL whose
    descriptor it can no longer resolve."""
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415
    from .wal import seal_torn_tail  # noqa: PLC0415 — tier-agnostic core
    seal_torn_tail(wal_path)
    record = {"record": "reissue-fence", "seq": seq}
    if register:
        record["register"] = register
    with open(wal_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except (OSError, ValueError):  # pragma: no cover — e.g. a pipe target
            pass


def _reissue_permitted(register: Optional[str], strength: Optional[str]) -> bool:
    """Whether the operator's knob admits AUTO-FIRING one owed deferred emission
    (item 440 §(b), item 309 §3b's v2 rule).

    ``strength`` is `None` unless a boundary policy turned the seam on, so the
    default answer is NO for every descriptor — recover auto-fires nothing
    unless an operator asked for it, and the v1 "never auto-fires an owed
    emission" rule is what a policy-less recover still does.

    With the seam on:

    * `read` / `keyed` — admitted at any strength. The fire is free (a read
      changes nothing) or dedup-safe by construction (the descriptor carries the
      key, so a fire that duplicates a pre-crash flush is the remote's dedup, not
      a double-apply). This is the case item 309 §3b classified as free to
      replay and could not act on.
    * `declared` — admitted ONLY at `strength: declared`, the operator's
      explicit "I accept the author's unverified claim".
    * ANYTHING ELSE, including no register at all — never. The ambiguous owed
      emission stays human-finish under every knob setting, because nothing
      about it can be proven and this module does not resolve an ambiguity
      optimistically."""
    if strength is None:
        return False
    if register in REDISPATCH_FREE:
        return True
    return register == "declared" and strength == "declared"


def _roll_forward_window(wal_path: str, wal: dict, approved: dict, *,
                         world: Optional[World] = None,
                         reissue: Optional[str] = None) -> dict:
    """A crash inside the approved-to-discharged window (item 245, Decision 3):
    `commit-approved` is durable but `discharge`/`activation-complete` may not
    be. The session is COMMITTED. Recover replays NO transactional inverse and
    re-issues NO compensation; it rolls the missing discharge record forward and
    reports the flush state per descriptor.

    Two record families carry seqs a commit discharges: the discharge-descriptors
    (243/247 transactional inverses and compensations) and the deferred-emission
    descriptors (245 class-(b) queue entries). Every one not already named in a
    durable discharge record is rolled forward here; a deferred emission with no
    `flushed` record is reported OWED (its host body may not have fired before
    the crash — the honest state)."""
    records = wal["records"]
    descriptors = [r for r in records if r.get("record") == "discharge-descriptor"]
    deferred = [r for r in records if r.get("record") == "deferred-emission"]
    flushed = {r.get("seq") for r in records if r.get("record") == "flushed"}
    flush_failed = {r.get("seq"): r for r in records
                    if r.get("record") == "flush-residue"}
    discharged: set = set()
    for r in records:
        if r.get("record") == "discharge":
            discharged.update(r.get("discharged") or [])

    # roll forward: append a discharge record over every witnessed seq not yet
    # discharged. (Deferred-emission seqs are the queue, not witnessed mutations;
    # their commit is the flush, tracked by `flushed`, not the discharge record.)
    owed_discharge = sorted(d.get("seq") for d in descriptors
                            if d.get("seq") not in discharged)
    if owed_discharge:
        _append_discharge(wal_path, owed_discharge)

    # item 440 §(b): the durable `reissue-fence` records a prior recovery run
    # wrote before firing an owed emission. Read once, before the loop, the same
    # way `_roll_back` reads `replay-fence`.
    reissue_fenced: set = {r.get("seq") for r in records
                           if r.get("record") == "reissue-fence"}

    outstanding: list = []
    fired, owed, reissued = [], [], []
    for d in deferred:
        seq = d.get("seq")
        call = d.get("call") or {}
        referent = f"{call.get('receiver')}.{call.get('method')}"
        if seq in flushed:
            fired.append({"seq": seq, "referent": referent})
        elif seq in flush_failed:
            info = flush_failed[seq].get("error") or {}
            owed.append({"seq": seq, "referent": referent, "outcome": "failed"})
            outstanding.append(_record(
                "flush-residue", crossing=_crossing_of_descriptor(d),
                attempted={"call": call.get("method"),
                           "args": list(call.get("args") or []), "phase": None},
                error=info or {"type": "flush-failed",
                               "message": "the host body raised at flush"},
                attempted_flag=True, outcome="failed", referent=referent,
                hint="the deferred emission's host body raised at flush "
                     "(continue-and-record); finish it by hand or re-run with an "
                     "idempotency key"))
        else:
            # item 440 §(b), item 309 §3b: THE RE-ISSUE SEAM. An owed deferred
            # emission whose descriptor carries a re-dispatchable register MAY be
            # auto-fired here, because the fire is free (a read) or dedup-safe
            # (the descriptor carries the key, so firing after a pre-crash flush
            # that actually landed is the remote's dedup, not a double-apply).
            # Firing needs no runtime: recover re-issues the NAMED CALL against
            # the same `World` adapter that already carries `apply_inverse` and
            # `apply_compensation` (`World.reissue`), which is the seam item 309
            # said it lacked. It is off unless an operator turned it on
            # (`reissue is None` → `_reissue_permitted` is False for every
            # descriptor), and an owed emission with NO register is never fired
            # under any setting.
            register = d.get("register")
            if _reissue_permitted(register, reissue) \
                    and (seq not in reissue_fenced
                         or register in REDISPATCH_FREE):
                entry = _reissue_owed(wal_path, world, d, seq, referent,
                                      register)
                if entry["outcome"] == "reissued":
                    reissued.append(entry)
                    continue
                owed.append({"seq": seq, "referent": referent,
                             "outcome": "failed"})
                outstanding.append(entry["residue"])
                continue
            spent = (seq in reissue_fenced
                     and _reissue_permitted(register, reissue))
            owed.append({"seq": seq, "referent": referent,
                         "outcome": "unknown" if spent else "not-attempted"})
            if spent:
                # the seam is ON and this descriptor's tier admits it, but its
                # single at-most-once attempt was already spent by an earlier
                # recovery run that did not live to record the outcome. The
                # fence proves the fire was ABOUT to start, never that it ran,
                # so a second attempt cannot be proven safe — refuse it and hand
                # it over, exactly as item 309 §3a does for an inverse.
                outstanding.append(_record(
                    "flush-residue", crossing=_crossing_of_descriptor(d),
                    attempted={"call": call.get("method"),
                               "args": list(call.get("args") or []),
                               "phase": None},
                    error={"type": "fenced-before-attempt",
                           "message": "fenced-before-attempt, outcome unknown, "
                                      "will not re-fire — an earlier recovery "
                                      "run fenced this owed emission under the "
                                      "author's unverified `declared` claim; a "
                                      "second attempt cannot be proven safe"},
                    attempted_flag=False, outcome="unknown", referent=referent,
                    hint="declare `idempotent(key: <param>)` so the re-issue is "
                         "dedup-safe by construction and needs no fence, or "
                         "finish this flush by hand (item 440)"))
                continue
            outstanding.append(_record(
                "flush-residue", crossing=_crossing_of_descriptor(d),
                attempted={"call": call.get("method"),
                           "args": list(call.get("args") or []), "phase": None},
                error={"type": "not-attempted",
                       "message": "approved but no `flushed` record — the host "
                                  "body may not have fired before the crash"},
                attempted_flag=False, outcome="not-attempted", referent=referent,
                hint=_owed_hint(register, reissue)))

    clean = not outstanding
    return {
        "verdict": "rolled-forward",
        "decision": ("the WAL carries `commit-approved`: the session was "
                     "committed (Decision 3's window rule). The durable approval, "
                     "not the discharge record, is the commit proof — recover "
                     "replays no witnessed inverse and re-issues no compensation. "
                     "It rolled the missing discharge record forward and reports "
                     "each approved deferred emission's flush state."),
        "hash": approved.get("hash"),
        "committed": True,
        "rolledForwardDischarge": owed_discharge,
        "flushed": fired,
        "owedFlushes": owed,
        # item 440 §(b): owed emissions this run AUTO-FIRED through the re-issue
        # seam. Empty (and the key still present) whenever the seam is off, which
        # is every recover with no operator policy.
        "reissued": reissued,
        "residue": {
            "clean": clean,
            "outstanding": outstanding,
            "proof": _window_proof(owed_discharge, fired, owed, reissued),
        },
        "guarantee": _guarantee(),
    }


def _owed_hint(register: Optional[str], strength: Optional[str]) -> str:
    """The operator hint on an owed emission recover did NOT fire (item 440).

    With the seam OFF the wording is item 245's v1 sentence, byte-for-byte, so a
    policy-less recover's residue is unchanged. With the seam ON it says WHY this
    particular descriptor was not admitted, which is the whole usability point:
    an unregistered emission is ambiguous and stays human-finish, and a
    `declared` one needs the operator's explicit acceptance of an unproven
    claim."""
    if strength is None:
        return ("the emission was approved but its flush is unconfirmed; v1 "
                "recover never auto-fires an owed emission — finish the flush "
                "by hand or re-run with the idempotency key (item 309)")
    if register == "declared":
        return ("the emission was approved but its flush is unconfirmed. Its "
                "register is `declared` (the author's unverified claim) and the "
                "policy admits only dedup-safe re-issues; finish the flush by "
                "hand, declare `idempotent(key: <param>)`, or widen the knob to "
                "`recovery may re-issue owed emissions (strength: declared)` "
                "(item 440)")
    return ("the emission was approved but its flush is unconfirmed, and it "
            "carries NO idempotency register — whether the pre-crash flush "
            "landed cannot be decided, so recovery refuses to fire it under any "
            "policy. Finish the flush by hand, or declare `idempotent(key: "
            "<param>)` so a re-issue is dedup-safe by construction (item 440)")


def _reissue_owed(wal_path: str, world: Optional[World], descriptor: dict,
                  seq, referent: str, register: Optional[str]) -> dict:
    """Fire ONE owed deferred emission through the re-issue seam (item 440 §(b)).

    Consume-before-fire: the `reissue-fence` is fsync'd BEFORE the call, so a
    crash between them leaves a durable 'fenced-before-attempt' record and the
    next run decides by TIER — free for a read/keyed descriptor, refused for a
    `declared` one. The fire itself is `World.reissue`, a forward re-dispatch of
    the named call with its captured arguments; a raising adapter is residue
    (`outcome: "failed"`), never a silent success."""
    call = descriptor.get("call") or {}
    if _has_redacted_arg(call):
        # item 256 Slice 3, same refusal the inverse and compensation paths make:
        # a `Secret[T]` argument never reached the log, so re-issuing would send
        # the placeholder to the remote. No fence is spent; nothing is attempted.
        return {"outcome": "failed", "seq": seq, "referent": referent,
                "residue": _record(
                    "redacted-residue", crossing=_crossing_of_descriptor(descriptor),
                    attempted={"call": call.get("method"),
                               "args": list(call.get("args") or []), "phase": None},
                    error={"type": "redacted-argument",
                           "message": "the owed emission's argument was declared "
                                      "`Secret[T]` and is redacted in the log, so "
                                      "the call cannot be re-issued — not "
                                      "attempted"},
                    attempted_flag=False, outcome="unknown", referent=referent,
                    hint="a confidential value is never written to the WAL. "
                         "Finish this flush by hand with the value from its own "
                         "store, or carry a non-confidential idempotency key")}
    if getattr(world, "reissues_deferred", False):
        return _reissue_through_binding(wal_path, world, descriptor, seq,
                                        referent, register)
    if not _re_issues_calls(world):
        return {"outcome": "failed", "seq": seq, "referent": referent,
                "residue": _unbound_record(_crossing_of_descriptor(descriptor),
                                           call, referent, UNBOUND_OWED_EMISSION)}
    if wal_path is not None and seq is not None and _spends_fences(world):
        _append_reissue_fence(wal_path, seq, register)
    try:
        if world is None:  # pragma: no cover — recover always supplies one
            raise RecoveryError("the re-issue seam needs a world adapter")
        world.reissue(call)
    except Exception as error:  # noqa: BLE001 — a re-issue is fallible
        return {"outcome": "failed", "seq": seq, "referent": referent,
                "residue": _record(
                    "flush-residue", crossing=_crossing_of_descriptor(descriptor),
                    attempted={"call": call.get("method"),
                               "args": list(call.get("args") or []), "phase": None},
                    error={"type": type(error).__name__, "message": str(error)},
                    attempted_flag=True, outcome="failed", referent=referent,
                    hint="the owed emission was re-issued by recovery and the "
                         "adapter raised; the crossing is unconfirmed — finish "
                         "it by hand (item 440)")}
    return {"outcome": "reissued", "seq": seq, "referent": referent,
            "register": register,
            "idempotency": descriptor.get("idempotency")}


def _reissue_through_binding(wal_path: Optional[str], world: World,
                             descriptor: dict, seq, referent: str,
                             register: Optional[str]) -> dict:
    """The re-issue seam through a real composition binding (issue #1477).

    Recover keeps what it always owned: the operator's policy and the tier
    (already decided by the caller) and the consume-before-fire
    `reissue-fence`, written here before the fire. The fire itself is the
    runtime's own flush (`runtime.reissue_deferred`), which checks the E-Stop
    and appends `flushed` (or `flush-residue`) for the seq, so a later recover
    reads the emission as flushed and fires nothing."""
    call = descriptor.get("call") or {}
    if wal_path is not None and seq is not None and _spends_fences(world):
        _append_reissue_fence(wal_path, seq, register)
    state = world.reissue_deferred([descriptor]).get(seq, "unresolved")
    if state in ("ran", "settled"):
        return {"outcome": "reissued", "seq": seq, "referent": referent,
                "register": register,
                "idempotency": descriptor.get("idempotency"),
                "replay": "binding"}
    named = _named_call(call)
    message, kind = {
        "failed": (f"{named} raised when the runtime flushed it "
                   "(`flush-residue` written)", "flush-residue"),
        "stranded": (f"{named}: an E-Stop is in force, so nothing fired",
                     "stranded-residue"),
        "other-generation": (f"{named}: "
                             + getattr(world, "generation_note", lambda _s: "")(seq)
                             + "; not fired", "generation-residue"),
    }.get(state, (f"{named}: names no host body in this composition binding; "
                  "not fired", "unresolved-residue"))
    return {"outcome": "failed", "seq": seq, "referent": referent,
            "residue": _record(
                kind, crossing=_crossing_of_descriptor(descriptor),
                attempted={"call": call.get("method"),
                           "args": list(call.get("args") or []), "phase": None},
                error={"type": state, "message": message},
                attempted_flag=state == "failed",
                outcome="failed" if state == "failed" else "unknown",
                referent=named,
                hint="finish the flush by hand; its reissue-fence is spent, so "
                     "a declared-tier emission will not be fired again")}


def _window_proof(rolled: list, fired: list, owed: list,
                  reissued: Optional[list] = None) -> str:
    reissued = reissued or []
    note = ""
    if reissued:
        keyed = [r for r in reissued if r.get("register") != READ]
        note = (f" {len(reissued)} owed emission(s) were AUTO-FIRED through the "
                f"item-440 re-issue seam under the operator's policy"
                + (f"; {len(keyed)} of them crossed again under a stable "
                   f"idempotency register, so a duplicate is the remote's dedup "
                   f"CONTRACT (never a confirmed fact of a dedup)." if keyed
                   else " (read tier: the calls change nothing, so re-issuing "
                        "them is observationally free)."))
    if not owed:
        return (f"committed: {len(rolled)} witnessed mutation(s) rolled forward "
                f"(discharge appended, none rolled back), {len(fired)} deferred "
                f"emission(s) confirmed flushed.{note} The world holds only what "
                f"the approved commit meant to cross.")
    rule = ("never auto-fired (v1)" if not reissued else
            "left for a human — nothing about them can be proven, so the seam "
            "refused them")
    return (f"committed with {len(owed)} OWED flush(es): the session was approved, "
            f"but {len(owed)} deferred emission(s) have no `flushed` record — their "
            f"host bodies may not have fired before the crash. Reported honestly, "
            f"{rule}; {len(fired)} confirmed flushed, "
            f"{len(rolled)} witnessed mutation(s) rolled forward.{note}")


def _re_issues_calls(world: Optional[World]) -> bool:
    return bool(getattr(world, "re_issues_calls", True))


#: issue #1477: why the composition binding does not make these calls, stated
#: in the verdict so an operator knows what is missing rather than reading
#: "not attempted" as a choice. Each names the runtime entry point that would
#: be needed; none exists in the py runtime today.
UNBOUND_OWED_EMISSION = (
    "this world has no entry point that re-fires an owed deferred emission "
    "(a FORWARD crossing, which must be flushed, never replayed as an "
    "inverse). A composition binding (`revl recover --composition`) fires it "
    "through `runtime.reissue_deferred`, the session's own flush")
UNBOUND_SHARED_RECLAIM = (
    "this world has no entry point that reclaims a shared grant, which is "
    "fenced by HANDLE (`shared-reclaim-fence`, `shared-complete`), not by "
    "seq. A composition binding (`revl recover --composition`) reclaims it "
    "through `runtime.reclaim_shared`")


def _unbound_record(crossing: dict, call: dict, referent: Optional[str],
                    reason: str) -> dict:
    """Residue for a call the bound world cannot make (issue #1477), with the
    reason it cannot. Not attempted, no fence spent."""
    return _record(
        "unbound-residue", crossing=crossing,
        attempted={"call": call.get("method"),
                   "args": list(call.get("args") or []), "phase": None},
        error={"type": "not-bound",
               "message": f"not attempted: {reason}"},
        attempted_flag=False, outcome="not-attempted", referent=referent,
        hint="finish it by hand, or recover against the model to see what "
             "the model would do (drop --composition)")


def _named_call(call: dict) -> str:
    args = call.get("args")
    shown = "<not captured>" if args is None else ", ".join(map(repr, args))
    receiver = call.get("receiver")
    return f"{receiver + '.' if receiver else ''}{call.get('method')}({shown})"


def _replay_through_binding(world: World, descriptors: list, *,
                            outstanding: list, ran: list, rolled_back: list,
                            restore_residue: list, fenced_deferred: list,
                            settled: list, compensations_ran: list) -> None:
    """Hand the open discharge descriptors to a real composition binding and
    fold its per-seq outcomes into the roll-back's lanes (issue #1477).

    Outcomes come from `runtime.replay_descriptors`: ``ran`` and ``failed`` are
    calls made; ``fenced`` is an undeclared inverse whose single attempt an
    earlier run spent; ``settled`` is a seq a discharge or `aborted` record
    already names; ``unresolved`` is a call naming no host body here, or one
    whose arguments were not captured at registration; ``stranded`` is an
    E-Stop in force. The binding adds ``other-generation``: a call a different
    composition wrote, which it never hands over. Only ``ran`` and ``settled``
    are clean. Everything else is residue, named by its call, never reported as
    done. A legacy boundary inverse (``_entry`` set) lands in ``ran`` and its
    residue names its `effect` record; the rest are descriptors."""

    def crossing(d: dict) -> dict:
        return (_crossing_of_effect(d["_effect"]) if "_effect" in d
                else _crossing_of_descriptor(d))

    def handed(d: dict) -> dict:
        return {k: v for k, v in d.items() if not k.startswith("_")}
    replayable, uncaptured = [], []
    for d in descriptors:
        call = d.get("call") or {}
        if _has_redacted_arg(call) or not isinstance(call.get("args"), list):
            uncaptured.append(d)
        else:
            replayable.append(d)
    for d in uncaptured:
        call = d.get("call") or {}
        redacted = isinstance(call.get("args"), list)
        outstanding.append(_record(
            "redacted-residue" if redacted else "unresolved-residue",
            crossing=crossing(d),
            attempted={"call": call.get("method"), "args": list(call.get("args") or []),
                       "phase": 1 if d.get("entry") == "transactional" else 2},
            error={"type": "redacted-argument" if redacted else "args-not-captured",
                   "message": (f"{_named_call(call)}: "
                               + ("an argument was declared `Secret[T]` and is "
                                  "redacted in the log" if redacted else
                                  "its arguments were not captured at "
                                  "registration (an argument is itself a call)")
                               + "; not attempted")},
            attempted_flag=False, outcome="unknown", referent=_named_call(call),
            hint="finish this call by hand; recovery never guesses an argument"))
    outcome = (world.replay_descriptors([handed(d) for d in replayable])
               if replayable else {})
    for d in replayable:
        seq = d.get("seq")
        call = d.get("call") or {}
        named = _named_call(call)
        state = outcome.get(seq, "unresolved")
        transactional = d.get("entry") == "transactional"
        phase = 1 if transactional else 2
        if state == "ran":
            if "_entry" in d:
                ran.append({**d["_entry"], "op": call, "replay": "binding"})
            else:
                (rolled_back if transactional else compensations_ran).append(
                    {"seq": seq, "referent": named, "op": call, "replay": "binding"})
            continue
        if state == "settled":
            settled.append({"seq": seq, "referent": named, "entry": d.get("entry")})
            continue
        if state == "fenced":
            fenced_deferred.append({"seq": seq, "referent": named})
        elif transactional and state != "other-generation":
            restore_residue.append({"seq": seq, "referent": named})
        note = getattr(world, "generation_note", lambda _seq: "")(seq)
        message, kind, hint = {
            "failed": (f"{named} raised when the runtime replayed it",
                       "restore-residue" if transactional else "compensation-residue",
                       "the host body raised; check the referent and finish it "
                       "by hand"),
            "fenced": (f"{named}: fenced-before-attempt, outcome unknown, will "
                       "not re-run: an earlier attempt fenced this undeclared "
                       "inverse", "fenced-residue",
                       "declare `undo idempotent`, or finish this inverse by "
                       "hand: its at-most-once attempt is already spent"),
            "stranded": (f"{named}: an E-Stop is in force, so nothing ran",
                         "stranded-residue",
                         "clear the latch (`revl estop --clear`) and recover "
                         "again"),
            "other-generation": (f"{named}: {note}; not attempted",
                                 "generation-residue",
                                 "run `revl recover --composition` again with "
                                 "the composition that generation ran"),
        }.get(state, (f"{named}: names no host body in this composition "
                      "binding; not attempted", "unresolved-residue",
                      "the call's receiver or method is not bound here; "
                      "finish it by hand"))
        outstanding.append(_record(
            kind, crossing=crossing(d),
            attempted={"call": call.get("method"), "args": list(call.get("args") or []),
                       "phase": phase},
            error={"type": state, "message": message},
            attempted_flag=state == "failed",
            outcome="failed" if state == "failed" else "unknown",
            referent=named, hint=hint))


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


class _CompensationPairing:
    """Which emission a compensation descriptor offsets, read off the WAL
    (issue #1369).

    A compensation's discharge descriptor names the `seq` of the emission's
    `effect` record in `offsets`; the effect record cannot name the descriptor,
    because it is written ahead of the host body and the descriptor only after
    it returns. The timeline also writes a `compensation` effect record for the
    same compensation, whose `boundary.for` is the emission's STEP index.

    `offset_by(record)` is the descriptor seq for an emission effect record
    whose compensation descriptor is SETTLED, meaning a `discharge` or `aborted`
    record names it; None otherwise, and always None for an emission with no
    compensation. `described(record)` is the descriptor seq for a `compensation`
    effect record whose compensation has a descriptor."""

    def __init__(self, records: list, effects: list, descriptors: list) -> None:
        self._settled: set = set()
        #: seqs an `aborted` record names: their compensation already ran
        self.ran_by_abort: set = set()
        for r in records:
            if r.get("record") == "discharge":
                self._settled.update(r.get("discharged") or [])
            elif r.get("record") == "aborted":
                self.ran_by_abort.update(r.get("replayed") or [])
        self._settled |= self.ran_by_abort
        self._by_emission: dict = {
            d.get("offsets"): d.get("seq") for d in descriptors
            if d.get("entry") == "compensation" and isinstance(d.get("offsets"), int)}
        self._emission_seq: dict = {
            (r.get("component"), r.get("stepIndex")): r.get("seq")
            for r in effects if r.get("kind") == "emission"}

    def offset_by(self, record: dict) -> Optional[int]:
        if record.get("kind") != "emission":
            return None
        return self.offset_by_seq(record.get("seq"))

    def offset_by_seq(self, effect_seq: Any) -> Optional[int]:
        """The settled compensation descriptor seq for the emission whose
        `effect` record has `effect_seq`, or None."""
        seq = self._by_emission.get(effect_seq)
        return seq if seq in self._settled else None

    def settle(self, seqs) -> None:
        """Mark descriptor seqs settled that a replay in THIS run just ran or
        found settled, which the WAL read at the start does not show yet. A
        caller that replays descriptors after classifying effect records uses
        it, then asks `offset_by_seq` of each `unreconstructible` entry's
        `seq` again."""
        self._settled.update(seqs)

    def described(self, record: dict) -> Optional[int]:
        if record.get("kind") != "compensation":
            return None
        emission = self._emission_seq.get(
            (record.get("component"), (record.get("boundary") or {}).get("for")))
        return self._by_emission.get(emission)


def _compensation_pairing(records: list, effects: list,
                          descriptors: list) -> "_CompensationPairing":
    return _CompensationPairing(records, effects, descriptors)


def _roll_back(wal: dict, *, world: World, wal_path: Optional[str] = None) -> dict:
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
    pairing = _compensation_pairing(wal["records"], effects, descriptors)

    # seed the world with every boundary referent the WAL says was created and
    # outlives the process — this is the external state a crash orphaned.
    seeded: dict = {}
    for record in effects:
        if pairing.described(record) is not None or pairing.offset_by(record) is not None:
            continue   # reported by its descriptor, or offset: not out
        referent = _referent_key(record, world)
        if referent is not None:
            world.seed(referent, record.get("label"))
            seeded[id(record)] = referent

    # item 309 §3a extended to the reconstructible legacy-boundary family (the
    # `record_boundary` acquire path): the durable per-inverse fences a prior
    # apply path wrote before applying an UNDECLARED inverse. A seq named here
    # has already spent its at-most-once attempt, so this run does NOT
    # re-apply it — the same invariant the transactional family already gets
    # below, extended to this family instead of a parallel mechanism. `seq` is
    # drawn from the WAL's single shared counter (`WriteAheadLog._seq`), so an
    # effect record's seq and a discharge-descriptor's seq share one namespace
    # and this set is read once for both families.
    fenced_seqs: set = {r.get("seq") for r in wal["records"]
                        if r.get("record") == "replay-fence"}
    fenced_deferred: list = []

    outstanding: list = []
    ran, moot, unreconstructible = [], [], []
    offset, described = [], []
    bound_legacy: list = []   # issue #1477, filled only through a real binding
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
            "seq": record.get("seq"),   # the effect record's, for the pairing
        }
        if pairing.described(record) is not None:
            # the timeline's own record of a compensation that ALSO has a
            # discharge descriptor: the descriptor is the re-issuable form and
            # the descriptor lane below reports it. Counting it here too would
            # report one compensation twice, once as closure-only.
            described.append({**entry, "descriptor": pairing.described(record)})
            continue
        if pairing.offset_by(record) is not None:
            # an emission whose compensation descriptor is SETTLED (issue
            # #1369): a discharge or `aborted` record names it, so the
            # compensation ran. An emission cannot be inverted, only offset,
            # and this one was.
            offset.append({**entry, "compensation": pairing.offset_by(record)})
            continue
        if referent is None:
            # in-process: the memory it acted on died with the process; running
            # its inverse would be a no-op. Moot, not residue.
            moot.append({**entry,
                         "why": "in-process referent — died with the process; "
                                "its inverse is a no-op after restart"})
            continue
        if inverse.get("reconstructible") \
                and getattr(world, "replays_descriptors", False):
            # issue #1477: through a real binding, a legacy boundary inverse is
            # the transactional entry it always was, a named call with captured
            # arguments. It joins the descriptor family's batch below: one seq
            # space, so one LIFO order, and the runtime's own fence and
            # `aborted` record for it.
            bound_legacy.append({"record": "effect", "seq": record.get("seq"),
                                 "entry": "transactional",
                                 "call": inverse.get("op") or {},
                                 "undo_idempotent": bool(
                                     inverse.get("undo_idempotent")
                                     or inverse.get("register") == READ),
                                 "_entry": entry, "_effect": record})
            continue
        if inverse.get("reconstructible") and not _re_issues_calls(world):
            outstanding.append(_unbound_record(
                _crossing_of_effect(record), inverse.get("op") or {}, referent,
                "this world makes no calls outside the discharge-descriptor "
                "family"))
            continue
        if inverse.get("reconstructible"):
            seq = record.get("seq")
            declared_idempotent = bool(inverse.get("undo_idempotent"))
            # item 440: the third tier. A `register: "read"` inverse changes
            # nothing, so it is re-dispatchable with no key and no fence; the
            # fenced branch below is skipped entirely and a second recovery run
            # never escalates it to an operator.
            tier = _replay_tier(inverse.get("register"), declared_idempotent)
            op = inverse["op"]
            if tier == "fenced" and seq in fenced_seqs:
                # item 309 §3a, extended: this undeclared inverse's single
                # at-most-once attempt was already spent on an earlier
                # `recover` pass (there is no in-process abort for this family
                # — `record_boundary` is a standalone WAL write, not wired to
                # the runtime's transactional disposer — so the ONLY prior
                # apply path that can have fenced this seq is a previous
                # recovery run). The fence proves an attempt was ABOUT to
                # start, never that it ran, so a second attempt is refused and
                # handed to a human, exactly like the transactional family.
                fenced_deferred.append({"seq": seq, "referent": referent})
                outstanding.append(_record(
                    "fenced-residue",
                    crossing=_crossing_of_effect(record),
                    attempted={"call": op.get("method"),
                               "args": list(op.get("args") or []), "phase": None},
                    error={"type": "fenced-before-attempt",
                           "message": "fenced-before-attempt, outcome unknown, "
                                      "will not re-run — an earlier recovery "
                                      "run fenced this undeclared inverse; a "
                                      "second attempt cannot be proven safe"},
                    attempted_flag=False, outcome="unknown", referent=referent,
                    hint="declare the boundary inverse idempotent (free "
                         "replay) or finish this inverse by hand — its "
                         "at-most-once attempt is already spent (item 309)"))
                continue
            # A DECLARED-idempotent inverse replays FREELY on every recovery
            # run (no fence, no bookkeeping). An UNDECLARED one takes its
            # single fenced attempt: the fence is fsync'd BEFORE the apply so
            # a crash between them leaves a fence and no double-apply (§3a,
            # consume-before-fire).
            if (tier == "fenced" and wal_path is not None and seq is not None
                    and _spends_fences(world)):
                _append_replay_fence(wal_path, seq)
                fenced_seqs.add(seq)
            world.apply_inverse(op)
            ran.append({**entry, "op": op, "replay": tier})
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
        if d.get("entry") == "compensation" and d.get("seq") in pairing.ran_by_abort:
            continue   # already ran: nothing of it is owed
        world.seed(world.key(d.get("call") or {}), d.get("entry"))

    transactional = [d for d in descriptors if d.get("entry") == "transactional"]
    compensations = [d for d in descriptors if d.get("entry") == "compensation"]
    transactional_rolled_back, discharged_skipped, restore_residue = [], [], []
    # `fenced_seqs`/`fenced_deferred` were computed above, before the legacy
    # `effect` loop, and are shared across both record families (one WAL seq
    # namespace). item 309 follow-up: the in-process abort's COMPLETION record.
    # A fence means "fenced-before-attempt, outcome UNKNOWN" only when the
    # process died mid-abort (no `aborted` record). When the `aborted`
    # completion record IS present, the in-process abort ran Phase 1 to
    # completion, so every fenced inverse's apply DID run — the outcome is
    # KNOWN (clean), not unknown. The report surfaces this same signal as
    # `abortCompleted`. This only applies to the transactional family below —
    # the legacy `record_boundary` family has no in-process abort wired to it,
    # so no `aborted` record can ever explain one of its fences.
    abort_completed: bool = any(r.get("record") == "aborted"
                                for r in wal["records"])
    settled_by_replay: list = []
    compensations_ran: list = []
    if getattr(world, "replays_descriptors", False):
        # issue #1477: the real world. The whole family goes to the runtime's
        # own abort path in one batch (fences, budget and the `aborted` record
        # are the runtime's), and the walk below has nothing left to do.
        _replay_through_binding(
            world, bound_legacy + [d for d in transactional + compensations
                                   if d.get("seq") not in discharged],
            outstanding=outstanding, ran=ran,
            rolled_back=transactional_rolled_back,
            restore_residue=restore_residue, fenced_deferred=fenced_deferred,
            settled=settled_by_replay, compensations_ran=compensations_ran)
        # issue #1369: the effect records were classified before the replay,
        # against the WAL as read. A compensation this replay just ran (or
        # found settled) offsets its emission, so re-ask the pairing for every
        # emission still counted closure-only residue.
        pairing.settle({e["seq"] for e in compensations_ran}
                       | {e["seq"] for e in settled_by_replay})
        still = []
        for e in unreconstructible:
            by = pairing.offset_by_seq(e.get("seq"))
            if by is None:
                still.append(e)
                continue
            offset.append({k: v for k, v in e.items() if k not in ("reason", "still_out")}
                          | {"compensation": by})
            outstanding[:] = [o for o in outstanding
                              if not (o.get("kind") == "unreconstructible"
                                      and o.get("referent") == e["still_out"])]
        unreconstructible[:] = still
        for d in transactional + compensations:
            if d.get("seq") in discharged:
                discharged_skipped.append({
                    "seq": d.get("seq"), "referent": world.key(d.get("call") or {}),
                    "retained": d.get("entry") == "transactional"})
        transactional, compensations = [], []
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
        declared_idempotent = bool(d.get("undo_idempotent"))
        # item 440: `read` | `free` | `fenced`. Only `fenced` consults the fence
        # set and only `fenced` writes one — a read and a declared-idempotent
        # inverse both replay freely, for different reasons the report keeps
        # apart (`replay: "read"` vs `replay: "free"`).
        tier = _replay_tier(d.get("register"), declared_idempotent)
        if tier == "fenced" and seq in fenced_seqs and abort_completed:
            # item 309 follow-up: a COMPLETED in-process abort. The `aborted`
            # completion record is present, so the abort's Phase 1 ran this fenced
            # inverse's apply to completion — its outcome is KNOWN (it ran), not
            # unknown. So it is RESOLVED: count it as cleanly rolled back, keep
            # residue clean, and do NOT re-apply it (that would be a double-apply;
            # a completed abort is never re-run on recover). Only the crash case
            # below — no `aborted` record — is genuine fenced-residue.
            transactional_rolled_back.append({"seq": seq, "referent": referent,
                                              "op": call,
                                              "replay": "abort-phase1"})
            continue
        if tier == "fenced" and seq in fenced_seqs:
            # item 309 §3a: an UNDECLARED inverse whose single at-most-once
            # attempt was already spent on another apply path (the headline
            # abort-then-crash: Phase 1 fenced-and-applied, then the process died
            # before discharge). The fence proves the attempt was ABOUT to start,
            # never that it ran, so the honest residue is "outcome unknown", never
            # "attempted once". A second attempt cannot be proven safe, so it is
            # refused automatically and handed to a human with the referent named.
            fenced_deferred.append({"seq": seq, "referent": referent})
            outstanding.append(_record(
                "fenced-residue",
                crossing=_crossing_of_descriptor(d),
                attempted={"call": call.get("method"),
                           "args": list(call.get("args") or []), "phase": 1},
                error={"type": "fenced-before-attempt",
                       "message": "fenced-before-attempt, outcome unknown, will "
                                  "not re-run — a prior apply path (abort Phase 1 "
                                  "or an earlier recovery run) fenced this "
                                  "undeclared inverse; a second attempt cannot be "
                                  "proven safe"},
                attempted_flag=False, outcome="unknown", referent=referent,
                hint="declare `undo idempotent` (free replay) or add an "
                     "idempotency key, or finish this inverse by hand — its "
                     "at-most-once attempt is already spent (item 309)"))
            continue
        if _has_redacted_arg(call):
            # item 256 Slice 3: the descriptor's argument was declared `Secret[T]`
            # and never reached the log, so this inverse is not reconstructible in
            # a fresh process. Refuse rather than re-issue against the placeholder
            # — that would address the wrong referent and report a miss as clean.
            # No fence is spent: nothing was attempted.
            restore_residue.append({"seq": seq, "referent": referent})
            outstanding.append(_record(
                "redacted-residue",
                crossing=_crossing_of_descriptor(d),
                attempted={"call": call.get("method"),
                           "args": list(call.get("args") or []), "phase": 1},
                error={"type": "redacted-argument",
                       "message": "the inverse's argument was declared "
                                  "`Secret[T]` and is redacted in the log, so "
                                  "the call cannot be reconstructed — not "
                                  "attempted"},
                attempted_flag=False, outcome="unknown", referent=referent,
                hint="a confidential value is never written to the WAL (it is "
                     "plaintext at rest). Re-issue this inverse by hand with the "
                     "value from its own store, or carry a non-confidential "
                     "handle (an idempotency key, a row id) as the witness"))
            continue
        # A DECLARED-idempotent inverse replays FREELY on every recovery run (no
        # fence, no bookkeeping) — `revl recover` is itself idempotent over the
        # declared subset (§3a). An UNDECLARED one takes its single fenced
        # attempt: the fence is fsync'd BEFORE the apply so a crash between them
        # leaves a fence and no double-apply (§3a, consume-before-fire).
        if tier == "fenced" and wal_path is not None and _spends_fences(world):
            _append_replay_fence(wal_path, seq)
            fenced_seqs.add(seq)
        # ABORTED / undischarged: reconstruct and run the declared inverse.
        try:
            world.apply_inverse(call)
            transactional_rolled_back.append({"seq": seq, "referent": referent,
                                              "op": call, "replay": tier})
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
    compensations_settled = []
    for d in sorted(compensations, key=lambda x: x.get("seq", 0), reverse=True):
        call = d.get("call") or {}
        referent = world.key(call)
        if d.get("seq") in pairing.ran_by_abort:
            # an `aborted` record names it: an in-process abort or a replay in
            # a fresh process (`runtime.replay_descriptors`) already ran this
            # compensation. Re-issuing it would run it twice (issue #1369).
            compensations_settled.append({"seq": d.get("seq"), "referent": referent})
            continue
        if d.get("seq") in discharged:
            # discharged on a clean unload: a compensation is never owed on
            # success (the forward emission was the deliverable). Skip, no residue.
            discharged_skipped.append({"seq": d.get("seq"), "referent": referent,
                                       "retained": False})
            continue
        if _has_redacted_arg(call):
            # Same refusal as Phase 1: a compensation whose argument was declared
            # `Secret[T]` cannot be re-issued from the log, so it is reported as
            # residue rather than applied against the placeholder.
            outstanding.append(_record(
                "redacted-residue",
                crossing=_crossing_of_descriptor(d),
                attempted={"call": call.get("method"),
                           "args": list(call.get("args") or []), "phase": 2},
                error={"type": "redacted-argument",
                       "message": "the compensation's argument was declared "
                                  "`Secret[T]` and is redacted in the log, so "
                                  "the call cannot be reconstructed — not "
                                  "attempted"},
                attempted_flag=False, outcome="unknown", referent=referent,
                hint="the forward crossing is still out. Offset it by hand with "
                     "the value from its own store, or carry a non-confidential "
                     "handle as the compensation's argument"))
            continue
        world.apply_compensation(call)   # forced record-branch: never pops
        compensations_reissued.append({"seq": d.get("seq"), "referent": referent})
        # item 309 §3c: a KEYED compensation's re-attempt stays best-effort and
        # stays a RECORD (247's honesty: compensation is never inversion), but the
        # residue message upgrades to claim the remote's dedup CONTRACT (never the
        # fact of a dedup — revl verified only that the key is stable). An unkeyed
        # compensation keeps today's wording exactly, so existing residue tests
        # are untouched.
        keyed = d.get("idempotency")
        if keyed:
            message = ("re-issued under a stable idempotency key; the remote's "
                       "dedup contract prevents double-apply (the contract, "
                       "conditional on its retention window — never the confirmed "
                       "fact of a dedup)")
            hint = ("the compensation was re-attempted best-effort under key "
                    f"`{keyed}`; revl verified the key was stable, not that the "
                    "remote dedup'd — the forward referent is still out")
        else:
            message = ("re-issued best-effort; the emission's landing cannot be "
                       "confirmed in a fresh process")
            hint = ("the compensation was re-attempted best-effort; its landing "
                    "cannot be confirmed after a crash — the forward referent is "
                    "still out. Verify it was offset, or carry an idempotency key")
        outstanding.append(_record(
            "compensation-residue",
            crossing=_crossing_of_descriptor(d),
            attempted={"call": call.get("method"),
                       "args": list(call.get("args") or []), "phase": 2},
            error={"type": "unconfirmed", "message": message},
            attempted_flag=True, outcome="unknown", referent=referent,
            hint=hint))

    # item 245, Decision 3: class-(b) deferred emissions with no `commit-approved`
    # are DROPPED — never fired, so zero crossings. Reported clean, never residue:
    # this is the exact-by-construction abort path (dropping is free, nothing
    # crossed the boundary). An in-process abort that finished writes an
    # `aborted` completion record; its presence tells a completed abort from a
    # crashed one, but the absence of `commit-approved` is the verdict either way.
    deferred = [r for r in wal["records"] if r.get("record") == "deferred-emission"]
    dropped_deferred = [{"seq": r.get("seq"),
                         "referent": f"{(r.get('call') or {}).get('receiver')}."
                                     f"{(r.get('call') or {}).get('method')}"}
                        for r in deferred]
    aborted = next((r for r in wal["records"] if r.get("record") == "aborted"), None)

    remaining = world.remaining()
    clean = not outstanding
    return {
        "verdict": "rolled-back",
        "decision": ("the WAL has no `commit-approved` marker: the session "
                     "aborted (a clean abort or a mid-activation crash). Recovery "
                     "reconstructed the boundary inverses from their descriptors "
                     "and ran them newest-first (LIFO), L-Raise style. "
                     "Transactional inverses with a durable discharge record were "
                     "skipped (committed, not rolled back); owed compensations "
                     + ("were re-issued through the composition binding by the "
                        "runtime's own abort path; "
                        if getattr(world, "replays_descriptors", False) else
                        "were re-attempted best-effort; ")
                     + "deferred emissions were dropped, never fired."),
        "committedEffects": len(effects),
        "torn": wal.get("torn", False),
        "ran": ran,
        "moot": moot,
        "unreconstructible": unreconstructible,
        # issue #1369: emissions whose compensation descriptor is settled, and
        # compensation records the descriptor lane reports instead
        "offset": offset,
        "compensationRecordsDescribed": described,
        "transactionalRolledBack": transactional_rolled_back,
        "dischargedSkipped": discharged_skipped,
        "compensationsReissued": compensations_reissued,
        "compensationsSettled": compensations_settled,
        # issue #1477: through a real composition binding, a compensation the
        # runtime ran is PERFORMED (its host body returned), not re-attempted
        # best-effort, and a seq an earlier replay already settled is named.
        # Both empty against the model.
        "compensationsRan": compensations_ran,
        "settledByReplay": settled_by_replay,
        # item 309 §3a: undeclared inverses whose single at-most-once attempt was
        # already spent (fenced), refused this run and deferred to a human.
        "fencedDeferred": fenced_deferred,
        "droppedDeferred": dropped_deferred,
        "abortCompleted": aborted is not None,
        "residue": {
            "clean": clean,
            "outstanding": outstanding,
            "worldRemaining": remaining,
            "proof": _residue_proof(ran, moot, outstanding, remaining,
                                    discharged_skipped, transactional_rolled_back,
                                    compensations_reissued, dropped_deferred,
                                    compensations_ran, settled_by_replay),
        },
        "guarantee": _guarantee(),
    }


def _residue_proof(ran: list, moot: list, outstanding: list, remaining: list,
                   discharged_skipped: list, transactional_rolled_back: list,
                   compensations_reissued: list,
                   dropped_deferred: Optional[list] = None,
                   compensations_ran: Optional[list] = None,
                   settled_by_replay: Optional[list] = None) -> str:
    committed = [d for d in discharged_skipped if d.get("retained")]
    # issue #1477: what a real composition binding did, stated first. Empty
    # (and so absent from the proof) against the model.
    bound = ""
    if compensations_ran:
        bound += (f" {len(compensations_ran)} compensation(s) ran through the "
                  f"composition binding and returned.")
    if settled_by_replay:
        bound += (f" {len(settled_by_replay)} descriptor(s) were already "
                  f"settled by an earlier replay; nothing was re-run.")
    ran_n = len(ran) + len(transactional_rolled_back)
    dropped_n = len(dropped_deferred or [])
    dropped_note = (f" {dropped_n} deferred emission(s) dropped, never fired "
                    f"(exact by construction — nothing crossed)."
                    if dropped_n else "")
    if not outstanding:
        note = ""
        if committed:
            note = (f" {len(committed)} committed transactional mutation(s) were "
                    f"retained (discharge record durable — a committed transaction "
                    f"is never rolled back).")
        return (f"no residue: {ran_n} reconstructed boundary inverse(s) ran and "
                f"cleared every referent they owed; {len(moot)} in-process "
                f"inverse(s) were moot (memory gone).{bound}{note}{dropped_note} The "
                f"world holds only what was deliberately committed.")
    kinds: dict = {}
    for rec in outstanding:
        kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
    breakdown = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    return (f"RESIDUE: {len(outstanding)} outstanding record(s) ({breakdown});{bound} "
            f"{len(remaining)} referent(s) still out in the world "
            f"({', '.join(remaining) or 'none named'}). "
            f"{len(compensations_reissued)} compensation(s) were re-attempted "
            f"best-effort and cannot be confirmed. Reported honestly — the WAL "
            f"never claimed a dead closure ran, and a re-issued compensation is "
            f"an offset that records, never an inversion that clears.")


def _guarantee() -> str:
    from .wal import WAL_GUARANTEE  # noqa: PLC0415 — tier-agnostic core
    return WAL_GUARANTEE


# ---------------------------------------------------------------------------
# rendering (for `revl recover`)
# ---------------------------------------------------------------------------


def render(report: dict) -> str:
    # issue #1477: a model run says so on its second line and on every line
    # that reports a call against the world, so none of them reads as an effect
    # undone out there. A verdict with no `world` (a hand-built report) renders
    # as before.
    model = report.get("world") == WORLD_MODEL
    tag = f" [{MODELLED}]" if model else ""
    lines = [f"verdict: {report['verdict'].upper()}"]
    if model:
        lines.append(f"world: MODEL. Recovery ran against an in-memory model; "
                     f"every call below was {MODELLED}. The outside world was "
                     f"not touched.")
    elif report.get("world") == WORLD_REAL and report.get("binding"):
        binding = report["binding"]
        booted = ", ".join(binding.get("booted") or []) or "none"
        lines.append(f"world: REAL. Recovery replayed the WAL's discharge "
                     f"descriptors through {', '.join(binding.get('composition') or [])} "
                     f"({binding.get('digest', '')[:19]}...); providers booted: "
                     f"{booted}.")
    lines += [report["decision"], ""]
    if report["verdict"] == "rolled-forward" and "owedFlushes" in report:
        # item 245's approved-to-discharged window verdict shares the
        # `rolled-forward` name but carries a different body (no
        # `committedEffects`/`components`/`resumed`), so it gets its own render
        # arm; without it `revl recover` raised KeyError on exactly the WAL the
        # item-440 re-issue seam acts on.
        lines.append("  rolled forward discharge: "
                     + (", ".join(str(s) for s in report.get(
                         "rolledForwardDischarge") or []) or "(none)"))
        for entry in report.get("flushed") or []:
            lines.append(f"  flushed  seq {entry['seq']:<3} {entry['referent']}")
        for entry in report.get("reissued") or []:
            key = entry.get("idempotency")
            under = (f"register {entry.get('register')!r}"
                     + (f", key {key!r}" if key else ""))
            lines.append(f"  re-issued seq {entry['seq']:<3} {entry['referent']} "
                         f"— auto-fired by the item-440 seam ({under}){tag}")
        for entry in report.get("owedFlushes") or []:
            lines.append(f"  OWED     seq {entry['seq']:<3} {entry['referent']} "
                         f"— {entry['outcome']}")
    elif report["verdict"] == "rolled-forward":
        lines.append(f"  committed effects (all balanced): {report['committedEffects']}")
        lines.append(f"  components: {', '.join(report['components']) or '(none)'}")
        lines.append(f"  resumed persisted generation: {report['resumed']}")
        steady = report.get("steadyState") or {}
        for entry in steady.get("crossed") or []:
            lines.append(f"  RESIDUE  {entry['label'] or '(effect)':<22} "
                         f"steady-state crossing — still out (post-activation, no "
                         f"clean shutdown): {entry['still_out']}")
        for entry in steady.get("moot") or []:
            lines.append(f"  moot     {entry['label'] or '(effect)':<22} "
                         f"steady-state in-process crossing (memory gone)")
        # issue #1017: the deferral cross-check. Both lists are empty on a WAL
        # with no `deferred-emission`, so a deferral-free roll-forward renders
        # byte-identically.
        deferrals = report.get("deferrals") or {}
        for entry in deferrals.get("flushed") or []:
            lines.append(f"  flushed  seq {entry['seq']:<3} {entry['referent']}")
        for entry in deferrals.get("owed") or []:
            lines.append(f"  OWED     seq {entry['seq']:<3} {entry['referent']} "
                         f"— {entry['outcome']}: approved, no `flushed` record "
                         f"under `activation-complete`")
    elif report["verdict"] == "roll-forward-refused":
        lines.append("  " + (report.get("message")
                             or "the persisted generation no longer passes the gate"))
        lines.append("")
        lines.append(f"guarantee: {report['guarantee']}")
        return "\n".join(lines)
    elif report["verdict"] == "roll-forward-needs-approval":
        ticket = report.get("ticket") or {}
        lines.append(f"  class-(c) activation crossing held for approval: "
                     f"{ticket.get('component') or '(component)'}")
    else:
        for entry in report.get("ran") or []:
            op = entry.get("op") or {}
            call = (f"{op.get('receiver')}.{op.get('method')}"
                    f"({', '.join(map(repr, op.get('args') or []))})")
            lines.append(f"  ran      {entry['label']:<22} {call}{tag}")
        for entry in report.get("moot") or []:
            lines.append(f"  moot     {entry['label']:<22} in-process (memory gone)")
        for entry in report.get("unreconstructible") or []:
            lines.append(f"  RESIDUE  {entry['label']:<22} closure-only — still out: "
                         f"{entry['still_out']}")
        for entry in report.get("offset") or []:
            lines.append(f"  offset   {entry['label']:<22} its compensation, seq "
                         f"{entry['compensation']}, is settled")
        for entry in report.get("compensationRecordsDescribed") or []:
            lines.append(f"  paired   {entry['label']:<22} reported by its "
                         f"descriptor, seq {entry['descriptor']}")
        for entry in report.get("transactionalRolledBack") or []:
            lines.append(f"  rolled-back  seq {entry['seq']:<3} transactional inverse "
                         f"re-issued — {entry['referent']}{tag}")
        for entry in report.get("dischargedSkipped") or []:
            tag = "retained (committed)" if entry.get("retained") else "discharged"
            lines.append(f"  skipped   seq {entry['seq']:<3} {tag} — not rolled back: "
                         f"{entry['referent']}")
        for entry in report.get("compensationsSettled") or []:
            lines.append(f"  settled  compensation seq {entry['seq']:<3} an `aborted` "
                         f"record names it — ran, not re-issued: {entry['referent']}")
        for entry in report.get("compensationsRan") or []:
            lines.append(f"  ran      compensation seq {entry['seq']:<3} "
                         f"{entry['referent']} - performed, returned")
        for entry in report.get("settledByReplay") or []:
            lines.append(f"  settled  {entry['entry']} seq {entry['seq']:<3} "
                         f"{entry['referent']} - an earlier replay settled it; "
                         f"not re-run")
        for entry in report.get("compensationsReissued") or []:
            lines.append(f"  RESIDUE  compensation seq {entry['seq']:<3} re-attempted "
                         f"best-effort — still out: {entry['referent']}{tag}")
        for rec in (report.get("residue") or {}).get("outstanding") or []:
            # issue #1477: the binding's own refusals, named by their call.
            if rec.get("kind") in ("unresolved-residue", "stranded-residue",
                                   "unbound-residue", "generation-residue"):
                lines.append(f"  RESIDUE  {rec.get('referent')} - "
                             f"{(rec.get('error') or {}).get('message')}")
        for entry in report.get("fencedDeferred") or []:
            lines.append(f"  FENCED   seq {entry['seq']:<3} undeclared inverse — "
                         f"fenced-before-attempt, will not re-run: {entry['referent']}")
        for entry in report.get("ran") or []:
            if entry.get("replay") == "read":
                lines.append(f"  read     {entry['label']:<22} re-dispatched freely "
                             f"— the inverse changes nothing (item 440){tag}")
    # design 460 §5: one line per un-finalized two-phase admission, in either
    # verdict. Present only when the WAL carried an admission the scan classified,
    # so a session that never admitted renders byte-identically.
    for entry in report.get("admissions") or []:
        did = (entry.get("decisionId") or "")[:19]
        tag = {"owed": "OWED", "advanced": "advanced", "stale": "STALE",
               "ambiguous": "AMBIGUOUS",
               "unverified": "UNVERIFIED"}.get(entry.get("classification"),
                                               entry.get("classification"))
        fin = " finalized" if entry.get("finalized") else ""
        fin = " abandoned" if entry.get("abandoned") else fin
        # design 460 §4: name the journal-served fenced crossings on an advanced
        # decision (served from the journal, dispatched zero) and the in-flight
        # ones that refused an ambiguous decision.
        served = entry.get("served")
        if served:
            fin += (f" [served {len(served)} fenced crossing(s) from the journal,"
                    f" dispatched {entry.get('dispatched', 0)}]")
        if entry.get("inFlight"):
            fin += f" [fenced crossing(s) in flight: {entry['inFlight']}]"
        lines.append(f"  admission {tag:<9} {did}...{fin} — "
                     f"{entry.get('decision', '')}")
    # item 308 S1 (issue #96): one line per `shared` reclaim, so a lease-lapse
    # close is legible to the operator as a `reclaim` — distinct from an in-frame
    # `bracket-fault` — end to end. Present only when the WAL carried a shared
    # grant, so a handle-free session renders byte-identically.
    shared = report.get("shared")
    if shared is not None:
        for entry in shared.get("reclaims") or []:
            tag = "reclaim " if entry["ok"] else "RECLAIM!"
            lines.append(
                f"  {tag} {entry['handle']:<20} shared last holder gone "
                f"({entry['basis']}, holders={entry['holders']}) — "
                f"{entry['outcome']}{tag}")
    residue = report["residue"]
    state = "CLEAN" if residue["clean"] else "RESIDUE"
    where = " in the model" if model else ""
    lines += ["", f"residue proof [{state}]{where}:",
              f"  {residue['proof']}"]
    lineage = report.get("lineage")
    if lineage:
        lines += ["", f"lineage: branch of {lineage['parent']} at step "
                      f"{lineage['divergedAt']}", f"  {lineage['note']}"]
    lines += ["", f"guarantee: {report['guarantee']}"]
    return "\n".join(lines)

"""Every mutating MCP response carries its exact undo (issue #1703).

An agent that can cheaply revert an attempt tries more. revl already has the
machinery to go back (`revl_undo` over the retained generation history,
`revl_restore` over a snapshot, `revl_unload`); what an agent loop lacked was
knowing WHICH call reverts the one it just made, with WHICH arguments. So a
successful mutating call now answers with

    "undo": {"tool": <name>, "arguments": {...}}

the one call that returns the session to the state it had before, and
`revl_step_back` with no arguments runs the last one recorded.

Where no exact inverse exists the response says so instead of guessing:
`"undo": null` with `"undoReason"`. A boundary crossing (`revl_call`,
`revl_commit_confirm`, `revl_deploy`) cannot be un-emitted, compensation is
not inversion (docs/erase-report.md); a halt, an approval decision or an
override is recorded evidence; a timeline move or a fork has no inverse that
leaves the session byte for byte where it was. Each reason is in
`IRREVERSIBLE` below.

The reversible verbs are the ones that move the composition between
generations (`load`, `swap`, `edit`, `change`, `undo`, `rollback`, `restore`,
`unload`, and the composed `ship`/`repair` when they apply), plus a fresh
lease claim, whose inverse is the release. A committed `revl_change` is a
generation move like an edit, undone by `revl_undo` to the generation before
it; a speculative one leaves the running generation unchanged and says so.
`revl_export` writes disk, not the session, and is in `IRREVERSIBLE`. The inverse of a generation move
is a gated change itself: `revl_undo` re-admits the earlier generation's
sources through the same gate a swap runs (docs/generation-history.md), so an
undo never bypasses admission.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Mutating verbs that move the composition between generations.
GENERATION_VERBS = frozenset({
    "revl_load", "revl_swap", "revl_edit", "revl_undo", "revl_rollback",
    "revl_restore", "revl_unload", "revl_ship", "revl_repair", "revl_change",
})

#: Mutating verbs with no exact inverse, and why. A response from one of these
#: carries `undo: null` and the reason, never a call that only looks like one.
IRREVERSIBLE = {
    "revl_call": "a call reaches the running composition and may cross the "
                 "boundary; an emission cannot be un-emitted (compensation is "
                 "not inversion, docs/erase-report.md)",
    "revl_commit": "the commit manifest step records the session's pending "
                   "crossings for confirmation; it has no inverse call",
    "revl_commit_confirm": "a confirmed commit fires the deferred crossings; "
                           "they cannot be un-emitted",
    "revl_abort": "an abort drops the deferred crossings and replays the "
                  "witnessed inverses; there is no call that re-queues them",
    "revl_deploy": "a deploy reconfigures a composition on another host; its "
                   "crossings left this session",
    "revl_estop": "an armed E-Stop is a halt with residue records; it is "
                  "cleared by an operator, not undone",
    "revl_approve": "an approval decision is recorded evidence",
    "revl_escalate": "an escalation is recorded evidence",
    "revl_revoke": "a revocation is recorded evidence",
    "revl_apply_distillation": "an applied distillation is recorded evidence",
    "revl_revoke_distillation": "a revoked distillation is recorded evidence",
    "revl_override": "an override is recorded evidence with its reason",
    "revl_fork": "a fork proposal opens a two-step protocol; it has no inverse "
                 "call that leaves the session byte for byte where it was",
    "revl_fork_confirm": "a confirmed fork rewinds the recorded tail; the "
                         "discarded steps are not replayable byte for byte",
    "revl_step_back": "a timeline step back unwinds recorded steps; replaying "
                      "them forward re-runs them rather than restoring them",
    "revl_replay_forward": "a forward replay re-runs recorded steps; there is "
                           "no call that un-runs them",
    "revl_export": "an export writes the held source over the files on disk "
                   "and keeps no copy of what it replaced, so no call puts "
                   "the earlier disk contents back; the session itself is "
                   "unchanged",
}

#: Why a lease action other than a fresh claim has no exact inverse.
_LEASE_NO_INVERSE = ("only a fresh lease claim has an exact inverse (its "
                     "release); a renewal or a release would need the earlier "
                     "expiry back, which no call restores")


def _none(reason: str) -> dict:
    return {"undo": None, "undoReason": reason}


@dataclass
class PreState:
    """What the session looked like before a mutating call ran."""

    loaded: bool
    generation: int | None = None
    readmittable: bool = False
    snapshot: dict | None = None
    lease_free: bool = False
    note: str | None = None


def capture(session, tool: str, arguments: dict) -> PreState | None:
    """The pre-call state a `tool` call's undo is derived from, or None when the
    tool is not a mutating verb this module answers for."""
    if tool in GENERATION_VERBS:
        return _capture_generation(session, tool)
    if tool == "revl_lease":
        return _capture_lease(session, arguments)
    return None


def _capture_generation(session, tool: str) -> PreState:
    if not session.loaded:
        return PreState(loaded=False)
    history = getattr(session, "_history", [])
    readmittable = bool(history) and history[-1].get("snapshot") is not None
    pre = PreState(loaded=True, generation=getattr(session, "_generation", None),
                   readmittable=readmittable)
    if tool == "revl_unload":
        # the only inverse of a teardown is a re-admission of what ran, so the
        # snapshot is taken before it is gone
        try:
            pre.snapshot = session.snapshot()
        except Exception as error:  # noqa: BLE001 - the reason is the answer
            pre.note = f"the running composition could not be snapshotted ({error})"
    return pre


def _capture_lease(session, arguments: dict) -> PreState:
    action = (arguments.get("action") or "claim").lower()
    component = arguments.get("component")
    book = getattr(session, "leases", None)
    free = (action == "claim" and bool(component) and book is not None
            and book.holder_of(component) is None)
    return PreState(loaded=session.loaded, lease_free=free,
                    note=None if free else _LEASE_NO_INVERSE)


def describe(session, tool: str, arguments: dict, pre: PreState | None) -> dict | None:
    """The `undo` fields for a successful `tool` call: `{"undo": {tool,
    arguments}}`, or `{"undo": None, "undoReason": ...}`. None when the tool
    is not mutating (the response carries no undo field at all)."""
    if tool in IRREVERSIBLE:
        return _none(IRREVERSIBLE[tool])
    if pre is None:
        return None
    if tool == "revl_lease":
        if not pre.lease_free:
            return _none(pre.note or _LEASE_NO_INVERSE)
        return {"undo": {"tool": "revl_lease",
                         "arguments": {"action": "release",
                                       "component": arguments.get("component")}}}
    return _describe_generation(session, pre)


def _describe_generation(session, pre: PreState) -> dict:
    if not pre.loaded:
        if not session.loaded:
            return _none("nothing was loaded before or after this call; "
                         "nothing changed")
        return {"undo": {"tool": "revl_unload", "arguments": {}}}
    if not session.loaded:
        if pre.snapshot is None:
            return _none(pre.note or "the composition that ran was not "
                         "re-admittable, so no snapshot could restore it")
        return {"undo": {"tool": "revl_restore",
                         "arguments": {"snapshot": pre.snapshot}}}
    if getattr(session, "_generation", None) == pre.generation:
        return _none("this call left the running generation unchanged; "
                     "nothing to undo")
    if not pre.readmittable:
        return _none(f"generation {pre.generation} was loaded without recorded "
                     "sources, so it cannot be re-admitted (docs/"
                     "generation-history.md)")
    return {"undo": {"tool": "revl_undo", "arguments": {"to": pre.generation}}}


@dataclass
class UndoStack:
    """The session's reversible changes, newest last, for `revl_step_back`
    with no arguments. An entry is the call that made the change and the undo
    that reverts it."""

    entries: list[dict] = field(default_factory=list)

    def record(self, tool: str, arguments: dict, undo: dict) -> None:
        self.entries.append({"tool": tool, "arguments": arguments, "undo": undo})

    def last(self) -> dict | None:
        return self.entries[-1] if self.entries else None

    def pop(self) -> dict | None:
        return self.entries.pop() if self.entries else None

    @property
    def depth(self) -> int:
        return len(self.entries)

    def clear(self) -> None:
        self.entries.clear()

"""Proposals: one speculative working copy per caller (issue #1696).

A speculative change (`revl_change` by default, `revl_edit {commit: false}`)
is applied to the caller's proposal and verified against the running
composition (admission, the lease and quarantine gates, optionally the
gauntlet), and then it stops: nothing swaps, nothing is written. Successive
speculative changes build on the same proposal. A commit re-verifies the
proposal against the running composition as it is NOW and swaps it in. A
discard drops it, and the running composition never saw it.

Proposals are keyed by the caller's operator identity (the lease holder), so
two callers of one HTTP server each have their own and neither sees the
other's until it is committed. A proposal records the generation it was built
on. If another commit moved the running composition since, committing the
stale proposal would replay a full working set over that commit and undo it
silently, so the commit is refused and the caller re-proposes.

See docs/design/1696-speculation.md.
"""

from __future__ import annotations

from . import edit as _edit
from . import leases as _leases


def _store(session) -> dict:
    store = getattr(session, "proposals", None)
    if store is None:
        store = {}
        session.proposals = store
    return store


def _key(session) -> str:
    return _leases.holder_identity(session)


def held(session) -> dict | None:
    """The calling operator's proposal, or None."""
    return _store(session).get(_key(session))


def base(session) -> dict | None:
    """The working set a speculative change starts from: the proposal, if any."""
    proposal = held(session)
    return proposal["vs"] if proposal is not None else None


def keep(session, result: dict, replacing) -> dict:
    """Hold the working set a speculative `admit` returned as the caller's
    proposal, and strip it from the result the caller sees."""
    vs = result.pop("_proposal", None)
    if vs is None:
        return result
    proposal = held(session)
    generation = (proposal["generation"] if proposal is not None
                  else getattr(session, "_generation", None))
    replaced = list(proposal["replacing"]) if proposal is not None else []
    replaced += [name for name in replacing if name not in replaced]
    _store(session)[_key(session)] = {"vs": vs, "generation": generation,
                                      "replacing": replaced}
    return {**result, "proposal": {"generation": generation,
                                   "replacing": replaced}}


def discard(session) -> bool:
    return _store(session).pop(_key(session), None) is not None


def stale(session) -> str | None:
    """Why the held proposal may not commit, or None."""
    proposal = held(session)
    now = getattr(session, "_generation", None)
    if proposal is not None and proposal["generation"] != now:
        return (f"the running composition moved since this proposal was built "
                f"(generation {proposal['generation']}, now {now}); committing it "
                "would undo that change. Discard it and propose again")
    return None


def commit(session, verify=None) -> dict:
    """Re-verify the caller's proposal against the running composition and swap
    it in."""
    proposal = held(session)
    if proposal is None:
        return {"ok": False, "committed": False, "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "session",
            "message": "nothing is proposed: make a change first (revl_change "
                       "without `commit`), then commit it"}]}
    why = stale(session)
    if why is not None:
        return {"ok": False, "swapped": False, "diagnostics": [{
            "severity": "error", "code": "REVL", "category": "session",
            "message": why}]}
    before = _edit.running_source(session)
    result = _edit.admit(session, proposal["vs"], before, [],
                         tuple(proposal["replacing"]), verify, commit=True)
    if result.get("swapped"):
        discard(session)
    return result


def touched_since(session, proposal_vs: dict) -> list[dict]:
    """What the proposal changes relative to the running composition."""
    return _edit._touched(_edit.running_source(session), proposal_vs)


__all__ = ["held", "base", "keep", "discard", "stale", "commit", "touched_since"]

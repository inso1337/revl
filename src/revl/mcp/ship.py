"""One intent, one call: the fused ship verb (roadmap item 50, the token
economy — docs/token-economy.md).

The audit in `bench/results/token-surface-audit.md` (finding #3) measures the
cost of the ship-a-component loop on the *protocol* side: an agent that has
generated one component walks

    revl_check  ->  revl_admit  ->  revl_plan  ->  revl_swap

to land it — three or four round-trips, each of which re-serialises the same
source (and `revl_admit` additionally round-trips the running manifest back
in). That is four schema+result exchanges, and 4x the source in tokens, to
express a single intent: *ship this*.

`revl_ship` fuses that predictable chain into one call. It runs the stages in
order and **early-exits on the first that fails** — a candidate that does not
compile is never admitted; one that is not admissible is never planned; nothing
is swapped unless every prior stage passed. The consolidated result carries one
stage list, so the agent sees exactly how far the candidate got and why it
stopped, from a single exchange.

Two token wins, both structural (no tokeniser in the runtime path):

  * **round-trips collapse** from up to four to one — `roundTrips.saved`
    reports the difference for the stages actually run.
  * **the running manifest is not re-sent.** Like `revl_plan`, the admit and
    plan stages default their `manifest` to the composition this server already
    holds (`session.ir`) when the agent does not pass one — so the agent never
    round-trips the running IR through its own context just to admit against it.
    `against` names what was used.

The module is deliberately free of `server` imports: it receives the primitive
stage handlers as callables, so it stays a pure orchestration that a test can
drive with fakes, and so the additions to `server.py` remain a thin wiring
handler plus one TOOLS entry (item 53 also touches that file this wave — the
surface here is a single new verb, not a rewrite).
"""

from __future__ import annotations

from typing import Callable

# The stages an agent would otherwise call one at a time, in ship order. The
# read-only rehearsal is check -> admit -> plan; `apply` extends it with the
# destructive swap so the whole chain is one call.
_READONLY_STAGES = ("check", "admit", "plan")


def _stage(name: str, ok: bool, result: dict, **extra) -> dict:
    entry = {"stage": name, "ok": bool(ok)}
    entry.update(extra)
    return entry


def _round_trips(stages_run: int) -> dict:
    """One fused call replaces `stages_run` separate ones. Saved is what the
    agent did not spend — never negative."""
    return {
        "fused": 1,
        "wouldHaveBeen": stages_run,
        "saved": max(0, stages_run - 1),
    }


def _stopped(stages: list, stopped_at: str, failing: dict, reason: str) -> dict:
    """Early-exit envelope: the later stages were never run (no wasted work),
    and the failing stage's own payload is merged up so the agent has the
    diagnostic/holes without a second look."""
    consolidated = {
        "ok": False,
        "shipped": False,
        "stoppedAt": stopped_at,
        "reason": reason,
        "stages": stages,
        "roundTrips": _round_trips(len(stages)),
    }
    # merge the failing stage's payload (diagnostics, holes, boundary, …) up to
    # the top level, but never let it clobber the envelope's own verdict keys.
    for key, value in failing.items():
        if key not in consolidated:
            consolidated[key] = value
    return consolidated


def ship(
    arguments: dict,
    *,
    check: Callable[[dict], dict],
    admit: Callable[[dict], dict],
    plan: Callable[[dict], dict],
    swap: Callable[[dict], dict] | None = None,
    session=None,
) -> dict:
    """Fuse check -> admit -> plan (-> swap, when `apply`) into one early-exit
    call.

    `check`, `admit`, `plan`, `swap` are the existing per-stage handlers; they
    are injected rather than imported so this stays pure. `session`, when given
    and loaded, supplies the default `manifest` for the admit/plan stages.
    """
    apply = bool(arguments.get("apply"))
    stages: list = []

    # -- stage 1: check — does the candidate compile at all? -----------------
    checked = check(arguments)
    holes = checked.get("holes") or []
    check_ok = bool(checked.get("ok"))
    stages.append(_stage("check", check_ok and not holes, checked,
                         holes=len(holes)))
    if not check_ok:
        return _stopped(stages, "check", checked,
                        "the candidate does not compile — fix the diagnostic "
                        "before admission")
    if holes:
        # a draft with open typed holes compiles but is refused at admission
        # (docs/holes.md). Stop here rather than spend an admit that will refuse
        # it — the holes (each with its fillSpec) are the agent's next work.
        return _stopped(stages, "check", checked,
                        "open typed holes — fill them (each carries a fillSpec) "
                        "before this can be admitted")

    # -- resolve the running composition once, for admit and plan ------------
    # The whole token point: default the manifest to what the server already
    # holds, so the agent does not re-send the running IR to admit against it.
    manifest = arguments.get("manifest")
    if manifest is None and session is not None and getattr(session, "loaded", False):
        manifest = session.ir
        against = "session"
    elif manifest is not None:
        against = "manifest"
    else:
        against = None  # nothing running — admit's own usage error will fire

    staged_args = dict(arguments)
    if manifest is not None:
        staged_args["manifest"] = manifest

    # -- stage 2: admit — does it link against the running composition? ------
    admitted = admit(staged_args)
    admit_ok = bool(admitted.get("ok")) and bool(admitted.get("admitted"))
    stages.append(_stage("admit", admit_ok, admitted, against=against))
    if not admit_ok:
        env = _stopped(stages, "admit", admitted,
                       "not admissible against the running composition — the "
                       "running system is untouched")
        env["against"] = against
        return env

    # -- stage 3: plan — the delta the swap would produce --------------------
    planned = plan(staged_args)
    # plan explains a rejected candidate rather than throwing; admit already
    # passed, so a plan that reports ok:false is a genuine stop, not an
    # exception. Treat a missing `ok` as success (plan payloads omit it on the
    # happy path and carry the delta directly).
    plan_ok = planned.get("ok", True) is not False
    stages.append(_stage("plan", plan_ok, planned))
    if not plan_ok:
        env = _stopped(stages, "plan", planned,
                       "the swap plan could not be produced — nothing was "
                       "applied")
        env["against"] = against
        return env

    # -- stage 4 (optional): swap — actually apply it ------------------------
    swapped_result = None
    if apply:
        if swap is None:
            raise ValueError("apply requested but no swap handler was provided")
        swapped_result = swap(arguments)
        swap_ok = bool(swapped_result.get("ok")) and bool(
            swapped_result.get("swapped"))
        stages.append(_stage("swap", swap_ok, swapped_result))
        if not swap_ok:
            env = _stopped(stages, "swap", swapped_result,
                           "admitted and planned, but the swap did not apply — "
                           "the running system is untouched")
            env["against"] = against
            return env

    # -- consolidated success ------------------------------------------------
    consolidated = {
        "ok": True,
        "shipped": bool(apply),  # true only when the swap actually landed it
        "stoppedAt": None,
        "against": against,
        "stages": stages,
        "roundTrips": _round_trips(len(stages)),
        "summary": {k: checked.get(k) for k in ("loadOrder", "components",
                                                "services", "irVersion")
                    if k in checked},
        "boundary": admitted.get("boundary") or checked.get("boundary"),
        "plan": {k: v for k, v in planned.items()
                 if k not in ("ok", "note")},
        "note": ("checked, admitted and planned in one call"
                 + (" and swapped in" if apply else
                    " — pass `apply: true` to swap it in without another round-trip")),
    }
    if apply and swapped_result is not None:
        consolidated["swap"] = {k: v for k, v in swapped_result.items()
                                if k not in ("ok", "note")}
    return consolidated

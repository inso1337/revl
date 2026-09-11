"""One intent, one call: the admission-gated cross-machine deploy verb (roadmap
item 476, issue #830).

The MCP surface hot-swaps a component into the LOCAL running composition
(`revl_swap` / `revl_ship` / `revl_edit`), and `revl deploy` reconfigures ACROSS
machines (`via = ssh`, #811) but is CLI-only. `revl_deploy` closes that gap: an
agent drives a cross-host reconfiguration through MCP with the SAME admission
and receipt machinery the CLI uses, so a far host re-hashes the bytes it runs
and the conductor verifies the signed admission + COMMIT receipts (item 127) —
never a second protocol.

Two modes, one verb (the `revl_ship` rehearsal shape):

  * `apply: false` (or absent) is a REHEARSAL: it runs the deploy-map admission
    gate (`deploy.admit_deploy_map`) and reports the plan, mutating NOTHING on
    either host. It is genuinely side-effect-free.
  * `apply: true` performs the cross-machine reconfiguration through the landed
    ssh leg (`deploy.deploy_ssh_map`) — staging the attested bundle under the
    pinned host key, driving the coordinated PREPARE/COMMIT/ABORT protocol, and
    returning the signed COMMIT receipt the conductor verifies (or the
    abort/unresolved outcome on failure). A rejected candidate leaves both hosts
    untouched; a remote the conductor cannot settle on ABORT is `unresolved`,
    never a false rollback.

The module is deliberately free of `server` and `approval` imports: it receives
the `admit` / `run` stage handlers (and the `authorize` approval gate) as
callables, so it stays a pure orchestration a test can drive with fakes, and so
`server.py` remains a thin wiring handler plus one TOOLS entry — the same split
`ship.py` uses.
"""

from __future__ import annotations

from typing import Callable

# The deploy-map verdict strings the report can carry (`deploy.run_deploy`'s
# wire vocabulary). Declared as literals rather than imported so this module
# stays import-light, and compared by value so a rename in `deploy` still
# surfaces — here as a verdict that reads as neither applied nor aborted nor
# unresolved, rather than as a silently-misjudged "applied".
_APPLIED = "applied"
_ABORTED_CLEAN = "aborted-clean"
_ABORTED_WITH_RESIDUE = "aborted-with-residue"
_UNRESOLVED = "unresolved"


def _stage(name: str, ok: bool, result: dict, **extra) -> dict:
    entry = {"stage": name, "ok": bool(ok)}
    entry.update(extra)
    return entry


def deploy(
    arguments: dict,
    *,
    admit: Callable[[dict], dict],
    run: Callable[[dict], dict],
    authorize: Callable[[dict], None] | None = None,
) -> dict:
    """Fuse deploy-map admission -> (apply) cross-machine reconfiguration into
    one early-exit call.

    `admit` runs the deploy-map admission gate and returns a payload with `ok`
    (plus `targets`/`refusals`/`boundaries` for the report). `run` drives the
    coordinated protocol and returns the :func:`deploy.run_deploy` report.
    `authorize`, when given, is called only on `apply: true`, AFTER admission and
    BEFORE `run`; it raises `ApprovalRequired` when the configured approval is
    missing, so a cross-machine reconfiguration is refused without the configured
    approval (the issue's Exit clause).
    """
    apply = bool(arguments.get("apply"))
    placement = arguments.get("placement")
    bundle = arguments.get("bundle")

    if placement is None:
        return {
            "ok": False,
            "admitted": False,
            "applied": False,
            "rehearsal": not apply,
            "stoppedAt": "usage",
            "reason": "provide `placement` — the deploy map (an item-56 placement "
                      "mapping with a `[processes.<p>.deploy]` table)",
            "stages": [],
        }

    stages: list = []
    admitted = admit(arguments)
    admit_ok = bool(admitted.get("ok"))
    stages.append(_stage("admit", admit_ok, admitted))
    if not admit_ok:
        env = {
            "ok": False,
            "admitted": False,
            "applied": False,
            "rehearsal": not apply,
            "stoppedAt": "admit",
            "reason": "the deploy map did not admit — no boundary was opened and "
                      "both hosts are untouched",
            "stages": stages,
        }
        for key, value in admitted.items():
            if key not in env:
                env[key] = value
        return env

    # -- rehearsal: the effect-free half ran; nothing is staged or spawned -----
    if not apply:
        return {
            "ok": True,
            "admitted": True,
            "applied": False,
            "rehearsal": True,
            "stoppedAt": None,
            "stages": stages,
            "targets": admitted.get("targets"),
            "boundaries": admitted.get("boundaries"),
            "plan": admitted.get("plan") or [],
            "note": "rehearsal — the deploy map admits but nothing was deployed "
                    "on either host; pass `apply: true` to perform the "
                    "cross-machine reconfiguration (admission-gated and "
                    "approval-gated)",
        }

    # -- apply: the destructive half ------------------------------------------
    if bundle is None:
        return {
            "ok": False,
            "admitted": True,
            "applied": False,
            "rehearsal": False,
            "stoppedAt": "run",
            "reason": "apply requested but no `bundle` was given — a cross-machine "
                      "deploy needs the attested bundle the far host re-hashes "
                      "and the conductor verifies against the signed receipts",
            "stages": stages,
        }

    if authorize is not None:
        authorize(arguments)  # raises ApprovalRequired unless already approved

    report = run(arguments)
    verdict = report.get("verdict")
    applied = verdict == _APPLIED
    aborted = verdict in (_ABORTED_CLEAN, _ABORTED_WITH_RESIDUE)
    unresolved = verdict == _UNRESOLVED
    return {
        "ok": applied,
        "admitted": True,
        "applied": applied,
        "aborted": aborted,
        "unresolved": unresolved,
        "rehearsal": False,
        "stoppedAt": None if applied else "run",
        "verdict": verdict,
        "phase": report.get("phase"),
        "protocol": report.get("protocol"),
        "reason": report.get("reason"),
        "refusedBy": report.get("refusedBy"),
        "refusals": report.get("refusals"),
        "participants": report.get("participants"),
        "commitLedger": report.get("commitLedger"),
        "abortOrder": report.get("abortOrder"),
        "failedAt": report.get("failedAt"),
        "residue": report.get("residue"),
        "proof": report.get("proof"),
        "stages": stages,
        "note": (
            "the conductor verified the signed admission and COMMIT receipts "
            "(receipt chain, item 127) and the far host re-hashed the bytes it "
            "ran" if applied else
            "the deploy did not fully apply — see `verdict`/`phase`/`residue`"),
    }

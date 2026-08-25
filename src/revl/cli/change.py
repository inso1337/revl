"""Per-command CLI handlers: plan / apply / rollback / recovery / quarantine.

Pure move — per-command CLI handlers, byte-identical behavior; see revl.__main__ for dispatch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .._paths import backends_root
from ..compiler import compile_files
from ..errors import RevlError


def _run_plan(args) -> int:
    """`revl plan` — what admitting these files would do (docs/plan.md).

    Exit status follows the gate, not the planner: 0 when the candidate is
    admissible, 1 when it is not. A plan is still printed either way, so a
    rejection tells you both why and what you were about to do.
    """
    from ..plan import plan as build_plan, render

    running = None
    if args.manifest:
        try:
            with open(args.manifest, encoding="utf-8") as handle:
                running = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: cannot read {args.manifest}: {error}", file=sys.stderr)
            return 1
        if not isinstance(running, dict):
            print(f"error: {args.manifest}: expected a compiled IR document (an object)",
                  file=sys.stderr)
            return 1

    result = build_plan(files=list(args.files), manifest=running,
                        replacing=tuple(args.replacing),
                        include_ir=bool(args.output))

    if args.output:
        # -o turns the plan into an executable artifact (docs/apply.md). Only an
        # admitted plan can be applied; a rejection is printed, not written.
        from ..apply import build_artifact, ApplyError  # noqa: PLC0415

        try:
            artifact = build_artifact(result, running)
        except ApplyError as error:
            print(render(result))
            print(f"\nerror: {error}", file=sys.stderr)
            return 1
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, indent=2)
        ops = len(artifact["operations"])
        print(f"wrote {args.output}: an applyable plan of {ops} operation(s) — "
              f"`revl apply {args.output}` (docs/apply.md)")
        return 0

    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0 if result["admissible"] else 1


def _run_canary(args) -> int:
    """`revl canary <baseline> --candidate <file> --slice <realm>` — progressive
    delivery for one slice (docs/verified-canary.md, roadmap item 59).

    Runs a successor generation on ONE designated realm while the baseline
    serves the rest, compares the two recorded worlds (a replay comparison,
    attributed to a code site), and proves the revert clean (survivors +
    residue). It decides; `revl swap` acts. Exit status: 0 when the candidate
    is admitted and the revert is clean (the other tenants provably untouched),
    1 when the candidate is refused or the revert would breach a sibling."""
    from ..compiler import compile_files  # noqa: PLC0415
    from ..mcp.canary import run_canary, render  # noqa: PLC0415

    try:
        running = compile_files(list(args.files))
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    report = run_canary(
        running,
        candidate_files=list(args.candidate),
        realm=args.slice,
        provider=args.provider,
        promote_to=args.promote_to,
        prove_residue=not getattr(args, "no_residue_proof", False),
    )
    print(json.dumps(report, indent=2) if args.json else render(report))
    if not report.get("ok"):
        return 1
    # a clean canary is an admitted candidate whose revert leaves every sibling
    # tenant untouched; a breached revert or a refused candidate is a failure.
    revert = report.get("revert") or {}
    breached = not revert.get("untouched", False)
    return 1 if breached else 0


def _run_apply(args) -> int:
    """`revl apply change.plan` — boot the plan's pre-state, then execute the
    plan against it: drift-refuse if the composition moved, verify each step
    against its prediction, and roll the applied prefix back on any failure
    (docs/apply.md). A one-shot: it tears the composition down afterwards and
    reports whether the change (or its rollback) left any residue."""
    from ..apply import validate_artifact, ApplyError
    from ..mcp.session import Session, SessionError

    try:
        with open(args.plan, encoding="utf-8") as handle:
            artifact = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: cannot read {args.plan}: {error}", file=sys.stderr)
        return 1
    try:
        validate_artifact(artifact)
    except ApplyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    running_ir = artifact.get("runningIR") or {}
    if args.against:
        # apply against a DIFFERENT current composition than the plan assumed —
        # the honest way to exercise drift refusal from the CLI.
        try:
            with open(args.against, encoding="utf-8") as handle:
                running_ir = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: cannot read {args.against}: {error}", file=sys.stderr)
            return 1

    session = Session()
    try:
        if running_ir.get("components"):
            session.load(running_ir)
        elif artifact["resultingIR"].get("components"):
            # cold start: nothing was running, so applying is just bringing the
            # resulting composition up.
            state = session.load(artifact["resultingIR"])
            report = {"applied": True, "coldStart": True,
                      "resulting": artifact["resulting"]["components"],
                      "state": state}
            _print_apply(report, args)
            unloaded = session.unload()
            print(f"torn down — no residue: {unloaded['noResidue']}")
            return 0
        report = session.apply(artifact)
    except SessionError as error:
        # a drift refusal lands here — the composition is untouched
        print(f"refused: {error}", file=sys.stderr)
        if session.loaded:
            session.unload()
        return 1

    _print_apply(report, args)
    unloaded = session.unload()
    print(f"\ntorn down — no residue: {unloaded['noResidue']}")
    return 0 if report["applied"] else 1


def _print_apply(report: dict, args) -> None:
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
        return
    if report["applied"]:
        print(f"applied: {len(report.get('steps') or [])} step(s) — resulting "
              f"composition: {', '.join(report.get('resulting') or []) or '(empty)'}")
        for step in report.get("steps") or []:
            print(f"  {step['op']:<8} {step['name']:<16} -> {step['state'] or 'gone'}")
        return
    print(f"FAILED at `{report.get('failedAt')}`: {report['reason']}")
    print(f"rolled back {len(report.get('rolledBack') or [])} step(s) "
          f"(LIFO, derived inverses):")
    for undo in report.get("rolledBack") or []:
        print(f"  {undo['undo']:<8} {undo['name']}")
    print(f"no residue: {report.get('noResidue')} "
          f"(registry {report['registry']['baseline']} -> "
          f"{report['registry']['afterRollback']})")


def _run_undo(args) -> int:
    """`revl undo history.json [--to N]` — operator undo for a running system
    (roadmap item 65, docs/generation-history.md).

    A history document (`revl.generation-history`, produced by the session's
    `history_document()`) is a list of generation snapshots. This replays them
    into a *fresh* session — load the first, swap the rest — to reach the same
    live generation history, then performs the undo through the gate. `--to`
    names a recorded generation; omit it to undo to N−1. The undo is itself an
    admitted, gated change: a target the current checker rejects is refused and
    the composition is left untouched. Prints the dossier, tears down."""
    from ..mcp import persist  # noqa: PLC0415
    from ..mcp.session import Session, SessionError  # noqa: PLC0415

    try:
        with open(args.history, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: cannot read {args.history}: {error}", file=sys.stderr)
        return 1
    if not isinstance(doc, dict) or doc.get("kind") != "revl.generation-history":
        print(f"error: {args.history}: expected a revl.generation-history document "
              f"(from the session's history export)", file=sys.stderr)
        return 1
    gens = doc.get("generations") or []
    if len(gens) < 2:
        print("error: the history has fewer than two generations — nothing to "
              "undo to", file=sys.stderr)
        return 1

    # translate a recorded generation number to the position it will hold in the
    # replayed session (which numbers its generations 1..k as it boots them).
    to_session = None
    if args.to is not None:
        idx = next((i for i, g in enumerate(gens)
                    if g.get("generation") == args.to), None)
        if idx is None:
            recorded = [g.get("generation") for g in gens]
            print(f"error: generation {args.to} is not in the history document "
                  f"(recorded: {recorded})", file=sys.stderr)
            return 1
        to_session = idx + 1

    session = Session()
    try:
        for i, gen in enumerate(gens):
            snap = gen.get("snapshot")
            if not snap or not snap.get("sources"):
                print(f"error: generation {gen.get('generation')} has no "
                      f"re-admittable snapshot — the history cannot be replayed",
                      file=sys.stderr)
                if session.loaded:
                    session.unload()
                return 1
            ir = persist._recompile(snap["sources"])
            origin = persist._origin_from(snap["sources"])
            config = (snap.get("meta") or {}).get("config")
            if i == 0:
                session.load(ir, config, origin=origin)
            else:
                session.swap(ir, origin=origin)
        result = session.undo(to_session)
    except (SessionError, RevlError) as error:
        print(f"refused: {error}", file=sys.stderr)
        if session.loaded:
            session.unload()
        return 1

    _print_undo(result, args)
    unloaded = session.unload()
    if not getattr(args, "json", False):
        print(f"\ntorn down — no residue: {unloaded['noResidue']}")
    return 0 if result.get("undone") else 1


def _print_undo(result: dict, args) -> None:
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return
    dossier = result.get("dossier") or {}
    crossings = dossier.get("unemittableCrossings") or {}
    if not result.get("undone"):
        print(f"REFUSED: {result.get('reason')}")
        print("the running composition is untouched — an undo is a gated change.")
    else:
        print(f"undone: generation {dossier.get('fromGeneration')} -> "
              f"{result.get('toGeneration')} (now running as generation "
              f"{result.get('generation')}), re-admitted through the gate.")
    unloads = dossier.get("unloads") or []
    print(f"\nunloads: {', '.join(unloads) or '—'}")
    dropped = (dossier.get("stateDropped") or {}).get("provisions") or []
    print(f"state dropped (provisions withdrawn): "
          f"{', '.join(p['key'] for p in dropped) or '—'}")
    given_up = crossings.get("givenUp") or []
    print(f"\ninterim boundary crossings that NO undo can un-emit "
          f"(compensation is not inversion — §6.1):")
    for token in crossings.get("crossings") or []:
        mark = "  ! " if token in set(given_up) else "  ~ "
        note = "  (given up going forward, already exercised)" \
            if token in set(given_up) else "  (target still reaches this)"
        print(f"{mark}{token}{note}")
    if not (crossings.get("crossings") or []):
        print("  (none — the interim generations crossed no boundary)")


def _run_recover(args) -> int:
    """`revl recover --wal FILE` — crash recovery over a write-ahead log
    (docs/crash-recovery.md). Reads the WAL, decides roll-forward vs roll-back,
    runs the reconstructible boundary inverses (roll-back) or resumes the
    persisted generation (roll-forward), and prints a checked verdict with a
    residue proof. Exit status follows the residue: 0 when clean, 1 when honest
    residue remains."""
    # the recovery module reads `replay.WriteAheadLog`, a backend module — put
    # backends/python on the path exactly as `run` does, but *without* needing a
    # cordis runtime (recovery works from the durable log, the process is dead).
    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from ..recovery import recover, render, RecoveryError  # noqa: PLC0415

    session = snapshot = None
    if getattr(args, "restore", None):
        try:
            with open(args.restore, encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            print(f"error: cannot read snapshot {args.restore}: {error}",
                  file=sys.stderr)
            return 1
        from ..mcp.session import Session  # noqa: PLC0415 — lazy: cordis only if resuming
        session = Session()

    try:
        report = recover(args.wal, session=session, snapshot=snapshot)
    except RecoveryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return 0 if report.get("residue", {}).get("clean") else 1


def _run_repair(args) -> int:
    """`revl repair <files> --component NAME ...` — the repair loop (item 62).

    Boots the composition into a session, then runs the unattended loop:
    regenerate/reuse -> gauntlet -> policy -> widening-ack -> hot-swap, bounded
    by a self-repair policy, and prints the incident dossier. Exit status: 0 when
    the fault was repaired (or planned clean with --plan), 2 when the loop paused
    for a human ack (a widening), 1 otherwise (ineligible / rejected / no
    candidate)."""
    # the session boots a real cordis runtime; put backends/python on the path
    # exactly as `run` does.
    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from ..mcp import repair as _repair  # noqa: PLC0415
    from ..mcp.session import Session, SessionError  # noqa: PLC0415

    try:
        ir = compile_files(args.files)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if ir.get("holes"):
        print("error: the composition has open typed holes; fill them before "
              "repair", file=sys.stderr)
        return 1

    session = Session()
    try:
        session.load(ir, record=not args.no_record)
    except SessionError as error:
        print(f"error: cannot boot the composition: {error}", file=sys.stderr)
        return 1

    if args.boundary_policy:
        from ..policy import load_policy, PolicyError  # noqa: PLC0415
        try:
            session.boundary_policy = load_policy(args.boundary_policy)
        except (OSError, PolicyError) as error:
            print(f"error: cannot read boundary policy: {error}", file=sys.stderr)
            return 1

    arguments: dict = {"component": args.component, "apply": not args.plan,
                       "accept": list(args.accept)}
    if args.trace:
        try:
            arguments["traceFile"] = args.trace
        except OSError as error:  # pragma: no cover
            print(f"error: cannot read trace: {error}", file=sys.stderr)
            return 1
    if args.predicate:
        arguments["predicate"] = args.predicate
    if args.candidate:
        try:
            arguments["candidate"] = {
                "source": "\n".join(Path(p).read_text(encoding="utf-8")
                                    for p in args.candidate)}
        except OSError as error:
            print(f"error: cannot read candidate: {error}", file=sys.stderr)
            return 1
    if args.self_repair_policy:
        try:
            arguments["selfRepairPolicy"] = Path(
                args.self_repair_policy).read_text(encoding="utf-8")
        except OSError as error:
            print(f"error: cannot read self-repair policy: {error}",
                  file=sys.stderr)
            return 1

    dossier = _repair.run_repair(session, arguments)

    if args.json:
        print(json.dumps(dossier, indent=2))
    else:
        print(_repair.render_incident(dossier))

    status = (dossier.get("incident") or {}).get("status")
    if status in (_repair.STATUS_REPAIRED, _repair.STATUS_PLANNED):
        return 0
    if status == _repair.STATUS_AWAITING_ACK:
        return 2
    return 1


def _run_quarantine(args) -> int:
    """`revl quarantine FILES` — the quarantine tier (item 45,
    docs/quarantine-tier.md). Grade an untrusted candidate with the gauntlet,
    then compile it to a standard wasm component and run its lifecycle + fault
    battery in wasmtime's component-model sandbox — where an escape is a trap.

    Prints the verdict (passed | trapped | rejected | deferred | unavailable)
    and, with --policy, the admission decision. Exit status: 0 when the
    candidate passed (or was deferred/unavailable — nothing to fail on), 1 when
    it was trapped or rejected, and (with --require-runtime) 3 when the substrate
    toolchain is absent so the candidate could not be proven."""
    from ..mcp import quarantine as _quarantine  # noqa: PLC0415
    from ..mcp.session import Session  # noqa: PLC0415

    session = Session()
    if getattr(args, "policy", None):
        from ..policy import load_policy, PolicyError  # noqa: PLC0415
        try:
            session.sandbox = load_policy(args.policy)
        except (OSError, PolicyError) as error:
            print(f"error: cannot read policy: {error}", file=sys.stderr)
            return 1

    arguments: dict = {"files": list(args.files)}
    if getattr(args, "service", None):
        arguments["service"] = args.service
    report = _quarantine.run(session, arguments)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_render_quarantine(report))

    verdict = report.get("verdict")
    if verdict in ("trapped", "rejected"):
        return 1
    if verdict == "unavailable" and getattr(args, "require_runtime", False):
        return 3
    return 0


def _render_quarantine(report: dict) -> str:
    """A compact human render of the quarantine report (mirrors the dossier
    verbs' text output; the full structure is under --json)."""
    verdict = report.get("verdict")
    glyph = {"passed": "PASS", "trapped": "TRAP", "rejected": "REJECT",
             "deferred": "DEFER", "unavailable": "SKIP"}.get(verdict, "?")
    lines = [f"quarantine: {glyph}  ({verdict})", f"  {report.get('note', '')}"]
    sub = report.get("substrate") or {}
    counts = sub.get("counts") or {}
    if sub.get("ran"):
        lines.append(f"  substrate: {sub.get('runtime')}")
        lines.append(f"    probes={counts.get('probes')} "
                     f"returned={counts.get('returned')} "
                     f"trapped={counts.get('trapped')}")
        for probe in sub.get("probes") or []:
            if probe.get("outcome") == "trapped":
                lines.append(f"    TRAP {probe['function']}("
                             f"{probe['input']!r}): {probe.get('trap')}")
    elif sub.get("reason"):
        lines.append(f"  substrate: not run — {sub.get('reason')}")
    admission = report.get("admission") or {}
    if admission.get("gated"):
        verb = "ADMIT" if admission.get("admit") else "REFUSE"
        if admission.get("bypass"):
            verb = "ADMIT (operator bypass)"
        lines.append(f"  admission: {verb} — {admission.get('note') or admission.get('message', '')}")
    return "\n".join(lines)

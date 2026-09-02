"""Per-command CLI handlers: plan / apply / rollback / recovery / estop / quarantine.

Pure move — per-command CLI handlers, byte-identical behavior; see revl.__main__ for dispatch.
"""

from __future__ import annotations

import json
import os
import sys
import time
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
    print("\ninterim boundary crossings that NO undo can un-emit "
          "(compensation is not inversion — §6.1):")
    for token in crossings.get("crossings") or []:
        mark = "  ! " if token in set(given_up) else "  ~ "
        note = "  (given up going forward, already exercised)" \
            if token in set(given_up) else "  (target still reaches this)"
        print(f"{mark}{token}{note}")
    if not (crossings.get("crossings") or []):
        print("  (none — the interim generations crossed no boundary)")


def _estop_latch_path(args) -> str | None:
    """The latch file `revl estop` acts on: `--latch`, else `<wal>.estop`.

    Deriving it from the WAL is not a convenience: the WAL is the durable
    rendezvous the reconciliation path already uses (`revl recover --wal`), so
    a halt and its reconciliation name the same session with one argument."""
    if getattr(args, "latch", None):
        return args.latch
    if getattr(args, "wal", None):
        return f"{args.wal}.estop"
    return None


def _run_estop(args) -> int:
    """`revl estop` — the operator's emergency halt (item 443,
    docs/design/443-estop.md).

    Every other stop revl has is cooperative: teardown replays inverses LIFO,
    faults route through residue records, withdrawal propagates to dependents.
    That is right for a composition fault and wrong for an operator emergency.
    This verb arms a latch that a running composition's crossing seams watch,
    so the halt costs one latch check rather than a whole two-phase unwind.

    What it does NOT do, and must not: run an inverse, run a compensation,
    flush a deferred emission, or write a discharge record. The stranded
    entries stay owed and their WAL descriptors stay on disk, which is exactly
    what lets `revl recover --wal FILE` reconcile afterwards — an E-Stop is
    deliberately shaped to look like a CRASH to the recovery path.

    Exit status follows the residue, as `revl recover` does: 0 when there is
    nothing outstanding, 1 when a halt is engaged and entries are owed. An
    E-Stop is never clean."""
    path = _estop_latch_path(args)
    if path is None:
        print("error: `revl estop` needs --latch FILE (or --wal FILE, which "
              "derives FILE.estop)", file=sys.stderr)
        return 2

    if getattr(args, "clear", False):
        try:
            os.unlink(path)
        except FileNotFoundError:
            existed = False
        except OSError as error:
            print(f"error: cannot clear latch {path}: {error}", file=sys.stderr)
            return 1
        else:
            existed = True
        report = {"cleared": existed, "latch": path, "resumed": False,
                  "note": "the latch is gone so a FRESH process may boot; the "
                          "halted instance stays dead and its stranded entries "
                          "stay owed (item 443)"}
        print(json.dumps(report, indent=2) if args.json
              else _render_estop_clear(report))
        return 0

    record = _read_estop_latch(path)

    if getattr(args, "report", False):
        if record is None:
            report = {"halted": False, "latch": path, "clean": True}
            print(json.dumps(report, indent=2) if args.json
                  else f"no E-Stop latch at {path} — nothing is halted")
            return 0
        outstanding = _estop_outstanding(record.get("wal")
                                         or getattr(args, "wal", None))
        report = {"halted": True, "latch": path, "clean": False, **record,
                  "outstanding": outstanding}
        print(json.dumps(report, indent=2) if args.json
              else _render_estop(report))
        return 1

    if record is not None:
        # Idempotent, like `runtime.estop`: hitting the button twice is not two
        # halts, and the SECOND press must not overwrite the first one's reason.
        report = {"halted": True, "latch": path, "clean": False,
                  "alreadyHalted": True, **record}
        print(json.dumps(report, indent=2) if args.json else _render_estop(report))
        return 1

    record = {
        "halted": True,
        "verdict": "halted",
        "reason": args.reason or "operator halt",
        "operator": args.operator or "unknown",
        "at": time.time(),
        "wal": getattr(args, "wal", None),
        "resumable": False,
        "reconcile": (f"revl recover --wal {args.wal}" if getattr(args, "wal", None)
                      else "revl recover --wal <file>"),
    }
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
    except OSError as error:
        print(f"error: cannot arm latch {path}: {error}", file=sys.stderr)
        return 1

    report = {**record, "latch": path, "clean": False,
              "outstanding": _estop_outstanding(getattr(args, "wal", None))}
    print(json.dumps(report, indent=2) if args.json else _render_estop(report))
    return 1


def _read_estop_latch(path: str) -> dict | None:
    """The halt an operator armed at `path`, or None when the latch is absent.

    A latch that exists but does not parse still reads as HALTED. Failing open
    on a malformed emergency stop is the one failure mode this feature exists
    to prevent, and it is the same rule the runtime seam applies."""
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except (ValueError, TypeError):
        return {"halted": True, "reason": "operator halt (unreadable latch)",
                "operator": "unknown"}
    return record if isinstance(record, dict) else {
        "halted": True, "reason": "operator halt (unreadable latch)",
        "operator": "unknown"}


def _estop_outstanding(wal_path: str | None) -> dict:
    """What the halted session still OWES, read off its WAL.

    Every `transactional` inverse and every `compensation` was WAL-logged as a
    named-call discharge descriptor at REGISTRATION (docs/design/teardown-
    contract.md, "WAL descriptor"), and a clean commit writes a `discharge`
    record naming the seqs it settled. An E-Stop writes neither — it strands —
    so the descriptors with no discharge behind them are exactly the entries
    still owed, and exactly what `revl recover` would replay. Counting them
    here re-derives the inventory from the durable log rather than trusting the
    dead process's memory."""
    if not wal_path:
        return {"known": False,
                "note": "no WAL was named, so the outstanding entries cannot be "
                        "read off disk — pass --wal FILE, or run `revl recover "
                        "--wal FILE` against the session's log"}
    from ..wal import WALIntegrityError, read_wal  # noqa: PLC0415
    try:
        wal = read_wal(wal_path)
    except (OSError, WALIntegrityError) as error:
        return {"known": False, "note": f"cannot read WAL {wal_path}: {error}"}
    discharged: set = set()
    descriptors: list[dict] = []
    for record in wal.get("records") or []:
        kind = record.get("record")
        if kind == "discharge":
            discharged.update(record.get("discharged") or [])
        elif kind == "discharge-descriptor":
            descriptors.append(record)
    owed = [d for d in descriptors if d.get("seq") not in discharged]
    return {
        "known": True,
        "wal": wal_path,
        "entries": [{"seq": d.get("seq"), "entry": d.get("entry"),
                     "receiver": (d.get("call") or {}).get("receiver"),
                     "method": (d.get("call") or {}).get("method"),
                     "idempotency": d.get("idempotency")}
                    for d in owed],
        "count": len(owed),
    }


def _render_estop(report: dict) -> str:
    lines = ["E-STOP ENGAGED" + ("  (already armed)" if report.get("alreadyHalted")
                                 else "")]
    lines.append(f"  latch     {report.get('latch')}")
    lines.append(f"  reason    {report.get('reason')}")
    lines.append(f"  operator  {report.get('operator')}")
    lines.append("")
    lines.append("  What this guarantees: no NEW boundary crossing is dispatched.")
    lines.append("  What it does NOT: nothing was unwound. No inverse ran, no")
    lines.append("  compensation ran, nothing was discharged. Every registered")
    lines.append("  entry is STRANDED — still owed — and every acquired handle")
    lines.append("  is still held. That is the trade the button makes.")
    outstanding = report.get("outstanding") or {}
    lines.append("")
    if outstanding.get("known"):
        lines.append(f"  outstanding ({outstanding['count']}):")
        for entry in outstanding["entries"] or []:
            key = entry.get("idempotency")
            lines.append(
                f"    seq {entry.get('seq')}  {entry.get('entry')}  "
                f"{entry.get('receiver')}.{entry.get('method')}"
                + (f"  [idempotency {key}]" if key else ""))
        if not outstanding["entries"]:
            lines.append("    (none on the WAL — nothing durable was registered)")
    else:
        lines.append(f"  outstanding: {outstanding.get('note', 'unknown')}")
    lines.append("")
    lines.append("  The instance is DEAD; there is no resume (item 443).")
    lines.append(f"  Reconcile with: {report.get('reconcile', 'revl recover --wal <file>')}")
    return "\n".join(lines)


def _render_estop_clear(report: dict) -> str:
    head = ("latch cleared" if report.get("cleared")
            else "no latch was armed")
    return (f"{head}: {report.get('latch')}\n"
            "  This is NOT a resume. The halted instance stays dead and its\n"
            "  stranded entries stay owed until `revl recover` reconciles them.")


def _run_recover(args) -> int:
    """`revl recover --wal FILE` — crash recovery over a write-ahead log
    (docs/crash-recovery.md). Reads the WAL, decides roll-forward vs roll-back,
    runs the reconstructible boundary inverses (roll-back) or resumes the
    persisted generation (roll-forward), and prints a checked verdict with a
    residue proof. Exit status follows the residue: 0 when clean, 1 when honest
    residue remains."""
    # The recovery core reads the WAL through the tier-agnostic `revl.wal`
    # reader (item 322), so recover itself needs NO backend — it works from the
    # durable log alone, for a py OR a non-py (go/rust/java/wasm) tier's WAL.
    # backends/python is still put on the path for the `--restore` roll-forward
    # path, which re-admits the persisted generation through a real cordis
    # runtime; the core roll-back/roll-forward decision never touches it.
    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from ..recovery import recover, render, RecoveryError  # noqa: PLC0415

    # item 440 §(b): the re-issue seam's operator knob rides the SAME `--policy`
    # file the roll-forward path already re-establishes, and is read whether or
    # not `--restore` was given (the seam acts on the owed-emission report, which
    # has nothing to do with resuming a snapshot). No policy → `None` → the seam
    # is off and recover auto-fires nothing, exactly as before this item.
    reissue = None
    if getattr(args, "policy", None):
        from ..policy import PolicyError, load_policy  # noqa: PLC0415
        try:
            reissue = load_policy(args.policy).reissue_strength()
        except (OSError, PolicyError) as error:
            print(f"error: cannot load policy {args.policy}: {error}",
                  file=sys.stderr)
            return 1

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
        # re-establish the operator-flag approval posture the snapshot was taken
        # under (item 246). Without it, `persist.restore` refuses a policy-recorded
        # snapshot rather than boot the recovered generation ungated; with it, the
        # activation gate re-arms and a class-(c) crossing re-prompts on resume
        # exactly as on first boot.
        if getattr(args, "policy", None):
            from ..policy import PolicyError, load_policy  # noqa: PLC0415
            try:
                session.sandbox = load_policy(args.policy)
            except (OSError, PolicyError) as error:
                print(f"error: cannot load policy {args.policy}: {error}",
                      file=sys.stderr)
                return 1
        if getattr(args, "approval_policy", None):
            session.approval_policy = args.approval_policy

    try:
        report = recover(args.wal, session=session, snapshot=snapshot,
                         reissue=reissue)
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

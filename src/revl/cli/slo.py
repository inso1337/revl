"""`revl slo` — the observed half of the composition SLO contract (item 473).

Three acts, one verb, because they read the same two documents (the
composition's declared contract and a receipt) and separating them into three
verbs would make an operator remember which one takes which:

  * MEASURE (the default) — read a recorded trace against the declared
    contract, take the declared `on breach` response, and sign a receipt bound
    to the generation;
  * `--verify` — check a receipt, fail closed;
  * `--gate` — refuse the next rollout when the receipt of the generation it
    replaces breached an objective the candidate still declares.

Every decision lives in `revl.slo`; this module is argument handling, file I/O
and exit status, and nothing else, so the contract is testable without a
process.
"""

from __future__ import annotations

import json
import sys

from ..errors import RevlError

#: The exit status of a refusal. Distinct from 1 (a breach was measured) so a
#: script can tell "the run missed its objectives" from "this rollout was not
#: allowed to start", which are different operator situations.
GATE_REFUSED = 2


def _read_json(path: str, what: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as error:
        raise RevlError(path, 0, f"cannot read {what}: {error}") from error
    except json.JSONDecodeError as error:
        raise RevlError(path, 0, f"{what} is not JSON: {error}") from error


def _events(document) -> list:
    """The event list of a recorded trace. `revl run --trace` writes the
    document with its events under `events`; a bare list is accepted too, so a
    trace sliced by hand still measures."""
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("events", "trace", "steps"):
            value = document.get(key)
            if isinstance(value, list):
                return value
    return []


def _contract_ir(args) -> dict:
    from ..composition import resolve_file  # noqa: PLC0415 — lazy

    if not args.composition:
        raise RevlError("", 0, "`revl slo` needs --composition FILE: the "
                               "contract is a composition-level declaration")
    return resolve_file(args.composition, args.root).to_ir()


def _run_slo(args) -> int:
    """`revl slo`. Exit status: 0 clean, 1 a measured breach or a refused
    verification, 2 a refused rollout, 1 for a malformed input."""
    from .. import slo  # noqa: PLC0415 — lazy

    try:
        if args.verify:
            return _verify(args, slo)
        if args.gate:
            return _gate(args, slo)
        return _measure(args, slo)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _verify(args, slo) -> int:
    if not args.receipt:
        raise RevlError("", 0, "`revl slo --verify` needs --receipt FILE")
    receipt = _read_json(args.receipt, "the receipt")
    key = slo.resolve_key(args.key)
    if not key:
        raise RevlError(args.receipt, 0,
                        "no signing key: pass --key PATH, or set "
                        f"{slo.KEY_FILE_ENV} or {slo.KEY_ENV}. A receipt "
                        "checked against no key is not a checked receipt")
    ok, reason = slo.verify_receipt(receipt, key)
    report = {"verified": ok, "reason": reason, "receipt": args.receipt}
    if args.json:
        print(json.dumps(report, indent=2))
    elif ok:
        print(slo.render_receipt(receipt))
        print("\n  VERIFIED")
    else:
        print(f"REFUSED: {reason}", file=sys.stderr)
    return 0 if ok else 1


def _gate(args, slo) -> int:
    ir = _contract_ir(args)
    receipt = (_read_json(args.receipt, "the predecessor receipt")
               if args.receipt else None)
    report = slo.gate_rollout(ir=ir, receipt=receipt,
                              key=slo.resolve_key(args.key))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(slo.render_gate(report))
    return 0 if report["admitted"] else GATE_REFUSED


def _measure(args, slo) -> int:
    ir = _contract_ir(args)
    contract = slo.contract_from_ir(ir)
    events = _events(_read_json(args.trace, "the trace")) if args.trace else []
    # The PER-CALL latency population, when the run left a WAL: item 250 Slice
    # 3a writes one `model-decision` record per model completion, at the
    # crossing. That is the run's own sample set, where the trace's `emit` hops
    # are the crossings a step-back walk visited; `slo.observations` prefers the
    # former and names which it used, inside the signed receipt.
    decisions = slo.decisions_from_wal(args.wal)
    monitor = slo.Monitor(
        contract, composition=args.composition, generation=args.generation,
        latch=args.latch, wal=args.wal, key=slo.resolve_key(args.key),
        signer=args.signer, events=events, decisions=decisions)
    verdict = monitor.seal() if args.seal else monitor.observe()
    if verdict is None:
        # Inert, and it says so: a composition with no `slo` block declares no
        # objective, so there is nothing to measure and nothing is written.
        report = {"declared": False, "composition": args.composition,
                  "note": "the composition declares no `slo` block"}
        print(json.dumps(report, indent=2) if args.json
              else f"{args.composition}: no `slo` contract is declared")
        return 0
    if monitor.receipt is not None and args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(monitor.receipt, handle, indent=2)
                handle.write("\n")
        except OSError as error:
            raise RevlError(args.out, 0,
                            f"cannot write the receipt: {error}") from error
    if args.json:
        out = {k: v for k, v in verdict.items() if k != "body"}
        out["receipt"] = monitor.receipt
        print(json.dumps(out, indent=2))
    else:
        print(slo.render(verdict))
        if monitor.receipt is None and verdict["breached"]:
            print("  (unsigned: no key, so this measurement carries no "
                  "receipt — pass --key PATH)")
    return 1 if verdict["breached"] else 0

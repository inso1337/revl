"""`revl receipts`: export and verify signed action-receipt chains.

Design note: `docs/design/570-action-receipt-chain.md`.

Two verbs, and they point in opposite directions:

* ``revl receipts export`` signs one receipt per consequential action and
  writes the chained document. The actions come from a JSON document
  (``--actions``) or from a write-ahead log a run left (``--wal``, read with
  the same rules the live writer follows, `revl.action_receipt_writer`). The key is resolved
  from ``--key``, ``REVL_RECEIPT_KEY_FILE`` or ``REVL_RECEIPT_KEY``; a missing
  key is an error and nothing is printed, because an unsigned receipt would
  read as a signed one.
* ``revl receipts verify`` takes a chain somebody presented and checks it. It
  is FAIL CLOSED: a chain this process cannot check is refused with a reason
  and exit 1, never reported valid. ``--key`` repeats so a chain that spans a
  key rotation can be checked against every key it names.
"""

from __future__ import annotations

import json
import sys

from .. import action_receipt
from ..errors import RevlError


def _load(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _actions_document(args):
    """Read the ``--actions`` input, which is either a bare list of actions or
    an object carrying ``actions`` and an optional ``session_verdict``."""
    # `json.load` raises a plain `ValueError` for an integer above Python's
    # 4300-digit conversion limit and a `UnicodeDecodeError` for non-UTF-8
    # input. Both are a refusal, not a traceback, because the document is
    # untrusted input.
    try:
        document = _load(args.actions)
    except (OSError, ValueError) as error:
        print(f"error: cannot read the actions document: {error}", file=sys.stderr)
        return None
    default_verdict = args.session_verdict or "committed"
    if isinstance(document, list):
        return {"actions": document, "session_verdict": default_verdict}
    if isinstance(document, dict):
        actions = document.get("actions")
        if not isinstance(actions, list):
            print("error: the actions document has no `actions` list",
                  file=sys.stderr)
            return None
        return {"actions": actions,
                "session_verdict": document.get("session_verdict")
                or default_verdict}
    print("error: the actions document is neither a list nor an object",
          file=sys.stderr)
    return None


def _wal_recorder(args):
    """The recorder for ``--wal``, or None after printing why not."""
    from .. import action_receipt_writer  # noqa: PLC0415 - only this path needs it
    from ..wal import WALIntegrityError, read_wal  # noqa: PLC0415

    if not args.worker:
        print("error: `revl receipts export --wal` needs --worker: the log "
              "holds no identity to attribute the actions to", file=sys.stderr)
        return None
    try:
        loaded = read_wal(args.wal)
    except (OSError, ValueError, WALIntegrityError) as error:
        print(f"error: cannot read the write-ahead log: {error}", file=sys.stderr)
        return None
    try:
        return action_receipt_writer.recorder_from_wal(
            loaded, worker=args.worker, task=args.task or args.session,
            policy=args.policy_digest)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return None


def _export(args) -> int:
    recorder = None
    document = None
    if args.wal:
        recorder = _wal_recorder(args)
        if recorder is None:
            return 1
    else:
        document = _actions_document(args)
        if document is None:
            return 1
    try:
        keys = action_receipt.resolve_keys(args.key)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if len(keys) != 1:
        print("error: export signs with exactly one key; repeat --key only "
              "when verifying a chain that spans a rotation", file=sys.stderr)
        return 1
    try:
        if recorder is not None:
            chain = recorder.export(
                keys[0], session=args.session,
                session_verdict=args.session_verdict, salt=args.salt)
        else:
            chain = action_receipt.export_chain(
                keys[0], session=args.session, actions=document["actions"],
                session_verdict=document["session_verdict"], salt=args.salt)
    except (RevlError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if recorder is not None and recorder.unsettled:
        print(f"note: {recorder.unsettled} deferred emission(s) never settled "
              "in this log: no receipt, they did not cross or no record says "
              "they did", file=sys.stderr)
    text = json.dumps(chain, indent=2, sort_keys=True) + "\n"
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as error:
            print(f"error: cannot write the chain: {error}", file=sys.stderr)
            return 1
    else:
        sys.stdout.write(text)
    return 0


def _verify(args) -> int:
    # `json.load` raises a plain `ValueError` for an integer above Python's
    # 4300-digit conversion limit and a `UnicodeDecodeError` for non-UTF-8
    # input. Both are a refusal, not a traceback, because the chain is
    # untrusted input.
    try:
        document = _load(args.file)
    except (OSError, ValueError) as error:
        print(f"error: cannot read the chain: {error}", file=sys.stderr)
        return 1
    # A key that cannot be resolved is not "no opinion": the chain was
    # PRESENTED, so the answer is a refusal with the reason, not a pass.
    try:
        keys = action_receipt.resolve_keys(args.key)
    except RevlError as error:
        verdict = action_receipt.Verdict(
            False, "key-unknown", f"no key to verify against: {error}")
    else:
        verdict = action_receipt.verify_chain(document, keys)
    if args.json:
        print(json.dumps({"ok": verdict.ok, "link": verdict.link,
                          "reason": verdict.reason,
                          "kind": document.get("kind")
                          if isinstance(document, dict)
                          and isinstance(document.get("kind"), str) else None},
                         indent=2))
    else:
        print(action_receipt.render_verify(
            verdict, document if isinstance(document, dict) else {}))
    if not verdict.ok:
        print(f"error: {verdict.reason}", file=sys.stderr)
        return 1
    return 0


def _run_receipts(args) -> int:
    """`revl receipts`: export a receipt chain, or check one."""
    if args.receipts_command == "export":
        return _export(args)
    if args.receipts_command == "verify":
        return _verify(args)
    print("error: `revl receipts` needs a subcommand (export or verify)",
          file=sys.stderr)
    return 2

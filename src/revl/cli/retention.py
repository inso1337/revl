"""`revl retention-receipt` — issue and verify a retention erasure receipt
(roadmap item 472, issue #824).

The item's exit criterion has two clauses. The first — a `Retained[T, P]` value
past P's deadline is refused at a persistence sink — is the checker's
(`revl.taint`, `G-RETAIN`). The second — an erasure request emits a signed
receipt naming each in-scope replica and derivative — is `revl.retention`'s,
and until this verb there was no way to MAKE an erasure request: the receipt
could be signed from Python and by nothing an operator runs.

WHY THIS VERB DOES NOT COMPILE THE COMPOSITION. The retention refusal is a
property of admission, so past a policy's deadline a composition that persists
a value under it NO LONGER COMPILES. That is exactly when an erasure request is
made. A receipt surface gated on a successful compile would be unusable in the
only case it exists for, so the policies are read with
`retention.declared_policies`, which parses the declaration closure and
validates it through the same `policy_from_decl` the checker uses.

THE TWO DIRECTIONS THIS VERB FAILS IN, said plainly because they are opposite:

  * ISSUING. A receipt that cannot be signed is never printed. A key no route
    resolves, a requester the policy does not authorise, a derivative class
    outside the closed vocabulary, a policy name nothing declares, a
    composition that declares no policy at all: each is a message on stderr and
    exit 1 with NO document on stdout, because a document printed without a
    signature would be read as one that has it.
  * VERIFYING. A receipt that IS presented and does not verify is refused —
    exit 1, naming the one reason. That includes the case where no key can be
    resolved: a presented receipt is never accepted unchecked, which is the
    fail-closed direction. An ABSENT receipt is a different case entirely and
    is not a refusal here at all: it is the issue path above.
"""

from __future__ import annotations

import json
import sys

from ..errors import RevlError


def _spec_residence(text: str) -> tuple:
    """Split a trailing `@residence` off a spec. `@` never appears in a
    crossing token (`host:<component>:<extern>`), so the split is unambiguous;
    a spec with no `@` states no residence, and a replica that states none can
    never be a `residence-mismatch` finding."""
    if "@" in text:
        body, residence = text.rsplit("@", 1)
        return body, (residence or None)
    return text, None


def _replicas_from_flags(specs) -> list:
    from .. import retention  # noqa: PLC0415 — lazy, the CLI stays import-light

    out = []
    for spec in specs or ():
        token, residence = _spec_residence(str(spec))
        if not token:
            raise RevlError(
                "<retention-receipt>", 0,
                f"--replica {spec!r} names no replica",
                hint="write `--replica TOKEN[@RESIDENCE]`, for example "
                     "`--replica host:Store:db_put@eu`")
        out.append(retention.Replica(token=token, residence=residence))
    return out


def _derivatives_from_flags(specs) -> list:
    from .. import retention  # noqa: PLC0415

    out = []
    for spec in specs or ():
        body, residence = _spec_residence(str(spec))
        if "=" not in body:
            known = ", ".join(retention.DERIVATIVE_CLASSES)
            raise RevlError(
                "<retention-receipt>", 0,
                f"--derivative {spec!r} does not name a class",
                hint=f"write `--derivative NAME=CLASS[@RESIDENCE]`, where the "
                     f"class is one of: {known}")
        name, kind = body.rsplit("=", 1)
        out.append(retention.Derivative(name=name, kind=kind,
                                        residence=residence))
    return out


_REPLICA_MEMBERS = ("token", "residence", "kind")
_DERIVATIVE_MEMBERS = ("name", "kind", "token", "residence")


def _inventory(path: str) -> tuple:
    """An operator's enumeration, read from a JSON document.

    A command line cannot carry the hundreds of rows a real erasure request
    covers, and a receipt that names only the rows that fit on one is a receipt
    that under-reports. The document is
    `{"replicas": [...], "derivatives": [...]}`; an unknown member is refused BY
    NAME rather than ignored, because a row member this reader silently drops
    is a fact the signature would then not cover."""
    from .. import retention  # noqa: PLC0415

    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as error:
        raise RevlError(path, 0, f"cannot read the inventory: {error}") from error
    except json.JSONDecodeError as error:
        raise RevlError(path, error.lineno,
                        f"the inventory is not JSON: {error.msg}") from error
    if not isinstance(document, dict):
        raise RevlError(path, 0, "the inventory is not a document",
                        hint='write `{"replicas": [...], "derivatives": [...]}`')
    unknown = sorted(set(document) - {"replicas", "derivatives"})
    if unknown:
        raise RevlError(
            path, 0,
            f"the inventory has unknown member(s): {', '.join(unknown)}",
            hint="the document carries `replicas` and `derivatives` and "
                 "nothing else; a member this reader dropped would be a fact "
                 "the signature does not cover")

    def rows(key, members):
        value = document.get(key) or []
        if not isinstance(value, list):
            raise RevlError(path, 0, f"the inventory's `{key}` is not a list")
        for index, row in enumerate(value):
            if not isinstance(row, dict):
                raise RevlError(path, 0,
                                f"{key}[{index}] is not a row document")
            extra = sorted(set(row) - set(members))
            if extra:
                raise RevlError(
                    path, 0,
                    f"{key}[{index}] has unknown member(s): "
                    f"{', '.join(extra)}",
                    hint=f"a {key[:-1]} row carries {', '.join(members)}")
            yield row

    replicas = []
    for row in rows("replicas", _REPLICA_MEMBERS):
        if not row.get("token"):
            raise RevlError(path, 0, "a replica row names no `token`")
        replicas.append(retention.Replica(
            token=str(row["token"]),
            residence=row.get("residence"),
            kind=str(row.get("kind") or "boundary")))
    derivatives = []
    for row in rows("derivatives", _DERIVATIVE_MEMBERS):
        if not row.get("name") or not row.get("kind"):
            raise RevlError(path, 0,
                            "a derivative row needs both `name` and `kind`")
        derivatives.append(retention.Derivative(
            name=str(row["name"]), kind=str(row["kind"]),
            token=row.get("token"), residence=row.get("residence")))
    return replicas, derivatives


def _verify(args) -> int:
    """`revl retention-receipt --verify FILE`. Fail closed: a presented receipt
    that this process cannot check is REFUSED, never reported valid."""
    from .. import retention  # noqa: PLC0415

    path = args.verify
    try:
        with open(path, "r", encoding="utf-8") as handle:
            receipt = json.load(handle)
    except OSError as error:
        print(f"error: cannot read the receipt: {error}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as error:
        print(f"error: the receipt is not JSON: {error.msg}", file=sys.stderr)
        return 1
    # A key that cannot be resolved is not "no opinion": the receipt was
    # PRESENTED, so the answer is a refusal with the reason, not a pass.
    try:
        key = retention.resolve_key(getattr(args, "receipt_key", None))
    except RevlError as error:
        ok, reason = False, f"no key to verify against: {error}"
    else:
        ok, reason = retention.verify_receipt(receipt, key)
    if args.json:
        print(json.dumps({"ok": ok, "reason": reason,
                          "kind": receipt.get("kind")
                          if isinstance(receipt, dict) else None}, indent=2))
    else:
        print(retention.render_verify(ok, reason,
                                      receipt if isinstance(receipt, dict)
                                      else {}))
    if not ok:
        print(f"error: {reason}", file=sys.stderr)
    return 0 if ok else 1


def _issue(args) -> int:
    from .. import retention  # noqa: PLC0415
    from .. import attest  # noqa: PLC0415 — for its one non-RevlError refusal

    if not args.files:
        print("error: `revl retention-receipt` needs the source files that "
              "declare the policy (or `--verify FILE` to check one)",
              file=sys.stderr)
        return 1
    if not args.policy or not args.requester:
        print("error: `revl retention-receipt` needs --policy NAME and "
              "--requester WHO", file=sys.stderr)
        return 1
    try:
        declared = retention.declared_policies(args.files)
    except RevlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not declared:
        # Signing an enumeration under a policy nobody wrote would put a
        # signature on a retention guarantee the source does not make.
        print("error: these sources declare no `retention` policy, so there is "
              "no policy to issue an erasure receipt under", file=sys.stderr)
        return 1
    policy = declared.get(args.policy)
    if policy is None:
        print(f"error: no `retention {args.policy}` here; declared: "
              f"{', '.join(sorted(declared))}", file=sys.stderr)
        return 1

    try:
        replicas, derivatives = ([], [])
        if getattr(args, "inventory", None):
            replicas, derivatives = _inventory(args.inventory)
        replicas = replicas + _replicas_from_flags(args.replica)
        derivatives = derivatives + _derivatives_from_flags(args.derivative)
        issued = None
        if getattr(args, "issued_at", None):
            issued = retention.parse_instant(
                args.issued_at, what="`--issued-at`",
                filename="<retention-receipt>", line=0)
        # The key is resolved BEFORE the body is built, so a run that cannot
        # sign fails before it has produced anything that looks like a receipt.
        key = retention.resolve_key(getattr(args, "receipt_key", None))
        receipt = retention.make_receipt(
            policy, args.requester, replicas, derivatives, key,
            now=issued, signer=getattr(args, "signer", None))
    except (RevlError, attest.NotCanonicalizable) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(receipt, indent=2))
    else:
        print(retention.render_receipt(receipt))
    return 0


def _run_retention_receipt(args) -> int:
    """`revl retention-receipt` — the erasure request, and the check on one."""
    if getattr(args, "verify", None):
        return _verify(args)
    return _issue(args)

"""Distributability analysis for `revl audit` (docs/interop-bridge.md §4).

The interop bridge lets one composition span host processes: a service can be
provided in one process and required in another, wired by generated
proxy/stub over a transport. Not every service can cross a process boundary
cleanly, though, and this module is the checked verdict that says which can:

- **transport-safe**: every operation is `async fn` (so the caller already
  treats it as an asynchronous contract, per Waldo et al.'s four leaks) and no
  parameter or return mentions a *resource type*. Such a service can be split
  across a seam with only value copies crossing the wire.
- **address-space-bound**: some operation is a bare synchronous method
  (`fn`/`emission fn`, chatty and latency-bound) or a signature mentions a
  resource type. It still works remotely, but the audit names the cost.

A *resource type* is a value returned by an `extern acquire` (a `Socket`, a
pool: a handle whose lifetime is tied to the providing fiber, so it crosses
by proxy, not by copy), OR any record/ADT that carries one — transitively: a
`type Session = { conn: Sock, .. }` is itself resource-typed, because copying
it copies a dead handle. Everything else is a value type and copies cleanly
(`_resource_taint` computes the closure).

This adds no grammar: `async`/`emission` are read off the service methods and
the resource set off `extern acquire` returns, both already in the IR.
"""

from __future__ import annotations

import re


def _resource_types(ir: dict) -> set[str]:
    """Type names returned by an `extern acquire`: the handles, not values."""
    return {
        ext["returns"]
        for ext in ir.get("externs") or []
        if ext.get("class") == "acquire" and ext.get("returns")
    }


def _resource_in(type_str: str | None, resources: set[str]) -> str | None:
    """The first resource type named anywhere in `type_str` (handles nesting
    like `Opt[Socket]` / `List[Socket]`), or None."""
    if not type_str:
        return None
    for resource in resources:
        if re.search(rf"\b{re.escape(resource)}\b", type_str):
            return resource
    return None


def _resource_taint(ir: dict) -> set[str]:
    """The transitive closure of resource-typedness over the type table
    (item 363 hardening F1).

    The base set is the `extern acquire` return handles (`_resource_types`). A
    record/variant type ANY of whose fields or case payloads mentions an
    already-tainted type is itself resource-typed — recursively, to a fixpoint.
    So a handle NESTED in a user record (`type Session = { conn: Sock, label:
    Str }`, `Sock` a resource) makes `Session` resource-typed too: a signature
    that carries `Session` across a seam is proxied, not copied by value.
    Without this closure a nested handle read as a plain value crossed the seam
    as a dead integer wearing the handle's type, detaching the undo/teardown
    contract from the copy — the data-loss the audit, the swap gate, and 363's
    boundary check all now refuse."""
    tainted = set(_resource_types(ir))
    types = ir.get("types") or {}
    changed = True
    while changed:
        changed = False
        for name, spec in types.items():
            if name in tainted:
                continue
            if spec.get("kind") == "record":
                member_types = list((spec.get("fields") or {}).values())
            elif spec.get("kind") == "variant":
                member_types = [c.get("payload") for c in spec.get("cases") or []]
            else:
                member_types = []
            if any(_resource_in(t, tainted) for t in member_types):
                tainted.add(name)
                changed = True
    return tainted


def distributability(ir: dict) -> dict:
    """service name -> {"verdict": ..., "reasons": [...], "resources": [...]}.

    `resources` is the STRUCTURED record of resource-type crossings — a list of
    ``{"method", "type"}`` — so a consumer (the cross-tier boundary check) keys
    the resource refusal on this kind rather than on the substring of a human
    reason string, which a wording change could silently disarm (F1)."""
    resources = _resource_taint(ir)
    report: dict[str, dict] = {}
    for name, service in (ir.get("services") or {}).items():
        problems: list[str] = []
        resource_hits: list[dict] = []
        for method, spec in (service.get("methods") or {}).items():
            types = [param.get("type") for param in spec.get("params") or []]
            if spec.get("returns"):
                types.append(spec["returns"])
            resource = next(
                (hit for t in types if (hit := _resource_in(t, resources))), None
            )
            if spec.get("emission"):
                problems.append(f"{method}: emission (sync)")
            elif not spec.get("async"):
                problems.append(f"{method}: not async fn")
            if resource:
                problems.append(f"{method}: resource type {resource} crosses")
                resource_hits.append({"method": method, "type": resource})
        if problems:
            report[name] = {"verdict": "address-space-bound", "reasons": problems,
                            "resources": resource_hits}
        else:
            report[name] = {
                "verdict": "transport-safe",
                "reasons": ["all operations async fn", "all params/returns value-typed"],
                "resources": [],
            }
    return report

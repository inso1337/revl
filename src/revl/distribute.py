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
by proxy, not by copy). Everything else is a value type and copies cleanly.

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


def distributability(ir: dict) -> dict:
    """service name -> {"verdict": ..., "reasons": [...]}."""
    resources = _resource_types(ir)
    report: dict[str, dict] = {}
    for name, service in (ir.get("services") or {}).items():
        problems: list[str] = []
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
        if problems:
            report[name] = {"verdict": "address-space-bound", "reasons": problems}
        else:
            report[name] = {
                "verdict": "transport-safe",
                "reasons": ["all operations async fn", "all params/returns value-typed"],
            }
    return report

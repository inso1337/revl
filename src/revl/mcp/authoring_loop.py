"""The agent's default authoring loop over MCP (issue #1704).

revl has four surfaces no other toolchain has: reuse by admission
(`revl_resolve`), a typed skeleton with fill specs (`revl_scaffold`), an exact
blast radius (`revl_query_withdraw`) and nine machine-checked guarantees. An
agent benchmark showed agents rarely reach for them first. This module holds
the three pieces that make them the default reach:

* `INSTRUCTIONS`, the `initialize` text, which names the loop in order with
  the exact verbs;
* `self_check`, which turns one compile into a pass/fail line for every
  guarantee G1 to G9, with the code and the fix on a failure, so `revl_check`
  answers "does this hold?" in one call;
* `blast_radius`, which `revl_edit` folds into its response so a change
  arrives with its cascade instead of needing a separate preflight call.
"""

from __future__ import annotations

from ..diagnostics import FIXES, GUARANTEES

#: The loop, in the order an agent should take it. Pinned by
#: tests/test_mcp_authoring_loop_1704.py.
LOOP = (
    ("reuse", "revl_resolve",
     "ask for an existing component that already provides the service; it "
     "returns source and manifest, so nothing is generated"),
    ("scaffold", "revl_scaffold",
     "otherwise start from a typed skeleton whose unknowns are hole[T], each "
     "with a fillSpec"),
    ("fill", "revl_edit",
     "fill one hole at a time from its fillSpec: write the expression into "
     "your draft and re-run revl_check, or send revl_edit {hole, expr} once "
     "the composition is running; each fill is compiled"),
    ("preflight", "revl_query_withdraw",
     "the exact blast radius of replacing or removing a component; revl_edit "
     "returns it for the components it touches, and revl_plan shows what a "
     "swap would do"),
    ("check", "revl_check",
     "`selfCheck` lists every guarantee G1-G9 as pass or fail, with the code "
     "and the fix (revl_explain <code> for more)"),
    ("commit", "revl_admit",
     "admit against the running manifest, then revl_swap (revl_edit "
     "re-admits and swaps on its own); on a cold start, revl_load"),
)

INSTRUCTIONS = (
    "Author revl components in this order: "
    + "; ".join(f"{i}. {step} with {verb}: {what}"
                for i, (step, verb, what) in enumerate(LOOP, 1))
    + ". Compile before proposing anything, and never swap a candidate that "
      "revl_admit has not admitted."
)

#: The nine guarantees a compile proves (DESIGN.md). The extended codes
#: (G-SECRET, G-RETAIN, ...) and the amendments are reported beside them.
CORE = tuple(f"G{n}" for n in range(1, 10))


def step_label(verb: str) -> str:
    """`Loop step N (name).` for a tool description, so every surface names
    the same order as the instructions."""
    for i, (step, name, _what) in enumerate(LOOP, 1):
        if name == verb:
            return f"Authoring loop step {i} of {len(LOOP)} ({step})."
    raise KeyError(verb)


def self_check(diagnostics: list[dict] | None, holes: list | None = None) -> dict:
    """Every guarantee G1-G9, from one compile.

    `diagnostics` is None when the compile succeeded: the checker enforces
    every guarantee before it returns an IR, so each one passes. On a refusal,
    a guarantee named by a diagnostic fails and carries that diagnostic's
    code, message, line and fix. The others are `unchecked`, not `pass`: the
    compile stopped before it could prove them, so a pass would be a claim the
    checker did not make. Refusals under any other code (a type error, an
    extended guarantee, an amendment) are listed in `otherFailures`, so a
    green G-row is never read past a red one elsewhere.
    """
    compiled = diagnostics is None
    failing: dict[str, list[dict]] = {}
    other: list[dict] = []
    for record in diagnostics or []:
        code = record.get("code")
        if code in CORE:
            failing.setdefault(code, []).append(record)
        else:
            other.append(_failure(record))

    rows = []
    for code in CORE:
        row = {"code": code, "guarantee": GUARANTEES[code]}
        if code in failing:
            row["status"] = "fail"
            row["fix"] = failing[code][0].get("fix") or FIXES.get(code)
            row["failures"] = [_failure(r) for r in failing[code]]
        elif compiled:
            row["status"] = "pass"
        else:
            row["status"] = "unchecked"
        rows.append(row)

    open_holes = len(holes or [])
    summary = {
        "pass": sum(r["status"] == "pass" for r in rows),
        "fail": sum(r["status"] == "fail" for r in rows),
        "unchecked": sum(r["status"] == "unchecked" for r in rows),
    }
    result = {
        "admissible": compiled and open_holes == 0,
        "guarantees": rows,
        "summary": summary,
        "otherFailures": other,
    }
    if compiled and open_holes:
        result["note"] = (f"every guarantee holds on this draft, but {open_holes} "
                          "open hole(s) keep it out of a running composition "
                          "(T3); fill them with revl_edit")
    elif not compiled and summary["unchecked"]:
        result["note"] = ("the compile refused before it could prove the "
                          "`unchecked` guarantees; fix the failures and "
                          "re-run revl_check")
    return result


def _failure(record: dict) -> dict:
    keep = ("code", "message", "file", "line", "fix")
    return {k: record[k] for k in keep if record.get(k) is not None}


def touched_components(before: dict | None, after: dict) -> list[str]:
    """Components whose definition differs between two compiled IRs, plus
    those added or removed. A component that also uses a service whose
    declaration changed counts as touched: its contract moved under it."""
    old = {c["name"]: c for c in (before or {}).get("components") or []}
    new = {c["name"]: c for c in after.get("components") or []}
    old_services = (before or {}).get("services") or {}
    new_services = after.get("services") or {}
    moved = {name for name in set(old_services) | set(new_services)
             if old_services.get(name) != new_services.get(name)}

    touched = set(old) ^ set(new)
    for name in set(old) & set(new):
        if _shape(old[name]) != _shape(new[name]):
            touched.add(name)
            continue
        uses = (set((new[name].get("provides") or {}).values())
                | set((new[name].get("requires") or {}).values()))
        if uses & moved:
            touched.add(name)
    return sorted(touched)


_POSITIONAL = frozenset({"line", "col", "column", "span", "source", "file"})


def _shape(value):
    """A component with its source positions removed, so an edit above it
    that only shifts its lines does not count as a change to it."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items() if k not in _POSITIONAL}
    if isinstance(value, list):
        return [_shape(v) for v in value]
    return value


def blast_radius(before: dict | None, after: dict) -> dict:
    """The `revl_query_withdraw` answer for every component a change touches,
    read off the RUNNING composition: what loses a provision while each one is
    replaced, in the order the runtime tears it down. A component the change
    adds has no dependents yet, so it gets an empty cascade."""
    from .. import query as Q  # noqa: PLC0415 - query imports the compiler

    touched = touched_components(before, after)
    running = {c["name"] for c in (before or {}).get("components") or []}
    radius = {}
    for name in touched:
        if before is None or name not in running:
            radius[name] = {"added": True, "cascade": [], "withdrawalOrder": [],
                            "breaks": 0}
            continue
        answer = Q.withdrawal(before, name)
        radius[name] = {k: answer[k] for k in
                        ("cascade", "withdrawalOrder", "orphanedKeys", "breaks",
                         "precision") if k in answer}
        if name not in {c["name"] for c in after.get("components") or []}:
            radius[name]["removed"] = True
    return {
        "touched": touched,
        "components": radius,
        "breaks": sum(r["breaks"] for r in radius.values()),
        "note": "exact, from the running composition's linked provider graph "
                "(the revl_query_withdraw answer for each touched component)",
    }


__all__ = ["INSTRUCTIONS", "LOOP", "CORE", "step_label", "self_check",
           "touched_components", "blast_radius"]

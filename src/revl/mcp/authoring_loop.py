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
* `blast_radius`, which `revl_edit`, `revl_swap` and `revl_change` fold into
  their responses so a change arrives with its cascade instead of needing a separate preflight call.
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
     "the exact blast radius of replacing or removing a component; revl_edit, "
     "revl_swap and revl_change return it for the components they touch, and "
     "revl_plan shows what a swap would do"),
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


def self_check(diagnostics: list[dict] | None, holes: list | None = None,
               unmet: list | None = None) -> dict:
    """Every guarantee G1-G9, from one compile.

    `diagnostics` is None when the compile succeeded: the checker enforces
    every guarantee before it returns an IR, so each one passes. On a refusal,
    a guarantee named by a diagnostic fails and carries that diagnostic's
    code, message, line and fix. The others are `unchecked`, not `pass`: the
    compile stopped before it could prove them, so a pass would be a claim the
    checker did not make. Refusals under any other code (a type error, an
    extended guarantee, an amendment) are listed in `otherFailures`, so a
    green G-row is never read past a red one elsewhere.

    `unmet` is `unmet_requirements(ir)` of the compiled composition (issue
    #2386). It is a warning, not a verdict: `admissible` does not read it. A
    requirement no revl component provides is legitimate, because a host
    service, a `--providers` model host, a placement remote or a non-revl
    provider can meet it at run time (`examples/app/notes.rvl`'s `webui`,
    issue #1751). It is listed in `unmetRequirements` with a note, because in
    this MCP session, which supplies none of those, the component would stay
    PENDING and a swap that adds it would be refused.
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
    unmet = list(unmet or []) if compiled else []
    result = {
        "admissible": compiled and open_holes == 0,
        "guarantees": rows,
        "summary": summary,
        "otherFailures": other,
    }
    if unmet:
        result["unmetRequirements"] = unmet
    if compiled and open_holes:
        result["note"] = (f"every guarantee holds on this draft, but {open_holes} "
                          "open hole(s) keep it out of a running composition "
                          "(T3); fill them with revl_edit")
        if unmet:
            result["note"] += ". " + _unmet_note(unmet)
    elif unmet:
        result["note"] = _unmet_note(unmet)
    elif not compiled and summary["unchecked"]:
        result["note"] = ("the compile refused before it could prove the "
                          "`unchecked` guarantees; fix the failures and "
                          "re-run revl_check")
    return result


def unmet_requirements(ir: dict) -> list[dict]:
    """Every `requires` key that no component of the compiled composition
    provides (issue #2386), one record per (component, key) in source order.

    The compiler admits such a requirement, and so does `admissible`: a host
    service, a `--providers` model host, a placement remote or a later load can
    provide the key. This MCP session supplies none of those, so the list is
    the warning that the component would stay PENDING here. A key any
    component provides counts as met, in any realm and a template's provision
    included: the compiler already checks a realm route (item 162), and a false
    report here would be worse than a missed one. Each record carries a `fix`
    that names the key and, when another component provides the same service
    under a different key, that key: the usual slip is requiring a service by
    its name, or requiring the service the component itself provides."""
    components = ir.get("components") or []
    provided: dict[str, str] = {}
    by_service: dict[str, list[tuple[str, str]]] = {}
    for comp in components:
        for key, service in (comp.get("provides") or {}).items():
            provided.setdefault(key, comp.get("name") or "")
            by_service.setdefault(service, []).append((key, comp.get("name") or ""))
    unmet = []
    for comp in components:
        name = comp.get("name") or ""
        for key, service in (comp.get("requires") or {}).items():
            if key in provided:
                continue
            unmet.append({"component": name, "key": key, "service": service,
                          "fix": _unmet_fix(name, key, service,
                                            by_service.get(service) or [])})
    return unmet


def _unmet_fix(component: str, key: str, service: str,
               providers: list[tuple[str, str]]) -> str:
    """The remedy for one unmet requirement (issue #2386). Another
    component's provision of the same service is named first: requiring it
    under its own key is the likely intent. Dropping the requirement is the
    fix only when the component itself is the one provider of the service."""
    others = [(k, by) for k, by in providers if by != component]
    if others:
        other_key, by = others[0]
        return (f"`{service}` is provided by `{by}` under the key `{other_key}`, "
                f"and a requirement is matched by key: write `requires "
                f"{other_key}: {service}`")
    own = [k for k, _by in providers]
    if own:
        return (f"`{component}` already provides `{service}` as `{own[0]}`, and "
                f"a component does not require the service it provides, so drop "
                f"`requires {key}`")
    return (f"provide `{key}: {service}` from a component, a host service, a "
            f"`--providers` model host or a placement remote, or drop "
            f"`requires {key}` from `{component}`")


def unmet_phrase(record: dict) -> str:
    """One unmet requirement in words, shared by `revl_check`'s note and the
    swap gate's PENDING refusal so both name the key the same way."""
    return (f"`{record['component']}` requires `{record['key']}: "
            f"{record['service']}`, which nothing in the composition provides")


def pending_reason(ir: dict, comp: dict, resolved) -> str:
    """Why the swap health gate found `comp`'s successor fiber PENDING (issue
    #2386): every requirement of it that does not resolve after the load, each
    named with its key and service. A key nothing in the composition provides
    carries the same fix `revl_check` gives; a key that is provided, but whose
    provider did not come up, says so instead."""
    name = comp.get("name") or ""
    static = {r["key"]: r for r in unmet_requirements(ir)
              if r["component"] == name}
    parts = []
    for key, service in (comp.get("requires") or {}).items():
        if key in static:
            record = static[key]
            parts.append(f"{unmet_phrase(record)} (fix: {record['fix']})")
        elif key not in resolved:
            parts.append(f"`{name}` requires `{key}: {service}`, whose provider "
                         f"did not come up")
    if not parts:
        return "the successor fiber is PENDING after the swap load"
    return ("the successor fiber is PENDING after the swap load because of an "
            "unmet requirement: " + "; ".join(parts))


def _unmet_note(unmet: list[dict]) -> str:
    named = " ".join(f"{unmet_phrase(r)}. Fix: {r['fix']}." for r in unmet)
    return (f"warning: {named} A host service, a `--providers` model host or "
            "a placement remote can provide such a key at run time, but this "
            "MCP session supplies none of them: here the component would stay "
            "PENDING, and a swap that adds it would be refused")


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


#: The precision a template entry carries: not exact, because the static
#: composition excludes it, and not an over-approximation either.
TEMPLATE_PRECISION = "template"

_TEMPLATE_NOTE = (
    "a spawn template is excluded from the static composition; its provisions "
    "come up per instance behind a spawn handle, so the linked ROOT graph has "
    "no withdrawal answer for it")


class BlastRadiusRefused(RuntimeError):
    """A touched component's withdrawal answer is not a withdrawal, and the
    component is not a spawn template. The radius cannot honestly answer, so it
    refuses rather than reporting zero breaks that would read as an empty
    cascade and could hide a real withdrawal set."""

    def __init__(self, component: str, answer: dict) -> None:
        self.component = component
        self.answer = answer
        super().__init__(
            f"the blast radius cannot name the withdrawal of {component!r}: "
            f"{answer.get('error') or 'the withdrawal query refused'}")


def blast_radius(before: dict | None, after: dict) -> dict:
    """The `revl_query_withdraw` answer for every component a change touches,
    read off the RUNNING composition: what loses a provision while each one is
    replaced, in the order the runtime tears it down. A component the change
    adds has no dependents yet, so it gets an empty cascade. A spawn template
    is not in the linked ROOT manifest, so its entry names the exclusion and
    carries no ROOT cascade; any other component the withdrawal query does not
    know refuses rather than reporting zeros."""
    from .. import query as Q  # noqa: PLC0415 - query imports the compiler

    touched = touched_components(before, after)
    running = {c["name"] for c in (before or {}).get("components") or []}
    templates = set(((before or {}).get("manifest") or {}).get("templates") or [])
    radius = {}
    template_answer = False
    for name in touched:
        if before is None or name not in running:
            radius[name] = {"added": True, "cascade": [], "withdrawalOrder": [],
                            "breaks": 0}
            continue
        answer = Q.withdrawal(before, name)
        if answer.get("ok"):
            radius[name] = {k: answer[k] for k in
                            ("cascade", "withdrawalOrder", "orphanedKeys", "breaks",
                             "precision") if k in answer}
        elif name in templates:
            # issue #2423: a spawned template is a component of the running IR
            # but not of the linked ROOT manifest, so `withdrawal` correctly
            # answers `unknown component`. Its provisions come up per instance,
            # so nothing at ROOT loses one. Say that in the entry instead of
            # letting it read as a real empty cascade.
            radius[name] = {"template": True, "cascade": [],
                            "withdrawalOrder": [], "orphanedKeys": [], "breaks": 0,
                            "precision": TEMPLATE_PRECISION,
                            "precisionNote": _TEMPLATE_NOTE}
            template_answer = True
        else:
            # Not a template: the manifest and the running IR disagree. Do not
            # turn that into a clean zero that hides a real withdrawal set.
            raise BlastRadiusRefused(name, answer)
        if name not in {c["name"] for c in after.get("components") or []}:
            radius[name]["removed"] = True
    note = ("exact, from the running composition's linked provider graph "
            "(the revl_query_withdraw answer for each touched component)")
    if template_answer:
        note = ("the revl_query_withdraw answer for each touched component in "
                "the linked provider graph. A spawn template is excluded from "
                "the static composition, so its entry is not an exact "
                "withdrawal answer: its provisions come up per instance behind "
                "a spawn handle and nothing at ROOT loses one")
    return {
        "touched": touched,
        "components": radius,
        "breaks": sum(r["breaks"] for r in radius.values()),
        "note": note,
    }


__all__ = ["INSTRUCTIONS", "LOOP", "CORE", "TEMPLATE_PRECISION",
           "BlastRadiusRefused", "step_label", "self_check",
           "touched_components", "blast_radius"]

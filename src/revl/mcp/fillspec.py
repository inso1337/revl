"""Hole-directed generation: enrich an open hole into a *fill spec*.

An open typed hole (docs/holes.md) is an obligation — it tells an agent *that*
it owes an expression of some type at some position. That is 60% of what an
agent needs. The obligation says what the hole must eventually *be*; it does
not say what the agent has to work *with*. The missing 40% is everything the
checker already knew standing at that position:

* the **expected** type the fill must meet;
* the **capability** upper bound — may an expression here cross the emission
  boundary, and within which named bound (the G4 question, docs/capabilities.md);
* the **bindings** in scope, with their types; and
* the **reachable services** — the injected dependencies whose methods a fill
  may call, with full signatures.

None of this is new inference. Every field is read straight off the
fully-lowered IR document (services table, component `requires`/`config`, the
enclosing provide-method's declared emission, and the `let`/param bindings that
precede the hole). This module is read-only on the checker: it *serializes*
what a check already established, so an agent can scaffold a component with
holes and fill them one at a time, each fill constrained by a spec that makes
most wrong answers unrepresentable.

The shape added to every obligation in `revl_check` is::

    "fillSpec": {
      "version": 3,
      "expected": "Str",
      "grammarCategory": "expression",
      "capability": {"permitsCrossing": false, "mayEmit": false, "bound": [],
                     "reason": "..."},
      "crossing": {"permitted": false, "required": false, "form": null,
                   "calls": [], "rule": "..."},
      "bindings": [{"name": "key", "type": "Str"}, ...],
      "reachableServices": [
        {"service": "Db", "method": "q", "signature": "q(sql: Str) -> Str",
         "instance": "db", "emission": false, "callableHere": true}
      ]
    }

VERSION 2 (this shape). Version 1 had no `version`, no `crossing`, no
`permitsCrossing` and no `callableHere`; every version-1 field is still here
with its version-1 meaning, so a version-1 reader keeps working. What version 2
fixes is a word: `mayEmit` reads as a property of the HOLE ("this position may
emit") and was read as an obligation ("this hole is an emission position, so
the fill must emit"). It is a permission only: a declared `emission` operation
bounds what its provider may do, and a provider may always be purer than
declared (G4). Version 2 states the permission under a name that cannot be
read as an obligation (`permitsCrossing`), says outright that a crossing is
never `required`, and gives the call-site FORM a crossing is written in, with
the exact crossings available at this position.

VERSION 3 adds `grammarCategory` (issue #1664): the syntactic category a fill
is a document of, a key of `revl.source_grammar.CATEGORIES`, so a client can
pass it to `revl grammar --format F --category C` (or the MCP `revl_grammar`
tool) and constrain its decoder to the hole's slot. It is read off the grammar
derived from the parser (`source_grammar.hole_category`), not a table: the
narrowest category every parse of the `hole` keyword passes through. Every
version-2 field is unchanged.
"""

from __future__ import annotations

from .. import source_grammar
from ..diagnostics import GUARANTEES
from ..holes import EMITTABLE_SECTIONS

#: The fillSpec shape this module writes. Version 1 had no `version` key.
FILL_SPEC_VERSION = 3

#: How a crossing is written at a hole, in any position that permits one.
CROSSING_FORM = ("emit <key>.<operation>(<args>) for an injected service, or "
                 "emit <extern>(<args>) for a declared emission extern; write "
                 "`let r = emit ...` to keep its value")

#: What `permitted`/`required` mean, said once, in the spec itself.
CROSSING_RULE = ("A crossing is a PERMISSION, never an obligation: a fill may "
                 "be a pure expression in every position. Where `permitted` "
                 "is true the fill may contain marked crossings (`emit`), one "
                 "marker per crossing, each through a call listed in `calls`; "
                 "where it is false a crossing is refused (G4). `required` is "
                 "always false.")


def _lit_type(value) -> str | None:
    """The type of an IR `lit` node — a declared primitive, not inference."""
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "Int"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, str):
        return "Str"
    return None


def _render_signature(method: str, decl: dict) -> str:
    """A service/fn method's declared signature as one readable line."""
    params = ", ".join(
        f"{p['name']}: {p['type']}" for p in decl.get("params", []))
    returns = decl.get("returns")
    rendered = f"{method}({params})"
    if returns is not None:
        rendered += f" -> {returns}"
    return rendered


def _service_return(services: dict, service: str, method: str) -> str | None:
    decl = services.get(service, {}).get("methods", {}).get(method)
    return decl.get("returns") if decl else None


def _expr_type(node, services: dict, functions: dict,
               bindings: dict) -> str | None:
    """The type of an IR expression, resolved from declared signatures only.

    Every branch reads a type that a declaration already fixed — a hole's own
    annotation, a literal's primitive, a name already bound, or the declared
    return of a function or service method. Nothing is inferred; an expression
    whose type no declaration pins down is reported as unknown (`None`), never
    guessed.
    """
    if not isinstance(node, dict):
        return None
    kind = node.get("kind")
    if kind == "hole":
        return node.get("type")
    if kind == "lit":
        return _lit_type(node.get("value"))
    if kind == "name":
        return bindings.get(node.get("id"))
    if kind == "fn":
        decl = functions.get(node.get("name"))
        return decl.get("returns") if decl else None
    if kind == "call":
        target = node.get("target") or {}
        service = None
        if target.get("kind") in ("req", "provided", "service"):
            service = bindings.get("@service:" + str(target.get("name")))
        if service:
            return _service_return(services, service, node.get("method"))
        return None
    return None


def _crossing_tokens(instance: str, decl: dict, carry: dict) -> set:
    """The capability tokens a crossing through `instance.<op>` contributes,
    as `emission_analysis._method_emissions` computes them: the require KEY,
    or, for a key declared `carrying(...)`, the carried tokens, plus the
    unnameable `*` when the carry cannot bound the operation's own reach."""
    carried = (carry or {}).get(instance)
    if not carried:
        return {instance}
    tokens = set(carried)
    caps = decl.get("capabilities")
    if caps is None or len(set(caps)) > len(tokens):
        tokens.add("*")
    return tokens


def _within(tokens: set, bound) -> bool:
    """Whether a crossing's tokens sit inside a provide-method's bound: `None`
    is bare `emission` (any boundary), a list is a scoped `emission[...]`."""
    if bound is None:
        return True
    return tokens <= set(bound)


def _crossing_calls(requires: dict, services: dict, externs: list,
                    capability: dict, carry: dict) -> list[dict]:
    """Every crossing a fill at this position may write, already in its
    call-site form: each injected service's emission operations and each
    declared emission extern whose tokens sit inside the bound."""
    if not capability.get("permitsCrossing"):
        return []
    bound = capability.get("bound")
    calls: list[dict] = []
    for instance, service in sorted((requires or {}).items()):
        methods = services.get(service, {}).get("methods", {})
        for method, decl in sorted(methods.items()):
            if not decl.get("emission"):
                continue
            tokens = _crossing_tokens(instance, decl, carry)
            if not _within(tokens, bound):
                continue
            args = ", ".join(f"<{p['name']}: {p['type']}>"
                             for p in decl.get("params", []))
            calls.append({"write": f"emit {instance}.{method}({args})",
                          "returns": decl.get("returns"),
                          "carrier": "service",
                          "capabilities": sorted(tokens)})
    for ext in externs or []:
        if ext.get("class") != "emission":
            continue
        tokens = set(ext.get("capabilities") or [ext["name"]])
        if not _within(tokens, bound):
            continue
        args = ", ".join(f"<{p['name']}: {p['type']}>"
                         for p in ext.get("params", []))
        calls.append({"write": f"emit {ext['name']}({args})",
                      "returns": ext.get("returns"),
                      "carrier": "extern",
                      "capabilities": sorted(tokens)})
    return calls


def _crossing(capability: dict, calls: list[dict]) -> dict:
    """The `crossing` block: may a fill here cross, must it (never), how a
    crossing is written, and which crossings exist at this position."""
    permitted = bool(capability.get("permitsCrossing"))
    return {
        "permitted": permitted,
        "required": False,
        "form": CROSSING_FORM if permitted else None,
        "calls": calls,
        "rule": CROSSING_RULE,
    }


def _callable_here(entries: list[dict], calls: list[dict]) -> list[dict]:
    """`reachableServices` with `callableHere`: a plain operation is callable
    in every position; an emission operation only where it is one of the
    crossings this position permits (and then only as `emit ...`)."""
    allowed = {c["write"].split("(", 1)[0] for c in calls}
    out = []
    for entry in entries:
        here = (not entry.get("emission")
                or f"emit {entry['instance']}.{entry['method']}" in allowed)
        out.append({**entry, "callableHere": here})
    return out


def _reachable_services(requires: dict, services: dict) -> list[dict]:
    """Every method of every injected dependency, with its signature.

    In a provide-method the reachable boundary is the component's `requires`:
    the services it was handed to call. Each is expanded to its full method
    table so a fill knows exactly what it may call and with what.
    """
    out: list[dict] = []
    for instance, service in sorted((requires or {}).items()):
        methods = services.get(service, {}).get("methods", {})
        for method, decl in methods.items():
            out.append({
                "service": service,
                "method": method,
                "signature": _render_signature(method, decl),
                "instance": instance,
                "emission": bool(decl.get("emission")),
            })
    return out


def _capability(emission: bool, capabilities, in_method: bool,
                reason: str) -> dict:
    """The emission upper bound at a hole's position (the G4 question).

    A hole is a pure expression, so it can never itself be `emit hole`
    (docs/holes.md §2). What the fill that *replaces* it may do is the real
    question: an expression at this position may cross the emission boundary
    only inside a provide-method whose service method is declared `emission`,
    and then only within that method's capability bound. `bound` is the list of
    named capabilities of a scoped `emission[db, log]`; `None` is bare
    `emission` — "any boundary"; an empty list with `mayEmit: false` is a
    position where no emission is permitted at all.
    """
    if not (in_method and emission):
        return {"permitsCrossing": False, "mayEmit": False, "bound": [],
                "reason": reason}
    return {
        # `permitsCrossing` is the version-2 name; `mayEmit` is the same bool
        # under its version-1 name, kept so a version-1 reader keeps working
        "permitsCrossing": True,
        "mayEmit": True,
        # bare `emission` carries no capability list -> unbounded ("any").
        "bound": list(capabilities) if capabilities is not None else None,
        "reason": "an emission-declared provide-method"
        + (f" scoped to {', '.join(capabilities)}" if capabilities else ""),
    }


def _obligation(hole: dict, fill_spec: dict) -> dict:
    """One obligation, in the same shape `diagnostics.obligations` produces,
    with the `fillSpec` added. Base fields stay byte-identical so an agent that
    only reads the obligation keeps working."""
    return {
        "severity": "obligation",
        "code": "T3",
        "category": "hole",
        "file": hole.get("file"),
        "line": hole.get("line"),
        "expected": hole.get("type"),
        "message": hole.get("message"),
        "guarantee": GUARANTEES["T3"],
        "fillSpec": fill_spec,
    }


def _walk_body(body, services, functions, bindings, capability,
               collected: list) -> None:
    """Walk a statement body in order, threading the scope forward.

    `bindings` maps a name to its resolved type; the `@service:<name>` entries
    record which service instance a name denotes so a call on it resolves. Each
    `let` extends the scope *after* its own value is examined, so a hole sees
    exactly the bindings that precede it — the checker's scope at that point.
    """
    for stmt in body or []:
        step = stmt.get("step")
        if step == "let":
            _collect_exprs(stmt.get("value"), services, functions,
                           dict(bindings), capability, collected)
            bindings[stmt["name"]] = _expr_type(
                stmt.get("value"), services, functions, bindings)
        elif step == "return":
            _collect_exprs(stmt.get("expr"), services, functions,
                           dict(bindings), capability, collected)
        else:
            # if/while/assert/emit and any other statement: their sub-exprs are
            # in the same scope, but they do not introduce a forward binding a
            # later hole would see, so scope is not extended here.
            _collect_exprs(stmt, services, functions, dict(bindings),
                           capability, collected)


def _collect_exprs(node, services, functions, bindings, capability,
                   collected: list) -> None:
    """Find every hole reachable inside one expression/sub-tree and record its
    fill spec against the scope in force here."""
    if isinstance(node, dict):
        if node.get("kind") == "hole":
            visible = [
                {"name": name, "type": typ}
                for name, typ in bindings.items()
                if not name.startswith("@")  # `@service:`/`@reachable`/... internals
            ]
            calls = _crossing_calls(
                bindings.get("@requires") or {}, services,
                bindings.get("@externs") or [], capability,
                bindings.get("@carry") or {})
            collected.append((node, {
                "version": FILL_SPEC_VERSION,
                "expected": node.get("type"),
                "grammarCategory": source_grammar.hole_category(),
                "capability": capability,
                "crossing": _crossing(capability, calls),
                "bindings": visible,
                "reachableServices": _callable_here(
                    bindings.get("@reachable", []), calls),
            }))
            return
        # `step` bodies nested in an expression are rare, but a statement dict
        # threaded here (see `_walk_body` else-branch) should still recurse.
        for value in node.values():
            _collect_exprs(value, services, functions, bindings, capability,
                           collected)
    elif isinstance(node, list):
        if node and all(
                isinstance(v, dict) and "step" in v for v in node):
            # a nested statement body (an `if`/`while` branch): thread its own
            # forward `let` scope, starting from the bindings visible here.
            _walk_body(node, services, functions, dict(bindings), capability,
                       collected)
            return
        for value in node:
            _collect_exprs(value, services, functions, bindings, capability,
                           collected)


def _position_context(component, services, externs) -> dict:
    """The internal (`@`-prefixed) entries every scope inside `component`
    carries: its reachable service table, its `requires` and `carrying(...)`
    maps, and the program's externs, which the crossing calls are read off."""
    return {
        "@reachable": _reachable_services(component.get("requires"), services),
        "@requires": component.get("requires") or {},
        "@carry": component.get("carry") or {},
        "@externs": externs,
    }


def _method_scope(component, method, service_decl, services, functions,
                  externs):
    """The binding scope a provide-method's body opens with: the component's
    config fields and the method's parameters, each with a declared type."""
    bindings: dict = _position_context(component, services, externs)
    for field in component.get("config", []) or []:
        bindings[field["name"]] = field.get("type")
    # record each injected dependency so a call on it resolves to its service.
    for instance, service in (component.get("requires") or {}).items():
        bindings["@service:" + instance] = service
    # method params: the IR method carries names only; their types are the
    # service method's declared parameter types, positionally.
    decl_params = service_decl.get("params", []) if service_decl else []
    for i, pname in enumerate(method.get("params", [])):
        bindings[pname] = decl_params[i]["type"] if i < len(decl_params) else None
    return bindings


def enrich(ir: dict) -> list[dict]:
    """Every open hole in `ir`, as an obligation carrying its fill spec.

    Sorted by (file, line) to match `holes.collect`, so this is a drop-in
    replacement for `diagnostics.obligations(ir["holes"])` in `revl_check`.
    """
    services = ir.get("services") or {}
    functions = {f["name"]: f for f in (ir.get("functions") or [])}
    externs = ir.get("externs") or []
    collected: list = []

    # Components: provide-methods (may be emission positions) and component-level
    # setup `let`/effect (always a pure position).
    for component in ir.get("components") or []:
        setup_scope: dict = _position_context(component, services, externs)
        for field in component.get("config", []) or []:
            setup_scope[field["name"]] = field.get("type")
        for instance, service in (component.get("requires") or {}).items():
            setup_scope["@service:" + instance] = service
        pure = _capability(False, None, in_method=False,
                           reason="a component setup position — pure, no emission")
        for stmt in component.get("body") or []:
            if stmt.get("step") == "provide":
                svc_methods = services.get(
                    stmt.get("service"), {}).get("methods", {})
                for method in stmt.get("methods", []):
                    decl = svc_methods.get(method.get("name"), {})
                    capability = _capability(
                        bool(decl.get("emission")), decl.get("capabilities"),
                        in_method=True,
                        reason="a non-emission provide-method — pure"
                        if not decl.get("emission") else "")
                    scope = _method_scope(
                        component, method, decl, services, functions, externs)
                    _walk_body(method.get("body"), services, functions,
                               scope, capability, collected)
            elif stmt.get("step") == "let":
                _collect_exprs(stmt.get("value"), services, functions,
                               dict(setup_scope), pure, collected)
                setup_scope[stmt["name"]] = _expr_type(
                    stmt.get("value"), services, functions, setup_scope)
            else:
                _collect_exprs(stmt, services, functions, dict(setup_scope),
                               pure, collected)

    # Top-level functions and tests are pure positions: a hole there may not
    # emit. Their scope is the declared parameters (functions) / nothing (tests).
    pure_fn = _capability(False, None, in_method=False,
                          reason="a function body — pure, no emission")
    for fn in ir.get("functions") or []:
        scope = {"@reachable": []}
        for p in fn.get("params", []):
            scope[p["name"]] = p.get("type")
        _walk_body(fn.get("body"), services, functions, scope, pure_fn,
                   collected)
    for section in ("tests",):
        for item in ir.get(section) or []:
            _walk_body(item.get("body"), services, functions,
                       {"@reachable": []}, pure_fn, collected)

    collected.sort(key=lambda c: (str(c[0].get("file")), c[0].get("line") or 0))
    return [_obligation(hole, spec) for hole, spec in collected]


__all__ = ["enrich", "EMITTABLE_SECTIONS", "FILL_SPEC_VERSION",
           "CROSSING_FORM", "CROSSING_RULE"]

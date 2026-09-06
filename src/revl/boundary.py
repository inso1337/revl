"""The G8 audit boundary walk — the one authoritative per-component boundary
surface (`_boundary`) and its supporting reachability helpers.

These are pure IR analysis functions with no dependency on the CLI. They live
here, apart from `revl.__main__`, so that the many modules that need them
(`plan`, `query`, `audit_diff`, `registry`, `profile`, `placement`, `cardinality`,
and the mcp server) can import them without pulling in the entire CLI
command-dispatch graph (argparse plus every `revl.cli.*` handler), which cost
~150 ms to import the first time it was reached on a hot path (issue #543 P-10).
`revl.__main__` re-exports every name below, so `__main__._boundary` and friends
still resolve for any caller that reaches them through the CLI module.
"""

from __future__ import annotations


# G8 audit: the pseudo-boundary recorded when a component reaches host code
# through a first-class function dispatch (an arrow-typed parameter or
# binding) — the same `*` capability the G4 analysis uses, for the same
# reason: no extern name can be given, because what runs is not statically
# boundable. The concrete names that travel alongside it are reported too.
_UNKNOWN_DISPATCH = "*"


def _ref_provenance(ref: dict) -> str:
    """The audit provenance string for one host-module ref (item 396/410):
    `path#symbol`, prefixed with the root KIND for an install-origin ref
    (`stdlib:backends/typescript/revl_fs_ts.ts#fsWrite`), so a reviewer sees
    which trust domain a crossing reaches into. A user ref has no `root` key and
    renders exactly as 396(B) did — byte-identical."""
    prefix = f"{ref['root']}:" if ref.get("root") else ""
    return f"{prefix}{ref['path']}#{ref['symbol']}"


def _fn_call_names(node, out: set) -> None:
    """Collect callable references in an IR tree: component positions use
    `{"kind": "fn", "name"}`, fn bodies use `{"kind": "call", "callee":
    {"kind": "var", ...}}`. Non-callable vars are filtered by the caller
    against known fn/extern names."""
    if isinstance(node, dict):
        if node.get("kind") == "fn" and isinstance(node.get("name"), str):
            out.add(node["name"])
        if node.get("kind") == "call":
            callee = node.get("callee")
            if isinstance(callee, dict) and callee.get("kind") == "var" \
                    and isinstance(callee.get("name"), str):
                out.add(callee["name"])
        for value in node.values():
            _fn_call_names(value, out)
    elif isinstance(node, list):
        for value in node:
            _fn_call_names(value, out)


def _extern_reachability(ir: dict) -> dict[str, set]:
    """fn name -> transitively reachable extern names (host-code surface)."""
    externs = {ext["name"] for ext in ir.get("externs") or []}
    functions = ir.get("functions") or {}
    if not isinstance(functions, dict):
        functions = {fn.get("name"): fn for fn in functions}

    direct: dict[str, set] = {}
    for name, decl in functions.items():
        calls: set = set()
        _fn_call_names(decl, calls)
        direct[name] = calls

    reach: dict[str, set] = {}

    def resolve(name: str, trail: set) -> set:
        if name in reach:
            return reach[name]
        if name in trail:
            return set()
        found: set = set()
        for callee in direct.get(name, set()):
            if callee in externs:
                found.add(callee)
            elif callee in direct:
                found |= resolve(callee, trail | {name})
        reach[name] = found
        return found

    for name in direct:
        resolve(name, set())
    reach["__externs__"] = externs
    return reach


def _boundary(ir: dict) -> dict:
    """G8: the enumerable boundary surface per component — every emission
    call site (including teardown-position ones), the capabilities each of
    those call sites may cross (docs/capabilities.md), compensation counts,
    iteration boundaries, and reachable host code (externs, transitively
    through functions).

    The capability map is what turns "this component emits" into "this
    component reaches *these* boundaries": each emission is annotated with the
    scope its declaration carries, and `*` is an unscoped `emission` — an
    operation whose declaration makes no promise about where it goes."""
    reach = _extern_reachability(ir)
    externs = reach["__externs__"]
    extern_class = {ext["name"]: ext for ext in ir.get("externs") or []}
    # the capability fixed point (G4's own analysis) — see the note where it
    # joins the walk below; without it a first-class dispatch is invisible
    from .lower import _emitting_capabilities  # noqa: PLC0415 — lazy, like plan

    fns = ir.get("functions") or []
    if isinstance(fns, dict):
        fns = list(fns.values())
    fn_caps_map = _emitting_capabilities(fns, ir.get("externs") or [])

    def _collect_arrows(node, out):
        # every `let <name> = (…) => …` binding in scope, keyed by the safe IR
        # name the application's callee resolves to. A component/method body is
        # a flat statement list plus nested arms, so one recursive pass finds
        # them all regardless of position.
        if isinstance(node, dict):
            if node.get("step") == "let" and isinstance(node.get("value"), dict) \
                    and node["value"].get("kind") == "arrow":
                out[node.get("name")] = node["value"]
            for value in node.values():
                _collect_arrows(value, out)
        elif isinstance(node, list):
            for value in node:
                _collect_arrows(value, out)

    def _param_emission_methods(body, param):
        # the emission methods called on `param` inside an arrow body — the
        # receiver lowers to a `name` node bound to the parameter (`t.run(s)` ->
        # target {name: t}), the same shape whether or not it is `emit`-marked
        # (the marker leaves no residue on the node). Yields (method, node).
        found = []

        def walk(n):
            if isinstance(n, dict):
                tgt = n.get("target")
                if n.get("kind") == "call" and isinstance(tgt, dict) \
                        and tgt.get("kind") == "name" and tgt.get("id") == param:
                    found.append(n.get("method"))
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)

        walk(body)
        return found

    report: dict[str, dict] = {}
    for comp in ir.get("components") or []:
        stats = {"emissions": set(), "compensated": 0, "awaits": 0, "capabilities": {}}
        arrow_defs: dict = {}
        _collect_arrows(comp.get("body") or [], arrow_defs)

        def walk_expr(node, comp=comp, stats=stats, arrow_defs=arrow_defs):
            if isinstance(node, dict):
                target = node.get("target")
                if node.get("kind") == "call" and isinstance(target, dict) and target.get("kind") == "req":
                    service = (comp.get("requires") or {}).get(target.get("name"))
                    spec = (((ir.get("services") or {}).get(service) or {}).get("methods") or {}).get(node.get("method")) or {}
                    if spec.get("emission"):
                        label = f"{target['name']}.{node['method']}"
                        stats["emissions"].add(label)
                        # `*` = declared bare `emission`: no promise about where
                        declared = spec.get("capabilities")
                        stats["capabilities"][label] = (
                            sorted(declared) if declared is not None else ["*"])
                # the spawn/instance seam: a provision-method call read off a
                # spawn handle (`s.<key>.<method>(...)`) carries no `req` target.
                # The receiver is an `instance-get` reached through `callee`,
                # not the `target` slot. Resolve the crossing to the spawned
                # component's own service method the same way the `req` arm
                # resolves a required method, so a granted-service emission
                # routed through a spawned worker is still enumerated at C's
                # boundary (item 246, G8 audit surface).
                if node.get("kind") == "call":
                    callee = node.get("callee")
                    if isinstance(callee, dict) and callee.get("kind") == "field":
                        recv = callee.get("target")
                        if isinstance(recv, dict) and recv.get("kind") == "instance-get":
                            service = recv.get("service")
                            mname = callee.get("name")
                            spec = (((ir.get("services") or {}).get(service) or {})
                                    .get("methods") or {}).get(mname) or {}
                            if spec.get("emission"):
                                label = f"{recv.get('key')}.{mname}"
                                stats["emissions"].add(label)
                                declared = spec.get("capabilities")
                                stats["capabilities"][label] = (
                                    sorted(declared) if declared is not None else ["*"])
                # the arrow-parameter seam (GHSA-wg4v-r47x-52p2 residual): a
                # provision handed in as an argument to a local arrow whose
                # parameter is service-typed crosses the boundary through the
                # parameter. Resolve the crossing to the provision's key +
                # spawned service method — the same label the direct spelling
                # `w.<key>.<method>` produces — so a granted-service emission
                # routed through a service-typed arrow parameter is enumerated
                # at C's boundary, matching the G4 marker demand at compile.
                callee = node.get("callee")
                if node.get("kind") == "call" and isinstance(callee, dict) \
                        and callee.get("kind") == "name" \
                        and callee.get("id") in arrow_defs:
                    arrow = arrow_defs[callee["id"]]
                    svc_params = arrow.get("service_params") or {}
                    params = arrow.get("params") or []
                    call_args = node.get("args") or []
                    for i, param in enumerate(params):
                        service = svc_params.get(param)
                        if service is None or i >= len(call_args):
                            continue
                        arg = call_args[i]
                        if not (isinstance(arg, dict)
                                and arg.get("kind") == "instance-get"):
                            continue
                        key = arg.get("key")
                        svc_name = arg.get("service") or service
                        methods = (((ir.get("services") or {}).get(svc_name) or {})
                                   .get("methods") or {})
                        for mname in _param_emission_methods(arrow.get("body"), param):
                            spec = methods.get(mname) or {}
                            if not spec.get("emission"):
                                continue
                            label = f"{key}.{mname}"
                            stats["emissions"].add(label)
                            declared = spec.get("capabilities")
                            stats["capabilities"][label] = (
                                sorted(declared) if declared is not None else ["*"])
                for value in node.values():
                    walk_expr(value)
            elif isinstance(node, list):
                for value in node:
                    walk_expr(value)

        def walk_steps(steps, stats=stats):
            for step in steps:
                kind = step.get("step")
                if kind == "await":
                    stats["awaits"] += 1
                if kind == "emit" and step.get("compensate") is not None:
                    stats["compensated"] += 1
                walk_expr(step)
                if kind == "provide":
                    for method in step.get("methods") or []:
                        walk_steps(method.get("body") or [])

        walk_steps(comp.get("body") or [])

        called: set = set()
        _fn_call_names(comp.get("body") or [], called)
        host: set = set()
        unknown_dispatch = False
        for name in called:
            if name in externs:
                host.add(name)
            else:
                host |= reach.get(name, set())
                # the name-only walk cannot see a first-class dispatch: a fn
                # whose body hands an emitting callable to a dispatcher (`f(x)`
                # through an arrow-typed parameter) reaches boundaries no call
                # name names. The lowerer's capability fixed point tracks that
                # — its concrete extern names join the surface, and `*` marks
                # the dispatch itself so the report can say what is unnameable.
                fn_caps = fn_caps_map.get(name) or set()
                host |= {c for c in fn_caps if c != "*"}
                if "*" in fn_caps:
                    unknown_dispatch = True

        # First-class launder (G8 item 24): a host extern reached ONLY as a
        # value handed to a dispatcher — `indirect(ship, a)` — is named in no
        # call position, so the walk above never sees it. It is exactly the
        # reach the G4 fixed point already tracks to keep the read-only hint
        # sound: `_calls_in`'s value channel records the escaping callable, and
        # `fn_caps_map` says which boundaries that value carries. Fold that same
        # first-class reach onto the surface so a laundered `host:` crossing is
        # enumerated identically to a direct call (audit --diff can then see it).
        from .emission_analysis import _calls_in  # noqa: PLC0415 — lazy, like plan
        value_refs: set = set()
        _calls_in(comp.get("body") or [], set(), values=value_refs)
        for ref in value_refs:
            ref_caps = fn_caps_map.get(ref) or set()
            host |= {c for c in ref_caps if c != "*"}
            if "*" in ref_caps:
                unknown_dispatch = True

        if unknown_dispatch:
            host.add(_UNKNOWN_DISPATCH)

        report[comp["name"]] = {
            "emissions": sorted(stats["emissions"]),
            "capabilities": dict(sorted(stats["capabilities"].items())),
            "compensated": stats["compensated"],
            "awaits": stats["awaits"],
            "externs": [
                {"name": _UNKNOWN_DISPATCH,
                 "class": "first-class dispatch",
                 "backends": []}
                if name == _UNKNOWN_DISPATCH else
                {"name": name,
                 "class": extern_class.get(name, {}).get("class"),
                 # item 396: union ref tiers so a ref-only extern shows its tier
                 # (not "no bodies"), and carry the ref provenance for the render.
                 "backends": sorted(
                     set(extern_class.get(name, {}).get("bodies") or {})
                     | set(extern_class.get(name, {}).get("refs") or {})),
                 # item 247: the DECLARED capability scope of a scoped extern
                 # (`emission[db]`, `witnessed[fs]`). That token — not the
                 # extern NAME — is what a scoped crossing is keyed on across
                 # every other authority surface: item 343 made the approval
                 # `ClassMap` read it, `audit_diff._capability_registers` keys
                 # the register floor by it, and `_lower_secrets` binds by it.
                 # Carrying it here is what lets `policy.component_reach` grade
                 # a DIRECTLY EMITTED extern in the same namespace as the rule
                 # that names it. Absent for an unscoped extern, whose token IS
                 # its name, so every pre-247 audit entry is byte-identical.
                 **({"capabilities": sorted(
                     extern_class.get(name, {}).get("capabilities") or ())}
                    if extern_class.get(name, {}).get("capabilities") else {}),
                 **({"refs": {tier: _ref_provenance(r) for tier, r in
                              (extern_class.get(name, {}).get("refs") or {}).items()}}
                    if extern_class.get(name, {}).get("refs") else {})}
                for name in sorted(host)
            ],
            # taint provenance (item 249, Decision 5): origins that reach an
            # emission here, and origins declassified here. Present only when the
            # component touches taint, so a taint-free surface is byte-identical.
            **({"taint": comp["taint"]} if comp.get("taint") else {}),
        }
    return report



"""Emission reachability and the G4 evidence trail (docs/capabilities.md).

Split out of `lower.py` (roadmap item 17): the least-fixed-point that decides
which functions/externs reach an irreversible host effect, the capability sets
they cross, and the witness/evidence machinery that explains a verdict. Pure
code movement — every name here is re-exported from `revl.lower`, so the public
import surface is unchanged. Functions here read only `env`/`program`
attributes (never the lowering spine), so this module does not import from
`lower`.
"""

from __future__ import annotations

from .parser import Program
from .why import TraceStep


def _calls_in(node, found: set, values: set | None = None) -> None:
    """Function/extern names a lowered node calls. Component bodies lower a
    call to `{kind: fn, name}`; pure fn bodies to `{kind: call, callee:
    {kind: var, name}}`.

    With `values` (a set, filled in place), a *first-class* reference — a
    bare `{kind: var, name}` naming a callable in value position, where the
    function value itself flows instead of a call happening — is recorded
    too. Call-callee positions are excluded: `f(x)` is a call (already in
    `found`), not a value escaping. This is how the emission analysis sees
    an emitting callable being handed to code that may dispatch it.
    """
    if isinstance(node, dict):
        kind = node.get("kind")
        if kind == "fn" and isinstance(node.get("name"), str):
            found.add(node["name"])
        callee = node.get("callee")
        if kind == "call" and isinstance(callee, dict) \
                and callee.get("kind") == "var" and isinstance(callee.get("name"), str):
            found.add(callee["name"])
        elif values is not None and kind == "var" \
                and isinstance(node.get("name"), str):
            values.add(node["name"])
        for key, value in node.items():
            if key == "callee" and kind == "call":
                continue  # the callee is a call target, not a flowing value
            _calls_in(value, found, values)
    elif isinstance(node, list):
        for value in node:
            _calls_in(value, found, values)


def _emitting_fns(fns: list, externs: list, witness: dict | None = None) -> set:
    """Names whose call reaches an irreversible host effect: `emission`
    externs, and functions that reach one transitively. An `acquire` extern
    is *revertible* (it carries an inverse), so it is deliberately not one.

    `witness` (optional, filled in place) records *why* each derived name is
    in the set: `witness[caller] = callee`, the edge that put it there. The
    verdict is unchanged either way — this is only the evidence the fixed
    point would otherwise throw away (why.py)."""
    return set(_emitting_capabilities(fns, externs, witness))


def _emitting_capabilities(fns: list, externs: list,
                           witness: dict | None = None) -> dict[str, set]:
    """`_emitting_fns` refined from a boolean to a *set*: name -> the
    capabilities its call reaches (docs/capabilities.md).

    A host capability is named by the `emission` extern itself — that extern
    *is* the boundary — so `extern emission fn send` contributes `send`, and a
    `fn` contributes the union of what it calls, not its own name. The fixed
    point is the same least one `_emitting_fns` took, now over sets instead of
    a flag, which is why a capability propagates through a chain of `fn`s.

    `witness` is filled in place as the same walk proceeds — the set and the
    derivation come from one traversal, so they cannot disagree."""
    caps: dict[str, set] = {ext["name"]: {ext["name"]}
                            for ext in externs if ext.get("class") == "emission"}
    calls: dict[str, set] = {}
    passed: dict[str, set] = {}  # first-class callable references per fn body
    for fn in fns:
        called: set = set()
        values: set = set()
        _calls_in(fn.get("body") or [], called, values=values)
        calls[fn["name"]] = called
        passed[fn["name"]] = values

    changed = True
    while changed:  # least fixed point over the call graph
        changed = False
        for name, called in calls.items():
            reached: set = set()
            for callee in called:
                reached |= caps.get(callee, set())
            # A first-class reference to an emitting callable: the function
            # *value* escapes this body and may be dispatched by whoever
            # receives it (an arrow-typed parameter, a stored binding), so
            # what it runs is not statically boundable here. `*` is
            # deliberately the capability no `emission[...]` list can name
            # (docs/capabilities.md), so the may-emit propagates through the
            # fixed point exactly like a real emission — a plain service
            # method whose body hands an emitting callable to a dispatcher is
            # refused by the same G4 check as one that calls the extern
            # directly. A dispatching helper that only ever receives pure
            # functions stays clean: nothing emitting is ever referenced as a
            # value on any path that reaches it. The concrete names travel
            # too (`reached |= caps[ref]`): `*` marks the dispatch as
            # unnameable for the declaration checks, while the G8 audit
            # surface still reports which boundaries the chain reaches.
            for ref in sorted(passed.get(name, ())):
                if caps.get(ref):
                    reached.add("*")
                    reached |= caps[ref]
                    if witness is not None:
                        witness.setdefault(name, ref)
            if reached and not reached <= caps.get(name, set()):
                if witness is not None and name not in caps:
                    # of the callees that prove the point, take the one with
                    # the shortest onward chain (ties by name): the author is
                    # asked to read the shortest derivation, deterministically.
                    # A body flagged only by the first-class-value edge has no
                    # emitting name-callee; its witness is already recorded.
                    culprits = sorted(c for c in called if caps.get(c))
                    if culprits:
                        witness[name] = min(
                            culprits,
                            key=lambda callee: _witness_depth(callee, witness))
                caps.setdefault(name, set()).update(reached)
                changed = True
    return caps


def _capability_hint(service: str, method: str, declared, extra: list[str]) -> str:
    """The repair for a provider that exceeds its declared capability set."""
    nameable = [cap for cap in extra if cap != "*"]
    if not nameable:  # nothing to widen *to* — the boundary has no name
        return (f"only a named boundary can be granted — give the emission a "
                f"capability (a required key or an `emission` extern) or declare "
                f"`emission fn {method}(...)` without a scope in service "
                f"`{service}` (G4)")
    widened = list(declared) + [cap for cap in nameable if cap not in declared]
    return (f"a capability-scoped emission bounds *where* a provider may cross "
            f"the boundary — widen the declaration to "
            f"`emission[{', '.join(widened)}] fn {method}(...)` in service "
            f"`{service}`, or route this emission through a declared "
            f"capability (G4)")


def _witness_depth(name: str, witness: dict) -> int:
    """Hops from `name` down to the emission that made it emitting. The
    witness graph is acyclic by construction — an entry is only ever written
    for a name that was *not* yet emitting, pointing at one that was — but
    the guard keeps a malformed map from hanging the compiler."""
    depth, seen = 0, {name}
    while name in witness:
        name = witness[name]
        if name in seen:
            break
        seen.add(name)
        depth += 1
    return depth


def _emission_chain(name: str, witness: dict) -> list[str]:
    """`name` followed to the emission it reaches, e.g.
    ["writeThrough", "audit_log", "audit_write"]."""
    chain, seen = [name], {name}
    while name in witness:
        name = witness[name]
        if name in seen:
            break
        seen.add(name)
        chain.append(name)
    return chain


class _EmissionEvidence:
    """The G4 fixed point's evidence: the witness edge behind each derived
    emitting name, plus enough of the declaration table to give every hop in
    a chain a source location.

    Deliberately a companion of `_emitting_fns` rather than a change to it:
    the set that decides the verdict stays exactly what it was."""

    def __init__(self, program: Program) -> None:
        self.witness: dict[str, str] = {}
        self._files = getattr(program, "decl_files", None) or {}
        self._fallback_file = program.filename
        self._decls: dict[str, object] = {}
        for decl in program.fn_decls:
            self._decls.setdefault(decl.name, decl)
        for decl in program.externs:
            self._decls.setdefault(decl.name, decl)
        self.emission_externs = {
            decl.name for decl in program.externs
            if decl.classification == "emission"
        }
        # async externs (roadmap item 80): name -> decl, for the v1 coloring
        # check to locate an async extern a body reaches (async-extern.md §3)
        self.async_externs = {
            decl.name: decl for decl in program.externs
            if getattr(decl, "async_", False)
        }

    def locate(self, decl) -> tuple[str | None, int | None]:
        if decl is None:
            return (None, None)
        return (self._files.get(id(decl)) or self._fallback_file or None,
                getattr(decl, "line", None))

    def capabilities_of(self, name: str) -> tuple:
        """Which capabilities `name`'s emission reaches.

        Empty today: G4's `emitting_fns` is a plain set, so "emission" is all
        the analysis knows. This is the single seam for the capability-set
        work — the moment a declaration carries a `capabilities` attribute
        every why-trace starts showing it, in the rendering and in the JSON
        alike, with no other edit (see `TraceStep.capabilities`)."""
        return tuple(getattr(self._decls.get(name), "capabilities", ()) or ())

    def step_for(self, name: str, last: bool) -> TraceStep:
        decl = self._decls.get(name)
        file, line = self.locate(decl)
        emission = last or name in self.emission_externs
        return TraceStep(name, "emission" if emission else "call", file, line,
                         "emission" if emission else None,
                         self.capabilities_of(name))

    def chain_steps(self, name: str) -> list[TraceStep]:
        chain = _emission_chain(name, self.witness)
        return [self.step_for(hop, index == len(chain) - 1)
                for index, hop in enumerate(chain)]


def _method_emissions(body: list, env: "Env",
                      steps_out: dict | None = None) -> tuple[list[str], set]:
    """What calling a provide-method irreversibly causes: `emit` steps and
    reachable emitting functions/externs. Teardown-position emissions count
    — calling the method schedules them.

    Returns `(evidence, capabilities)`: the human-readable call sites, and the
    *set* of boundaries they cross (docs/capabilities.md). A call through a
    required key `db` is capability `db` — the key is composition-wide (G2
    makes it unique), so it names the same boundary to every reader; host code
    is named by the `emission` extern it reaches.

    `steps_out` (optional, filled in place) maps each returned label to the
    derivation behind it: a list of `TraceStep`s running from the callee the
    method names down to the emission it reaches. Additive out-parameter for
    the same reason `_emitting_fns` takes one — the labels, and so the
    message, are byte-identical whether or not evidence is collected."""
    found: list[str] = []
    seen: set = set()
    caps: set = set()

    def note(label: str, steps: list | None = None) -> None:
        if label not in seen:
            seen.add(label)
            found.append(label)
            if steps_out is not None and steps is not None:
                steps_out[label] = steps

    def service_emission_step(local: str | None, method: str | None) -> list:
        """The terminal step for `local.method`, an `emission fn` on the
        service bound to `local`."""
        service = env.services.get(env.requires.get(local) or "")
        decl = service.methods.get(method) if service is not None else None
        evidence = getattr(env, "emission_evidence", None)
        # a MethodDecl has a line but no file of its own; its service does
        file, _ = evidence.locate(service) if evidence is not None else (None, None)
        line = getattr(decl, "line", None)
        label = f"{local}.{method}"
        detail = "emission" if service is None else f"emission `{service.name}.{method}`"
        # same seam as `_EmissionEvidence.capabilities_of`, for the service
        # side: whatever the operation declares it may reach, the trace shows
        capabilities = tuple(getattr(decl, "capabilities", ()) or ())
        return [TraceStep(label, "emission", file, line, detail, capabilities)]

    def walk(node):
        if isinstance(node, dict):
            if node.get("step") == "emit":
                expr = node.get("expr") or {}
                target = expr.get("target") or {}
                if target.get("kind") == "req":
                    note(f"{target.get('name')}.{expr.get('method')}",
                         service_emission_step(target.get("name"), expr.get("method")))
                    caps.add(target.get("name"))
                else:
                    note("a host emission")
                    # every host emission reaches a named `emission` extern
                    # (`_is_emission_call` admits nothing else); `*` is the
                    # unreachable-in-practice fallback, and it is deliberately
                    # a capability no `emission[...]` list can name, so an
                    # unnameable boundary fails the bound rather than passing it
                    reached: set = set()
                    _calls_in(expr, reached)
                    if not reached & env.emitting_fns:
                        caps.add("*")
            # an emission may also appear in value position (`let r = emit …`)
            target = node.get("target")
            if node.get("kind") == "call" and isinstance(target, dict) \
                    and target.get("kind") == "req":
                service = env.services.get(env.requires.get(target.get("name")) or "")
                decl = (service.methods.get(node.get("method"))
                        if service is not None else None)
                if decl is not None and decl.emission:
                    note(f"{target.get('name')}.{node.get('method')}",
                         service_emission_step(target.get("name"), node.get("method")))
                    caps.add(target.get("name"))
            calls: set = set()
            values: set = set()
            _calls_in(node, calls, values=values)
            for name in sorted(calls & env.emitting_fns):
                evidence = getattr(env, "emission_evidence", None)
                note(f"{name}()",
                     evidence.chain_steps(name) if evidence is not None else None)
                caps.update(env.emitting_caps.get(name) or {"*"})
            # a first-class reference to an emitting callable: the value may
            # be dispatched by whoever receives it (an arrow-typed parameter,
            # a stored binding), so the method reaches code no bound can
            # name — same verdict as calling it, one indirection later
            for name in sorted(values & env.emitting_fns):
                evidence = getattr(env, "emission_evidence", None)
                note(f"{name} (passed as a function value)",
                     evidence.chain_steps(name) if evidence is not None else None)
                caps.update(env.emitting_caps.get(name) or set())
                caps.add("*")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return found, caps

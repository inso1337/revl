"""Checked parallel-emission partition (item 259, Slice 1 - checker only).

Derives, per component, a parallelizable PARTITION of each straight-line
emission run from the capability declarations already present (item 294 scopes
and Def. 39 `commutative`). This slice is CHECKER-ONLY: it reads the compiled IR
and produces a plan the audit surface renders. It changes NO runtime behaviour
and executes nothing in parallel; the runtime fan-out (slice 2) is a separate
landing that consumes this plan. The exit criterion here is that the partition
and its barriers are DERIVED correctly, not that any execution is equivalent.

The plan is an ordered list of groups per component, each group an ordered list
of emission-step indices (indices number the emissions in body order across the
component). A group of size 1 is a plain sequential emission. A group of size > 1
names emissions a later slice's runtime MAY fire concurrently because every pair
in it is either declared-disjoint (`cap_order.disjoint`, D1) or same-key and
`commutative` (Def. 39). Anything not provably independent stays sequential,
silently, in its own singleton group. The default when the proof is absent is
sequential; the feature's worst case is "no speedup", never a wrong result.

Soundness of the derivation rests on three rules, all fail-safe:

  * The straight-line run. The unit is one straight-line run of emission steps
    inside an activation or provide-method body. The run resets to length zero
    at any sequence-breaking step (a control-flow join, a loop, an `await`, a
    `provide`/`spawn`/`effect` registration, an assignment). A parallel group
    never spans one.

  * The pairwise-with-the-whole-group grow. A group grows only while every new
    emission is compatible with EVERY emission already in it, not just the
    previous one. This closes the chain `a - b - c` hole where `a` and `c` share
    a key but `b` is disjoint from both: `a` and `c` do not land in one group
    unless they are themselves same-key `commutative`.

  * Barrier B (first-class emissions, HIGH-2 of the adversarial review). The
    three emission shapes below are exactly the arms `__main__._boundary`'s walk
    recognizes, and those arms surface only a `req`-target call and an
    `instance-get` provision call. An emission reached through a FIRST-CLASS
    ARROW (an emitting callable handed to a dispatcher) is named in no call
    position, so the arm walk never sees it; `__main__` catches it only at the
    fn-aggregate level through `_emitting_capabilities` (`*`/`unknown_dispatch`).
    So any straight-line step whose callee reach includes the fn-caps `*` marker
    HARD-BREAKS the run, exactly like a control-flow join. A hidden first-class
    crossing can then never be reordered around, because no group spans it.

Three emission shapes are read off the IR (the same ones `_boundary` enumerates):

  1. an `emit` step (`{"step": "emit", ...}`), including a host-extern emission;
  2. a `req`-target emission call (`target.kind == "req"`, method `emission`);
  3. a spawn-handle provision-method call `s.<key>.<method>(...)` reached through
     an `instance-get` receiver (the item 246 seam).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import cap_order
from .cap_order import Cap
from .emission_analysis import _calls_in, _emitting_capabilities

# Step kinds that CAN be pure straight-line filler: a `let`/expression that
# crosses no boundary does not break the run (so a benign computation between two
# disjoint emissions does not defeat their grouping). Every other step kind that
# is not an emission or a body container (`provide`/`timer`) is a sequence break.
_PURE_ELIGIBLE = frozenset({"let", "let_pattern", "expr", "call", "assert"})

# `_emitting_capabilities` marks a first-class dispatch with this token; a step
# reaching it is Barrier B.
_STAR = "*"


@dataclass(frozen=True)
class _Emission:
    """One emission site in a straight-line run: its position in the component's
    emission order, its declared capabilities (parsed to `Cap`), and whether the
    operation declares `commutative`."""

    index: int
    caps: tuple[Cap, ...]
    commutative: bool
    # The `emit` step dict this emission was read off, or None for the fail-safe
    # singleton an unresolvable `emit` degrades to. Slice 2's emitter consumes it
    # (`parallel_plan_steps`) to map a plan group back to the body steps it fans
    # out; slice 1's index-only `parallel_plan` never reads it.
    step: dict | None = None

    def tokens(self) -> set[str]:
        return {c.token for c in self.caps}


def _parse_caps(cap_strs) -> tuple[Cap, ...]:
    """Parse declared capability strings to `Cap`s. A string that will not parse
    (it should not occur on a validated IR) degrades to `*`, which forces the
    emission to a singleton - fail-safe."""
    out: list[Cap] = []
    for s in cap_strs:
        try:
            out.append(cap_order.parse_cap(s))
        except cap_order.CapError:
            out.append(Cap(_STAR, ()))
    return tuple(out)


def _resolve_emission(ir: dict, comp: dict, expr: dict):
    """Resolve an expression to `(cap_strings, commutative)` if it is one of the
    three emission shapes, else None. `commutative` reads the method spec's flag
    or its service's, the same flag admission threads (S2.3)."""
    if not isinstance(expr, dict):
        return None
    services = ir.get("services") or {}
    requires = comp.get("requires") or {}
    kind = expr.get("kind")
    target = expr.get("target")

    def _from_spec(svc: dict, spec: dict):
        declared = spec.get("capabilities")
        caps = list(declared) if declared is not None else [_STAR]
        commutative = bool(spec.get("commutative") or svc.get("commutative"))
        return caps, commutative

    # (2) req-target emission call.
    if kind == "call" and isinstance(target, dict) and target.get("kind") == "req":
        svc = services.get(requires.get(target.get("name"))) or {}
        spec = (svc.get("methods") or {}).get(expr.get("method")) or {}
        if spec.get("emission"):
            return _from_spec(svc, spec)

    # (3) spawn-handle instance-get provision call `s.<key>.<method>(...)`.
    if kind == "call":
        callee = expr.get("callee")
        if isinstance(callee, dict) and callee.get("kind") == "field":
            recv = callee.get("target")
            if isinstance(recv, dict) and recv.get("kind") == "instance-get":
                svc = services.get(recv.get("service")) or {}
                spec = (svc.get("methods") or {}).get(callee.get("name")) or {}
                if spec.get("emission"):
                    return _from_spec(svc, spec)

    # (1b) direct host-extern emission `emit send(...)`: `{"kind": "fn", "name"}`.
    if kind == "fn":
        entry = _extern_index(ir).get(expr.get("name"))
        if entry is not None and entry.get("class") == "emission":
            caps = entry.get("capabilities") or [entry["name"]]
            return list(caps), bool(entry.get("commutative"))

    return None


def _extern_index(ir: dict) -> dict:
    return {e["name"]: e for e in ir.get("externs") or []}


def _contains_emission_call(node, ir: dict, comp: dict) -> bool:
    """Whether a (non-`emit`) step hides an emission crossing in value position
    (`let r = emit ...`, an `instance-get` provision call bound to a name). Such
    a crossing is not one of the groupable straight-line emit steps and its host
    effect could be reordered around, so it is treated as a barrier - fail-safe."""
    if isinstance(node, dict):
        if _resolve_emission(ir, comp, node) is not None:
            return True
        return any(_contains_emission_call(v, ir, comp) for v in node.values())
    if isinstance(node, list):
        return any(_contains_emission_call(v, ir, comp) for v in node)
    return False


def _step_reach(step: dict, fn_caps_map: dict, ir: dict, comp: dict):
    """Classify a non-emission step's boundary reach:

      * "star"     - the step calls a fn/extern whose reach includes `*`
                     (Barrier B: a first-class emitting arrow it cannot name);
      * "concrete" - the step reaches a named crossing (a helper fn that emits,
                     or an emission call in value position);
      * None        - the step crosses no boundary (pure filler).
    """
    called: set = set()
    _calls_in(step, called)
    reach: set = set()
    for name in called:
        reach |= fn_caps_map.get(name, set())
    if _STAR in reach:
        return "star"
    if reach or _contains_emission_call(step, ir, comp):
        return "concrete"
    return None


def _independent(a: _Emission, b: _Emission) -> bool:
    """Disjoint declared caps and neither an unnameable `*` (S2.2)."""
    if _STAR in a.tokens() or _STAR in b.tokens():
        return False
    return all(cap_order.disjoint(x, y) for x in a.caps for y in b.caps)


def _same_key(a: _Emission, b: _Emission) -> bool:
    """A genuine reuse of one boundary: the two crossings resolve to the same
    declared capability set (S2.3), neither an unnameable `*`."""
    if _STAR in a.tokens() or _STAR in b.tokens():
        return False
    return set(a.caps) == set(b.caps)


def _reorderable(a: _Emission, b: _Emission) -> bool:
    """Same key and both declared `commutative` (Def. 39 execution payoff)."""
    return _same_key(a, b) and a.commutative and b.commutative


def _compatible(a: _Emission, b: _Emission) -> bool:
    return _independent(a, b) or _reorderable(a, b)


class _Partitioner:
    """Grows contiguous parallel groups over a component's straight-line emission
    runs, sealing the open group at every barrier and sequence break."""

    def __init__(self, ir: dict, comp: dict, fn_caps_map: dict) -> None:
        self.ir = ir
        self.comp = comp
        self.fn_caps_map = fn_caps_map
        self.groups: list[list[int]] = []
        # The same partition, but each group is the list of `emit` step DICTS its
        # indices name (slice 2's emitter reads this to fan out the actual steps).
        self.step_groups: list[list[dict]] = []
        self.current: list[_Emission] = []
        self.next_index = 0

    def _seal(self) -> None:
        if self.current:
            self.groups.append([e.index for e in self.current])
            self.step_groups.append(
                [e.step for e in self.current if e.step is not None])
            self.current = []

    def _add_emission(self, cap_strs, commutative: bool,
                      step: dict | None = None) -> None:
        emission = _Emission(self.next_index, _parse_caps(cap_strs), commutative,
                             step)
        self.next_index += 1
        # Grow only while pairwise-compatible with the WHOLE running group.
        if self.current and all(_compatible(m, emission) for m in self.current):
            self.current.append(emission)
        else:
            self._seal()
            self.current = [emission]

    def walk(self, steps) -> None:
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            kind = step.get("step")
            if kind == "emit":
                res = _resolve_emission(self.ir, self.comp, step.get("expr") or {})
                if res is None:
                    # an `emit` we cannot resolve: force a singleton, fail-safe.
                    self._seal()
                    self._add_emission([_STAR], False, step)
                else:
                    self._add_emission(res[0], res[1], step)
                continue
            if kind == "provide":
                # a registration: seal, then each provide-method body is its own
                # straight-line run (sealed on either side).
                self._seal()
                for method in step.get("methods") or []:
                    self.walk(method.get("body") or [])
                    self._seal()
                continue
            if kind == "timer":
                # a timer body fires on a tick, not in-line with the surrounding
                # emissions: its own run, sealed on either side.
                self._seal()
                self.walk(step.get("body") or [])
                self._seal()
                continue
            if kind in _PURE_ELIGIBLE:
                reach = _step_reach(step, self.fn_caps_map, self.ir, self.comp)
                if reach is not None:
                    # "star" is Barrier B; "concrete" is an unmodeled crossing.
                    self._seal()
                # else pure filler: does not break the run.
                continue
            # every other step kind (spawn/effect/if/for/while/await/return/...)
            # is a sequence break.
            self._seal()
        # the caller seals the trailing run (or the run before a barrier).


def parallel_plan(ir: dict) -> dict[str, list[list[int]]]:
    """Derive the parallelizable partition of every component's emission runs.

    Returns `{component_name: [group, ...]}` where each group is an ordered list
    of emission-step indices (indices number the component's emissions in body
    order). A component with no emissions is omitted. Every group of size 1 is a
    plain sequential emission; a group of size > 1 is a run of provably
    independent (or same-key `commutative`) emissions.

    Checker-only: this derives the plan; it executes nothing. Never raises on a
    validated IR - an unresolvable emission or capability degrades to a `*`
    singleton, so the worst case is less parallelism, never a wrong grouping."""
    fns = ir.get("functions") or []
    if isinstance(fns, dict):
        fns = list(fns.values())
    fn_caps_map = _emitting_capabilities(fns, ir.get("externs") or [])

    plan: dict[str, list[list[int]]] = {}
    for comp in ir.get("components") or []:
        part = _Partitioner(ir, comp, fn_caps_map)
        part.walk(comp.get("body") or [])
        part._seal()
        if part.groups:
            plan[comp["name"]] = part.groups
    return plan


def parallel_plan_steps(ir: dict) -> dict[str, list[list[dict]]]:
    """The same partition as :func:`parallel_plan`, but each group is the list of
    `emit` STEP DICTS it names rather than their integer indices.

    Slice 2's py emitter consumes this to map a plan group back to the body steps
    it fans out (identity on the step dicts), so the runtime fan-out and the
    checker's derivation cannot diverge - the emitter never re-derives the
    partition, it reads this one. The two entry points share `_Partitioner`, so a
    group here is byte-for-byte the same grouping `parallel_plan` reports.

    Every group member is an `emit` step (the only shape `_Partitioner` turns into
    an emission), so a consumer can assume `step["step"] == "emit"`. Groups whose
    emissions came from a nested provide/timer body still appear; the emitter maps
    only those whose members are steps of the body it is rendering and leaves the
    rest sequential - safe, since a group it cannot place stays unfanned."""
    fns = ir.get("functions") or []
    if isinstance(fns, dict):
        fns = list(fns.values())
    fn_caps_map = _emitting_capabilities(fns, ir.get("externs") or [])

    plan: dict[str, list[list[dict]]] = {}
    for comp in ir.get("components") or []:
        part = _Partitioner(ir, comp, fn_caps_map)
        part.walk(comp.get("body") or [])
        part._seal()
        if part.step_groups:
            plan[comp["name"]] = part.step_groups
    return plan


def has_parallel_group(plan: dict[str, list[list[int]]]) -> bool:
    """Whether any component's plan has a group of size > 1 - the condition under
    which the audit surface adds its (otherwise absent) `parallel_plan` key, so a
    body with no parallelizable group renders byte-identically to before."""
    return any(len(g) > 1 for groups in plan.values() for g in groups)

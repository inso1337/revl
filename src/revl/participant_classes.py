"""Typed participant classes for a multi-node step (issue #2725).

A deploy map (docs/deploy.md, docs/network-placement.md) names which components
run in which participant process. How that participant can be FINISHED is
already implicit in the declarations each component's crossings carry: an
`undo`, a `deferred` emission, a `witnessed` effect, a `compensate`, or an
approval floor. This module reads those declarations and derives ONE class per
participant, plus the plan-time refusal that class makes possible.

The classes name how the participant can be finished, not whether the
multi-node step is atomic. There is still no replicated WAL and no quorum
coordinator; a participant the conductor cannot reach is `unresolved`, never
`rolled-back` (docs/deploy.md §3). The class is a property of the participant's
own declarations, and the ordering rule below is the one thing it can decide
before any boundary opens.

The vocabulary is closed, ordered from the finest finish to the coarsest:

    prepare-reversible          an exact inverse is declared (`undo`); PREPARE
                                is effect-free and ABORT replays the inverse.
    deferred-irreversible       the irreversible tail is a `deferred` emission:
                                ABORT drops it (nothing fired) and COMMIT
                                flushes it once.
    recoverable-forward         a `witnessed` effect, whose recorded witness is
                                the recovery handle, or a `deferred` emission
                                with an idempotency register (re-issue, never
                                undo). The issue names this class for the
                                declaration that keeps a recovery record; the
                                class is derived from those two declarations,
                                never inferred from a name.
    compensable                 a best-effort offset (`compensate`), not a
                                restoration; residue is possible.
    irreversible                an immediate emission with no declared finish.
    irreversible-needs-approval an irreversible crossing under an approval
                                floor (`requires approval`, or a `with` edge):
                                it cannot be undone, so it must be last.

A participant with no boundary crossing at all reads `no-crossing`. When a
participant carries several crossings its class is the WORST one (the rank in
:data:`PARTICIPANT_CLASS_RANK`), because the weakest finish is the one that
decides what can be settled.

Nothing here adds a distributed commit protocol. The ordering refusal is a
plan-time admission rule: an `irreversible-needs-approval` participant may not
be ordered before a participant that can still fail, because a later failure
would strand a crossing nobody can undo. That is why the approved participant
must commit LAST.
"""

from __future__ import annotations

from .query import Composition

#: The closed participant-class vocabulary, finest finish first.
PREPARE_REVERSIBLE = "prepare-reversible"
DEFERRED_IRREVERSIBLE = "deferred-irreversible"
RECOVERABLE_FORWARD = "recoverable-forward"
COMPENSABLE = "compensable"
IRREVERSIBLE = "irreversible"
IRREVERSIBLE_NEEDS_APPROVAL = "irreversible-needs-approval"
NO_CROSSING = "no-crossing"

PARTICIPANT_CLASSES = (
    PREPARE_REVERSIBLE,
    DEFERRED_IRREVERSIBLE,
    RECOVERABLE_FORWARD,
    COMPENSABLE,
    IRREVERSIBLE,
    IRREVERSIBLE_NEEDS_APPROVAL,
    NO_CROSSING,
)

#: How much a class constrains finishing a participant. A participant's class is
#: the MAX over its crossings: the weakest finish is the one that decides what
#: can be settled. `no-crossing` is below every real crossing.
PARTICIPANT_CLASS_RANK = {
    NO_CROSSING: -1,
    PREPARE_REVERSIBLE: 0,
    DEFERRED_IRREVERSIBLE: 1,
    RECOVERABLE_FORWARD: 2,
    COMPENSABLE: 3,
    IRREVERSIBLE: 4,
    IRREVERSIBLE_NEEDS_APPROVAL: 5,
}

#: The one class an ordering refusal is keyed on. Named so a reader does not
#: have to compare strings across the module.
NEEDS_APPROVAL = IRREVERSIBLE_NEEDS_APPROVAL

#: The refusal rule name, matching the deploy map's existing `rule` vocabulary.
RULE_APPROVAL_ORDER = "irreversible-approval-order"
RULE_UNKNOWN_COMPONENT = "unknown-participant-component"


def worse(left: str, right: str) -> str:
    """The worse (more constraining) of two participant classes."""
    if PARTICIPANT_CLASS_RANK[right] > PARTICIPANT_CLASS_RANK[left]:
        return right
    return left


def _walk(node):
    """Every dict/list node in a lowered tree, root first. The same walk
    `query._walk` uses; kept local so this module reads one dependency, the
    Composition index, and not a private helper as well."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _step_carries_approval(nodes) -> bool:
    """Whether any `emit` step in `nodes` carries a `with` approval edge.

    Read off the lowered step (`step["approval"]`), the same edge the runtime
    frame check, the admission walk and the eraser read (item 246). An approval
    on any other node is not a crossing approval and does not raise the class.
    """
    for node in _walk(nodes):
        if (isinstance(node, dict) and node.get("step") == "emit"
                and node.get("approval") is not None):
            return True
    return False


def _extern_class(decl: dict, *, deferred: bool,
                  registered: bool) -> str | None:
    """One reached extern's participant class, from its DECLARED facts.

    ``deferred`` is the route-aware flag: a `deferred` extern reached as a
    function value fires at the call (it is not enqueued), so it is not held
    and reads as an immediate irreversible crossing. ``registered`` says a
    `witnessed` effect was reached in effect position, where its inverse is
    actually registered; reached any other way it fires and registers nothing,
    so it is irreversible (issue #1707).

    `None` means the extern is not a boundary crossing (a `pure` read or a
    `local` write).
    """
    if decl.get("requires_approval"):
        return IRREVERSIBLE_NEEDS_APPROVAL
    if deferred:
        return (RECOVERABLE_FORWARD if decl.get("register")
                else DEFERRED_IRREVERSIBLE)
    if decl.get("compensate") is not None:
        return COMPENSABLE
    if decl.get("class") == "witnessed":
        return RECOVERABLE_FORWARD if registered else IRREVERSIBLE
    if decl.get("undo") is not None:
        return PREPARE_REVERSIBLE
    if decl.get("class") == "emission":
        return IRREVERSIBLE
    return None


def _crossing(component: str, scope: str, kind: str, cls: str, *,
              name: str | None = None, via: str | None = None) -> dict:
    entry = {"component": component, "scope": scope, "kind": kind,
             "class": cls}
    if name is not None:
        entry["name"] = name
    if via is not None:
        entry["via"] = via
    return entry


def component_crossings(composition: Composition, component: str) -> list:
    """Every boundary crossing of one component with its derived class.

    Reads the same per-scope facts the approval `ClassMap` reads (service
    emissions with their `compensated` flag, reached host externs with their
    route-aware `deferred` flag) and adds the two facts those facts do not
    carry: the `with` approval edge on an emit step, and the `*` first-class
    widening. The declaration flags (`requires approval`, `undo`, `compensate`,
    `register`) are read from the IR extern entry, never inferred from a name.
    """
    crossings: list = []
    for scope_id in composition.scopes_of.get(component) or []:
        scope = composition.scopes[scope_id]
        nodes = scope.get("nodes") or []
        facts = scope.get("facts") or {}
        registered = composition.witnessed_registered(nodes)

        for fact in facts.get("externs") or []:
            name = fact.get("name")
            decl = composition.externs.get(name) or {}
            cls = _extern_class(
                decl, deferred=bool(fact.get("deferred")),
                registered=name in registered)
            if cls is None:
                continue
            crossings.append(_crossing(
                component, scope_id, "extern", cls, name=name,
                via="deferred" if fact.get("deferred") else None))

        for fact in facts.get("emissions") or []:
            label = f"{fact.get('key')}.{fact.get('method')}"
            cls = COMPENSABLE if fact.get("compensated") else IRREVERSIBLE
            crossings.append(_crossing(
                component, scope_id, "service", cls, name=label))

        if _step_carries_approval(nodes):
            crossings.append(_crossing(
                component, scope_id, "approval", NEEDS_APPROVAL))

        if composition.value_widens(nodes):
            crossings.append(_crossing(
                component, scope_id, "widening", IRREVERSIBLE))
    return crossings


def participant_classes(ir: dict, placement: dict) -> dict:
    """One class per participant of a deploy/placement map.

    The participant set and their components are read from
    ``placement["processes"][<p>]["components"]``, the same keys
    `deploy._local_participant_spec` reads. Participants are returned in the
    deploy map's commit order (`sorted(processes)`), which is the order
    `deploy_local_map` and `deploy_ssh_map` build their participant list in.

    An unknown component (named by the map, absent from the IR) contributes no
    crossing; :func:`missing_components` reports it separately so admission can
    refuse it rather than silently class the participant on partial evidence.
    """
    composition = Composition(ir)
    processes = placement.get("processes") or {}
    out: dict[str, dict] = {}
    for pname in sorted(processes, key=str):
        pconf = processes.get(pname) or {}
        components = [str(c) for c in (pconf.get("components") or [])]
        crossings: list = []
        for component in components:
            if component not in composition.components:
                continue
            crossings.extend(component_crossings(composition, component))
        cls = NO_CROSSING
        for crossing in crossings:
            cls = worse(cls, crossing["class"])
        out[str(pname)] = {
            "class": cls,
            "components": components,
            "raisedBy": crossings,
        }
    return out


def missing_components(ir: dict, placement: dict) -> list[dict]:
    """The components a deploy map names that the composition does not declare.

    Each entry names the participant and the component. Admission refuses these
    when an IR is supplied: a class derived over partial evidence could read
    `no-crossing` for a participant whose real crossing was simply not found.
    """
    declared = {c.get("name") for c in ir.get("components") or []}
    processes = placement.get("processes") or {}
    out: list[dict] = []
    for pname in sorted(processes, key=str):
        pconf = processes.get(pname) or {}
        for component in pconf.get("components") or []:
            if str(component) not in declared:
                out.append({"process": str(pname),
                            "component": str(component)})
    return out


def order_refusals(classes: dict) -> list[dict]:
    """Refuse a plan that orders an approved irreversible participant before a
    participant that can still fail.

    The order is the deploy map's commit order (`sorted(processes)`), the order
    `run_deploy` sends COMMIT in. An `irreversible-needs-approval` participant
    that is not last is refused: a later participant's COMMIT can fail, and
    then the approved crossing is already out and cannot be undone. The refusal
    names both participants and the fix (put the approved one last).
    """
    order = [str(p) for p in sorted(classes, key=str)]
    refusals: list[dict] = []
    for index, pname in enumerate(order):
        if classes[pname].get("class") != NEEDS_APPROVAL:
            continue
        later = order[index + 1:]
        if not later:
            continue
        fallible = next(
            (q for q in later
             if classes[q].get("class") != NO_CROSSING), later[0])
        refusals.append({
            "process": pname,
            "rule": RULE_APPROVAL_ORDER,
            "before": fallible,
            "reason": (
                f"`{pname}` reaches an irreversible crossing that needs a final "
                f"approval, but it is ordered before `{fallible}`, which can "
                f"still fail. If `{fallible}` fails at COMMIT, `{pname}`'s "
                f"crossing is already out and cannot be undone. Order `{pname}` "
                f"last, after every participant that can still fail."),
        })
    return refusals


def unknown_component_refusals(ir: dict, placement: dict) -> list[dict]:
    """The plan-time refusals for components the map names but the IR lacks.

    One refusal per (participant, component), in the deploy map's `rule` /
    `process` / `reason` shape so `admit_deploy_map` can append them beside its
    own refusals and the CLI renders them the same way.
    """
    refusals: list[dict] = []
    for missing in missing_components(ir, placement):
        refusals.append({
            "process": missing["process"],
            "rule": RULE_UNKNOWN_COMPONENT,
            "reason": (
                f"the deploy map places component {missing['component']!r} in "
                f"process {missing['process']!r}, but the composition does not "
                f"declare that component. A participant class derived over "
                f"partial evidence could read as safe; refused rather than "
                f"classed on what happened to be found."),
        })
    return refusals


def plan_refusals(ir: dict, placement: dict) -> list[dict]:
    """Every plan-time refusal the participant classes add for this map:
    an unknown placed component, then any unsafe approval order."""
    refusals = unknown_component_refusals(ir, placement)
    refusals.extend(order_refusals(participant_classes(ir, placement)))
    return refusals


def render(classes: dict, refusals: list[dict]) -> list[str]:
    """Human lines for the audit surface: one line per participant, then any
    plan-time refusal. A `raisedBy` crossing is named so the class is
    inspectable rather than asserted."""
    lines: list[str] = []
    width = max((len(name) for name in classes), default=0)
    for pname in sorted(classes, key=str):
        entry = classes[pname]
        components = ", ".join(entry.get("components") or []) or "—"
        raised = entry.get("raisedBy") or []
        line = (f"  {pname:<{width}}  [{entry['class']}]  "
                f"components: {components}")
        if raised:
            named = ", ".join(_crossing_text(c) for c in raised[:3])
            if len(raised) > 3:
                named += f", +{len(raised) - 3} more"
            line += f"  raised by: {named}"
        lines.append(line)
    for refusal in refusals:
        lines.append(
            f"  participant order REFUSED [{refusal['rule']}] "
            f"{refusal['process']}: {refusal['reason']}")
    return lines


def _crossing_text(crossing: dict) -> str:
    kind = crossing.get("kind")
    if kind == "extern":
        name = crossing.get("name") or "?"
        if crossing.get("via"):
            return f"`{name}` ({crossing['via']}, {crossing['class']})"
        return f"`{name}` ({crossing['class']})"
    if kind == "service":
        return f"`emit {crossing.get('name')}` ({crossing['class']})"
    if kind == "approval":
        return "an emit carrying a `with` approval edge"
    if kind == "widening":
        return "a first-class dispatch of an emitting callable (`*`)"
    return f"a {kind} crossing ({crossing['class']})"

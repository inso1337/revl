"""Each provided operation's effect class, and the class an edit changes
(issue #1707, D1 in docs/harness-gate-guide.md).

The approval policy reads one fact per call: the worst class over the call's
whole reach (`approval.ClassMap`). A class-(a) `witnessed` op auto-approves;
factor it behind a helper that also reaches a non-witnessed crossing and it
becomes class (c), prompting on every call. Nothing said so: the verbs that
compile, admit, edit and swap answered with the same summary either way.

A `witnessed` extern is class (a) only where its declared inverse is actually
REGISTERED, which is a property of the call site: the call must be the
acquisition of an `effect`/`let-effect` step (`Composition.witnessed_registered`
reads exactly the test `backends/python/emit.py` registers on). A witnessed
extern reached any other way — `let r = stash_path(p)` or
`return stash_path(p)` in a provide-method body — fires the host mutation and
registers nothing, so it is class (c) and its relay is class (c) too. The class
is therefore never more optimistic than the runtime's own behaviour.

This module turns the class map into two things a response carries:

* `effectClasses`: every provided operation with its class and the crossings
  that set it (the ones AT that class, since a class is the worst over the
  reach);
* given the running composition, `effectClassChanges` (every operation whose
  class moved, including ones added or withdrawn) and `effectClassWarnings`
  (every operation whose class ROSE, naming it and the crossing that raised
  it).

It reads the same `ClassMap` the per-call decision reads, so the report and
the decision cannot disagree. It needs no runtime and builds the map whether
or not an approval policy is on: the class is a property of the composition,
and an agent that has not turned the policy on still wants to know it.
"""

from __future__ import annotations

from .approval import _ORDER, ClassMap

_POSTURE = {
    None: "no boundary crossing",
    "a": "auto-approved, every crossing has a registered inverse",
    "b": "auto-approved and enumerated at commit",
    "c": "one prompt per call",
}


def _label(crossing: dict) -> str:
    """One crossing as the text an agent reads."""
    kind, comp = crossing.get("kind"), crossing.get("component")
    if kind == "emission":
        return f"`emit {crossing.get('key')}.{crossing.get('method')}` in {comp}"
    if kind == "extern":
        # issue #1707: a `witnessed` extern reached OUTSIDE effect position
        # registers no inverse, so it is not the class-(a) crossing its
        # declaration suggests. Say so where the reader meets it.
        why = (" — no inverse registered at this call site"
               if crossing.get("registered") is False else "")
        return (f"`{crossing.get('name')}` ({crossing.get('class')} extern) "
                f"in {comp}{why}")
    if kind == "widening":
        return f"an emitting callable handed on as a value in {comp}"
    return f"a {kind} crossing in {comp}"


def _identity(crossing: dict) -> tuple:
    return tuple(crossing.get(k) for k in
                 ("kind", "component", "key", "method", "name", "capability"))


def _shown(crossing: dict) -> dict:
    """The fields of a crossing a response carries, plus its text."""
    shown = {k: crossing[k] for k in ("kind", "component", "key", "method",
                                      "name", "capability", "actionClass",
                                      "registered")
             if crossing.get(k) is not None}
    shown["text"] = _label(crossing)
    return shown


def _at_class(reach: dict) -> list:
    """The crossings that set the reach's class, deduplicated in order."""
    seen, out = set(), []
    for crossing in reach["crossings"]:
        if crossing.get("actionClass") == reach["class"] and _identity(crossing) not in seen:
            seen.add(_identity(crossing))
            out.append(crossing)
    return out


def _operations(class_map: ClassMap) -> dict:
    """`(key, method) -> (component, reach)` for every provided operation the
    class map resolves to one provider."""
    out = {}
    for scope in class_map.index.scopes.values():
        if scope["kind"] != "provide-method":
            continue
        reach = class_map.classify_call(scope["key"], scope["method"])
        if reach is not None:
            out[(scope["key"], scope["method"])] = (reach["component"], reach)
    return out


def _class_map(ir) -> ClassMap | None:
    if not isinstance(ir, dict) or not ir.get("components"):
        return None
    return ClassMap(ir)


def provided_classes(ir) -> list:
    """`effectClasses`: each provided operation's class and what set it."""
    class_map = _class_map(ir)
    if class_map is None:
        return []
    return [{"key": key, "method": method, "component": component,
             "class": reach["class"],
             "raisedBy": [_shown(c) for c in _at_class(reach)]}
            for (key, method), (component, reach)
            in sorted(_operations(class_map).items())]


def _raised(before: dict | None, after: dict) -> list:
    """The crossings that raised `after` over `before`: those at the new class
    that the old reach did not have. When the class rose without a new
    crossing at it (a reached provider changed underneath), every crossing at
    the new class is named instead."""
    old = {_identity(c) for c in (before or {}).get("crossings") or []}
    at_class = _at_class(after)
    fresh = [c for c in at_class if _identity(c) not in old]
    return fresh or at_class


def _cls(text: str | None) -> str:
    return "no class" if text is None else f"class ({text})"


def _warning(key: str, method: str, component: str, before: dict | None,
             after: dict) -> dict:
    raised = _raised(before, after)
    old, new = (before or {}).get("class"), after["class"]
    message = (
        f"`{key}.{method}` ({component}) rose from {_cls(old)} to {_cls(new)}: "
        f"{', '.join(_label(c) for c in raised)} raised it. Before: "
        f"{_POSTURE[old]}. Now: {_POSTURE[new]}.")
    return {"code": "EFFECT_CLASS_ROSE", "key": key, "method": method,
            "component": component, "before": old, "after": new,
            "crossings": [_shown(c) for c in raised], "message": message}


def class_changes(before_ir, after_ir) -> tuple[list, list]:
    """`(effectClassChanges, effectClassWarnings)` from the running
    composition `before_ir` to `after_ir`."""
    before_map, after_map = _class_map(before_ir), _class_map(after_ir)
    before = _operations(before_map) if before_map is not None else {}
    after = _operations(after_map) if after_map is not None else {}
    changes, warnings = [], []
    for key, method in sorted(set(before) | set(after)):
        old = before.get((key, method))
        new = after.get((key, method))
        old_cls = old[1]["class"] if old else None
        new_cls = new[1]["class"] if new else None
        if old is not None and new is not None and old_cls == new_cls:
            continue
        component = (new or old)[0]
        changes.append({"key": key, "method": method, "component": component,
                        "before": old_cls, "after": new_cls})
        if old is not None and new is not None and _ORDER[new_cls] > _ORDER[old_cls]:
            warnings.append(_warning(key, method, component, old[1], new[1]))
    return changes, warnings


def report(after_ir, before_ir=None, *, against: bool = False) -> dict:
    """The fields a response carries. `against=True` adds the diff from
    `before_ir`, the running composition (absent or empty is a cold start: every
    operation is new and nothing can have risen)."""
    out = {"effectClasses": provided_classes(after_ir)}
    if against:
        changes, warnings = class_changes(before_ir, after_ir)
        out["effectClassChanges"] = changes
        out["effectClassWarnings"] = warnings
    return out

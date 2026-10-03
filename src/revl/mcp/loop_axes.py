"""The agent-loop axes `revl_state` reports (issue #1738).

Six measures of how a session used the loop, each as a numerator, a
denominator and their ratio, read from the session's own records. They are
reported always, with or without an approval policy, and they are cumulative
for the life of the MCP session: an unload, a commit or an abort does not
reset them.

The definitions (docs/harness-gate-guide.md carries the same text):

`reversibilityRate`
    Executed boundary calls (`revl_call`s whose reach crosses a boundary)
    that were witnessed with a registered inverse, class (a), over all
    executed boundary calls, classes (a), (b) and (c). A ticketed call counts
    once, when it runs after its approval; the refused first attempt crossed
    nothing.
`autoApprovedWithProof`
    Executed boundary calls that needed no human answer because the checker
    proved them revertible or deferred, classes (a) and (b), over the same
    denominator. A class-(c) call a standing approval or grant covered is in
    the denominator and not the numerator: a grant is consent, not proof.
`promptsPerSession`
    Prompts raised (per-call tickets, commit prompts, residue prompts, the
    `prompts` tally of item 245/246) over commit sessions. A commit session
    runs from a `load` to the `unload`, `revl_commit_confirm` or `revl_abort`
    that ends it; a swap stays in the same one. The open session counts.
`preflightCoverage`
    Composition edits whose touched components were all named by a
    blast-radius query earlier in the session (`revl_query_*`,
    `revl_live_query`, `revl_plan`) or by the cascade the edit's own response
    carries (`blastRadius`, #1704), over all composition edits. An edit is
    a `revl_swap`, `revl_edit`, `revl_rollback`, `revl_undo`, `revl_restore`,
    `revl_ship` or `revl_repair` that changed at least one component; a
    refused or no-op one touched nothing and is not counted.
`violationsCaughtBeforeExecution`
    Refusals at check, admit, plan, load, swap or edit time whose diagnostics
    name a guarantee, over those plus refusals at run time (a `revl_call`
    that failed; an approval ticket is a question, not a failure). Pre-execution refusals that name no guarantee (a session
    precondition, a usage error) are in neither count.
`residueAfterAbort`
    Unresolved compensation records `revl_abort` left (the `compensationResidue`
    it returns) over the aborts. A failed swap that reboots its predecessor is
    not an abort here.
"""

from __future__ import annotations

# Classes of a decided boundary call (approval.py). Class None is not a
# boundary call and is counted nowhere.
_CLASSES = ("a", "b", "c")

# Verbs whose result names the components a blast-radius question covered.
QUERY_TOOLS = frozenset({
    "revl_query_withdraw", "revl_query_drift", "revl_query_dependents",
    "revl_query_reach", "revl_query_emitters", "revl_live_query", "revl_plan",
})

# Verbs that edit the running composition. Each is counted only when the
# composition actually changed under it.
EDIT_TOOLS = frozenset({
    "revl_swap", "revl_edit", "revl_rollback", "revl_undo", "revl_restore",
    "revl_ship", "revl_repair",
})

# Verbs that refuse a change before anything runs.
PRE_EXECUTION_TOOLS = frozenset({
    "revl_check", "revl_admit", "revl_plan", "revl_load", "revl_swap",
    "revl_edit", "revl_ship", "revl_restore",
})

RUN_TIME_TOOLS = frozenset({"revl_call"})


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def _axis(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": _ratio(numerator, denominator)}


def _names(entries) -> set[str]:
    """The `component` of each entry of a list of dicts."""
    return {e["component"] for e in entries or []
            if isinstance(e, dict) and isinstance(e.get("component"), str)}


def covered_components(arguments: dict, payload: dict) -> set[str]:
    """The components a blast-radius query's result names: the component it
    asked about, the components it lists, and those its cascade, providers
    and call sites name. Read structurally, so every query shape is covered by
    the same rule."""
    names = {value for value in (arguments.get("component"), payload.get("component"))
             if isinstance(value, str)}
    for key in ("components", "impacted"):
        value = payload.get(key)
        if isinstance(value, list):
            names.update(v for v in value if isinstance(v, str))
        elif isinstance(value, dict):  # revl_plan: {added, replaced, withdrawn}
            for group in value.values():
                if isinstance(group, list):
                    names.update(v for v in group if isinstance(v, str))
    for key in ("cascade", "providers", "callSites", "sites"):
        names |= _names(payload.get(key))
    return names


def carried_components(payload: dict) -> set[str]:
    """The components an edit response's own cascade covers (`blastRadius`,
    #1704): the ones it touched, and each one's withdrawal cascade."""
    radius = payload.get("blastRadius")
    if not isinstance(radius, dict):
        return set()
    names = {n for n in radius.get("touched") or [] if isinstance(n, str)}
    for entry in (radius.get("components") or {}).values():
        if isinstance(entry, dict):
            names |= _names(entry.get("cascade"))
    return names


def names_a_guarantee(payload: dict) -> bool:
    return any(isinstance(d, dict) and d.get("guarantee")
               for d in payload.get("diagnostics") or [])


def is_refusal(payload: dict) -> bool:
    return (payload.get("ok") is False or payload.get("admitted") is False
            or payload.get("admissible") is False)


class LoopAxes:
    """The session-cumulative counters behind the six axes."""

    def __init__(self) -> None:
        self.boundary = {klass: 0 for klass in _CLASSES}
        self.unclassified = 0
        self.closed_prompts = 0
        self.closed_sessions = 0
        self.covered: set[str] = set()
        self.edits = 0
        self.preflighted_edits = 0
        self.refused_before = 0
        self.refused_at_run = 0
        self.aborts = 0
        self.abort_residue = 0

    # -- producers --------------------------------------------------------

    def record_call(self, klass) -> None:
        """One executed `revl_call`, by the class of its reach. `False` means
        the call could not be classified, which is counted on its own line
        rather than read as class none."""
        if klass is False:
            self.unclassified += 1
        elif klass in self.boundary:
            self.boundary[klass] += 1

    def close_owner(self, owner) -> None:
        """A commit session ended: fold its prompt tally in."""
        if owner is None:
            return
        self.closed_prompts += sum((owner.prompts or {}).values())
        self.closed_sessions += 1

    def record_abort(self, residue: list) -> None:
        self.aborts += 1
        self.abort_residue += len(residue or [])

    def record_tool(self, name: str, arguments: dict, payload: dict,
                    touched: list[str] | None) -> None:
        """One MCP tool call, after its handler returned. `touched` is the
        components the call changed in the running composition."""
        if not isinstance(payload, dict):
            return
        if name in QUERY_TOOLS and payload.get("ok", True) is not False:
            self.covered |= covered_components(arguments, payload)
        if name in EDIT_TOOLS and touched:
            self.edits += 1
            if set(touched) <= self.covered | carried_components(payload):
                self.preflighted_edits += 1
        if name in EDIT_TOOLS:
            # an answer the agent was handed covers later edits as a query does
            self.covered |= carried_components(payload)
        if name in PRE_EXECUTION_TOOLS and is_refusal(payload) \
                and names_a_guarantee(payload):
            self.refused_before += 1
        if name in RUN_TIME_TOOLS and payload.get("ok") is False \
                and not payload.get("approvalRequired"):
            self.refused_at_run += 1

    # -- the report -------------------------------------------------------

    def document(self, live_owner=None) -> dict:
        crossed = sum(self.boundary.values())
        prompts = self.closed_prompts
        sessions = self.closed_sessions
        if live_owner is not None:
            prompts += sum((live_owner.prompts or {}).values())
            sessions += 1
        return {
            "reversibilityRate": _axis(self.boundary["a"], crossed),
            "autoApprovedWithProof": _axis(self.boundary["a"] + self.boundary["b"],
                                           crossed),
            "promptsPerSession": _axis(prompts, sessions),
            "preflightCoverage": _axis(self.preflighted_edits, self.edits),
            "violationsCaughtBeforeExecution": _axis(
                self.refused_before, self.refused_before + self.refused_at_run),
            "residueAfterAbort": _axis(self.abort_residue, self.aborts),
            "boundaryCalls": {**self.boundary, "unclassified": self.unclassified},
        }
